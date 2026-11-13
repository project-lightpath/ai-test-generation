"""Durable run-state transitions for the ATG lifecycle.

The state machine is deliberately independent of persistence, HTTP, Tekton, or
any model/provider SDK. A caller can persist the returned state and event
fingerprint in the same transaction as its workflow journal.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import hashlib
import json
import re
from typing import Mapping


class RunStateError(ValueError):
    """Base class for invalid run-state operations."""


class DuplicateEventError(RunStateError):
    """Raised when an event id is reused with a different payload."""


class StaleEventError(RunStateError):
    """Raised when an event was based on an old state revision."""


class InvalidTransitionError(RunStateError):
    """Raised when an event is not valid for the current status."""


class InvalidEventError(InvalidTransitionError):
    """Raised when an event payload is malformed before transition evaluation."""


class SupersessionError(RunStateError):
    """Raised when a checkpoint replacement does not name the active checkpoint."""


EVENT_TYPES = frozenset({
    "admit", "admission_blocked", "analysis_started", "analysis_completed", "analysis_blocked", "analysis_inconclusive", "checkpoint_created", "checkpoint_expired",
    "decision_recorded", "planning_completed", "plan_submitted", "generation_started", "generation_completed",
    "execution_started", "execution_completed", "quality_passed", "quality_blocked",
    "quality_inconclusive", "quality_gap", "retry_requested", "return_to_analyze",
    "return_to_plan", "return_to_code", "return_to_execute", "cancel",
})
CHECKPOINT_KINDS = frozenset({"analysis_review", "plan_review", "code_review", "execution_review", "quality_review"})
DECISIONS = frozenset({"approved", "rejected", "blocked", "stated_gap"})
STATE_STATUSES = frozenset({
    "requested", "admitted", "analyzing", "analysis_review", "plan_review", "planning",
    "code_review", "coding", "execution_review", "executing", "quality_review",
    "completed", "blocked", "inconclusive", "stated_gap", "cancelled",
})
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")


@dataclass(frozen=True)
class RunEvent:
    """An append-only command against one run revision."""

    event_id: str
    run_id: str
    event_type: str
    expected_revision: int
    occurred_at: str
    checkpoint_id: str | None = None
    checkpoint_kind: str | None = None
    decision: str | None = None
    supersedes: str | None = None
    reason: str | None = None
    actor_id: str | None = None
    actor_role: str | None = None
    authorization_ref: str | None = None
    decision_id: str | None = None
    expires_at: str | None = None
    plan_id: str | None = None
    plan_revision: int | None = None
    plan_artifact_id: str | None = None
    plan_artifact_digest: str | None = None
    feedback_id: str | None = None
    generation_id: str | None = None
    artifact_id: str | None = None
    artifact_digest: str | None = None

    def __post_init__(self) -> None:
        """Validate event identity, payload, and event-specific requirements."""
        validate_identifier(self.event_id, "event_id")
        validate_identifier(self.run_id, "run_id")
        if not isinstance(self.event_type, str) or not self.event_type:
            raise InvalidEventError("event_type is required as a string")
        if self.event_type not in EVENT_TYPES:
            raise InvalidEventError(f"unsupported event type: {self.event_type}")
        if not isinstance(self.expected_revision, int) or isinstance(self.expected_revision, bool) or self.expected_revision < 0:
            raise InvalidEventError("expected_revision must be a non-negative integer")
        _validate_event_timestamp(self.occurred_at, "occurred_at")
        if self.expires_at is not None:
            _validate_event_timestamp(self.expires_at, "expires_at")
        for field_name in ("checkpoint_id", "supersedes", "actor_id", "authorization_ref", "decision_id"):
            value = getattr(self, field_name)
            if value is not None:
                validate_identifier(value, field_name)
        for field_name in ("plan_id", "plan_artifact_id"):
            value = getattr(self, field_name)
            if value is not None:
                validate_identifier(value, field_name)
        for field_name in ("generation_id", "artifact_id"):
            value = getattr(self, field_name)
            if value is not None:
                validate_identifier(value, field_name)
        if self.feedback_id is not None:
            validate_identifier(self.feedback_id, "feedback_id")
        if self.plan_revision is not None and (not isinstance(self.plan_revision, int) or self.plan_revision < 1):
            raise InvalidEventError("plan_revision must be a positive integer")
        if self.plan_artifact_digest is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", self.plan_artifact_digest):
            raise InvalidEventError("plan_artifact_digest must be a sha256 digest")
        if self.artifact_digest is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", self.artifact_digest):
            raise InvalidEventError("artifact_digest must be a sha256 digest")
        for field_name in ("actor_role", "reason"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise InvalidEventError(f"{field_name} must be a string when provided")
        if self.checkpoint_kind is not None and self.checkpoint_kind not in CHECKPOINT_KINDS:
            raise InvalidEventError(f"unsupported checkpoint kind: {self.checkpoint_kind}")
        if self.decision is not None and self.decision not in DECISIONS:
            raise InvalidEventError(f"unsupported decision: {self.decision}")
        if self.event_type == "checkpoint_created" and (not self.checkpoint_id or not self.checkpoint_kind):
            raise InvalidEventError("checkpoint creation requires checkpoint_id and checkpoint_kind")
        if self.event_type == "checkpoint_expired" and not self.checkpoint_id:
            raise InvalidEventError("checkpoint expiration requires checkpoint_id")
        if self.event_type == "decision_recorded":
            if not all((self.checkpoint_id, self.decision, self.decision_id, self.actor_id, self.actor_role, self.authorization_ref)):
                raise InvalidEventError("decisions require checkpoint, decision, reviewer, and authorization fields")
        if self.event_type == "plan_submitted" and not all((self.plan_id, self.plan_revision, self.plan_artifact_id, self.plan_artifact_digest)):
            raise InvalidEventError("plan submission requires plan identity, revision, and artifact metadata")
        if self.event_type in {"admission_blocked", "analysis_blocked", "analysis_inconclusive", "cancel", "retry_requested", "return_to_analyze", "return_to_plan", "return_to_code", "return_to_execute"} and not self.reason:
            raise InvalidEventError(f"{self.event_type} requires a reason")
        if self.event_type == "return_to_plan" and not self.feedback_id:
            raise InvalidEventError("return_to_plan requires feedback_id")
        if self.event_type == "generation_started" and not self.generation_id:
            raise InvalidEventError("generation_started requires generation_id")
        if self.event_type == "generation_completed" and not all(
            (self.generation_id, self.artifact_id, self.artifact_digest)
        ):
            raise InvalidEventError("generation_completed requires generation and artifact identity")


@dataclass(frozen=True)
class TransitionResult:
    """Result of applying an event; duplicates are successful no-ops."""

    state: "RunState"
    duplicate: bool = False


@dataclass(frozen=True)
class RunState:
    """Serializable state projection for one ATG run."""

    run_id: str
    status: str = "requested"
    revision: int = 0
    updated_at: str = ""
    active_checkpoint_id: str | None = None
    active_checkpoint_expires_at: str | None = None
    checkpoint_return_status: str | None = None
    active_plan_id: str | None = None
    active_plan_revision: int | None = None
    active_plan_artifact_id: str | None = None
    active_plan_artifact_digest: str | None = None
    active_feedback_id: str | None = None
    active_generation_id: str | None = None
    generation_in_progress: bool = False
    completed_generation_id: str | None = None
    completed_generation_artifact_id: str | None = None
    completed_generation_artifact_digest: str | None = None
    superseded_checkpoint_ids: tuple[str, ...] = ()
    expired_checkpoint_ids: tuple[str, ...] = ()
    processed_event_ids: tuple[str, ...] = ()
    event_fingerprints: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)
    terminal_reason: str | None = None
    admission_blocked: bool = False

    _TERMINAL = frozenset({"completed", "blocked", "inconclusive", "stated_gap", "cancelled"})
    _CHECKPOINT_STATUSES = {
        "analysis_review": "analysis_review",
        "plan_review": "plan_review",
        "code_review": "code_review",
        "execution_review": "execution_review",
        "quality_review": "quality_review",
    }
    _CHECKPOINT_PREREQUISITES = {
        "analysis_review": {"admitted", "analyzing"},
        "plan_review": {"planning"},
        "code_review": {"planning", "coding"},
        "execution_review": {"coding", "executing"},
        "quality_review": {"executing", "quality_review"},
    }
    _CHECKPOINT_KIND_BY_STATUS = {value: key for key, value in _CHECKPOINT_STATUSES.items()}
    _APPROVED_NEXT = {
        "analysis_review": "planning",
        "plan_review": "coding",
        "code_review": "executing",
        "execution_review": "quality_review",
        "quality_review": "completed",
    }
    _ALLOWED = {
        "admit": {"requested": "admitted"},
        "admission_blocked": {"requested": "blocked"},
        "analysis_started": {"admitted": "analyzing"},
        "analysis_completed": {"analyzing": "admitted"},
        "analysis_blocked": {"analyzing": "blocked"},
        "analysis_inconclusive": {"analyzing": "inconclusive"},
        "planning_completed": {"planning": "planning"},
        "plan_submitted": {"planning": "planning"},
        "generation_started": {"coding": "coding"},
        "generation_completed": {"coding": "coding"},
        "execution_started": {"executing": "executing"},
        "execution_completed": {"executing": "executing"},
        "quality_passed": {"quality_review": "completed"},
        "quality_blocked": {"quality_review": "blocked"},
        "quality_inconclusive": {"quality_review": "inconclusive"},
        "quality_gap": {"quality_review": "stated_gap"},
        "retry_requested": {"blocked": "admitted", "inconclusive": "admitted"},
        "return_to_analyze": {"analyzing": "analyzing", "analysis_review": "analyzing", "planning": "analyzing", "plan_review": "analyzing", "coding": "analyzing", "code_review": "analyzing", "executing": "analyzing", "execution_review": "analyzing", "quality_review": "analyzing"},
        "return_to_plan": {"planning": "planning", "plan_review": "planning", "coding": "planning", "code_review": "planning", "executing": "planning", "execution_review": "planning", "quality_review": "planning"},
        "return_to_code": {"coding": "coding", "code_review": "coding", "executing": "coding", "execution_review": "coding", "quality_review": "coding"},
        "return_to_execute": {"executing": "executing", "execution_review": "executing", "quality_review": "executing"},
    }

    @classmethod
    def initial(cls, run_id: str) -> "RunState":
        """Create a new requested run projection."""
        validate_identifier(run_id, "run_id")
        return cls(run_id=run_id)

    def apply(self, event: RunEvent) -> TransitionResult:
        """Apply an event, treating exact retries as idempotent no-ops."""

        if event.run_id != self.run_id:
            raise InvalidTransitionError("event belongs to a different run")

        fingerprint = event_fingerprint(event)
        previous = self.event_fingerprints.get(event.event_id)
        if previous is not None:
            if previous != fingerprint:
                raise DuplicateEventError(f"event id {event.event_id!r} has a different payload")
            return TransitionResult(self, duplicate=True)

        if event.expected_revision != self.revision:
            raise StaleEventError(
                f"event expects revision {event.expected_revision}, current revision is {self.revision}"
            )
        if self.updated_at and _parse_timestamp(event.occurred_at) < _parse_timestamp(self.updated_at):
            raise InvalidTransitionError("event occurred_at cannot precede the current updated_at")
        retrying = event.event_type == "retry_requested" and self.status in {"blocked", "inconclusive"} and not self.admission_blocked
        if self.status in self._TERMINAL and not retrying:
            raise InvalidTransitionError(f"run is terminal: {self.status}")

        if event.event_type == "checkpoint_created":
            next_state = self._create_checkpoint(event)
        elif event.event_type == "decision_recorded":
            next_state = self._record_decision(event)
        elif event.event_type == "cancel":
            if not event.reason:
                raise InvalidTransitionError("cancellation requires a reason")
            next_state = self._replace(
                event,
                status="cancelled",
                terminal_reason=event.reason,
                active_checkpoint_id=None,
                active_checkpoint_expires_at=None,
                checkpoint_return_status=None,
            )
        elif event.event_type == "checkpoint_expired":
            next_state = self._expire_checkpoint(event)
        else:
            if (event.event_type == "retry_requested" or event.event_type.startswith("return_to_")) and not event.reason:
                raise InvalidTransitionError("retry or feedback return requires a reason")
            try:
                next_status = self._ALLOWED[event.event_type][self.status]
            except KeyError as exc:
                raise InvalidTransitionError(
                    f"cannot apply {event.event_type!r} while run is {self.status!r}"
                ) from exc
            if event.event_type == "generation_completed":
                if not self.generation_in_progress or event.generation_id != self.active_generation_id:
                    raise InvalidTransitionError("generation_completed must follow its generation_started event")
            if event.event_type == "plan_submitted" and self.active_plan_revision is not None and event.plan_revision <= self.active_plan_revision:
                raise InvalidTransitionError("plan revision must supersede the active plan revision")
            clear_checkpoint = event.event_type == "retry_requested" or event.event_type.startswith("return_to_") or next_status in self._TERMINAL
            next_state = self._replace(
                event,
                status=next_status,
                **(
                    {
                        "active_plan_id": event.plan_id,
                        "active_plan_revision": event.plan_revision,
                        "active_plan_artifact_id": event.plan_artifact_id,
                        "active_plan_artifact_digest": event.plan_artifact_digest,
                    }
                    if event.event_type == "plan_submitted" else {}
                ),
                generation_in_progress=(
                    True if event.event_type == "generation_started"
                    else False if event.event_type == "generation_completed"
                    else False if event.event_type == "plan_submitted" or event.event_type == "return_to_plan"
                    else self.generation_in_progress
                ),
                **(
                    {
                        "active_generation_id": event.generation_id,
                        "completed_generation_id": event.generation_id,
                        "completed_generation_artifact_id": event.artifact_id,
                        "completed_generation_artifact_digest": event.artifact_digest,
                    }
                    if event.event_type == "generation_completed" else
                    {"active_generation_id": event.generation_id}
                    if event.event_type == "generation_started" else {}
                    if event.event_type not in {"plan_submitted", "return_to_plan"} else
                    {
                        "active_generation_id": None,
                        "completed_generation_id": None,
                        "completed_generation_artifact_id": None,
                        "completed_generation_artifact_digest": None,
                    }
                ),
                terminal_reason=(
                    None
                    if event.event_type == "retry_requested"
                    else event.reason
                    if event.event_type in {"admission_blocked", "analysis_blocked", "analysis_inconclusive"}
                    else self.terminal_reason
                ),
                **(
                    {
                        "active_checkpoint_id": None,
                        "active_checkpoint_expires_at": None,
                        "checkpoint_return_status": None,
                    }
                    if clear_checkpoint
                    else {}
                ),
                **(
                    {"active_feedback_id": event.feedback_id}
                    if event.event_type == "return_to_plan" else {}
                ),
            )

        fingerprints = dict(self.event_fingerprints)
        fingerprints[event.event_id] = fingerprint
        return TransitionResult(
            replace(
                next_state,
                event_fingerprints=fingerprints,
                admission_blocked=self.admission_blocked or event.event_type == "admission_blocked",
            ),
            duplicate=False,
        )

    def _create_checkpoint(self, event: RunEvent) -> "RunState":
        """Create or supersede a lifecycle checkpoint."""
        if not event.checkpoint_id or event.checkpoint_kind not in self._CHECKPOINT_STATUSES:
            raise InvalidTransitionError("checkpoint creation requires id and supported kind")
        if event.checkpoint_id == self.active_checkpoint_id:
            raise InvalidTransitionError("checkpoint is already active")
        if self.active_checkpoint_id is not None:
            if event.supersedes != self.active_checkpoint_id:
                raise SupersessionError("replacement must supersede the active checkpoint")
            if self._CHECKPOINT_KIND_BY_STATUS.get(self.status) != event.checkpoint_kind:
                raise InvalidTransitionError("replacement checkpoint must keep the same review kind")
            replacement = True
        else:
            if event.supersedes is not None:
                raise SupersessionError("there is no active checkpoint to supersede")
            replacement = False
        if not replacement and self.status not in self._CHECKPOINT_PREREQUISITES[event.checkpoint_kind]:
            raise InvalidTransitionError(
                f"{event.checkpoint_kind} requires status "
                f"in {sorted(self._CHECKPOINT_PREREQUISITES[event.checkpoint_kind])!r}"
            )
        if event.checkpoint_kind == "plan_review" and self.active_plan_id is None:
            raise InvalidTransitionError("plan_review requires a submitted plan artifact")
        if event.checkpoint_kind == "code_review" and not all(
            (self.completed_generation_id, self.completed_generation_artifact_id, self.completed_generation_artifact_digest)
        ):
            raise InvalidTransitionError("code_review requires a completed generation and artifact identity")
        if event.checkpoint_id in self.superseded_checkpoint_ids:
            raise InvalidTransitionError("checkpoint id was already superseded")
        if self.active_checkpoint_id is not None:
            superseded = self.superseded_checkpoint_ids + (self.active_checkpoint_id,)
        else:
            superseded = self.superseded_checkpoint_ids
        return self._replace(
            event,
            status=self._CHECKPOINT_STATUSES[event.checkpoint_kind],
            active_checkpoint_id=event.checkpoint_id,
            active_checkpoint_expires_at=event.expires_at,
            checkpoint_return_status=self.checkpoint_return_status if replacement else self.status,
            superseded_checkpoint_ids=superseded,
        )

    def _expire_checkpoint(self, event: RunEvent) -> "RunState":
        """Expire the active checkpoint and return to its prior stage."""
        if self.active_checkpoint_id is None or event.checkpoint_id != self.active_checkpoint_id:
            raise SupersessionError("expiration does not target the active checkpoint")
        if self.active_checkpoint_expires_at and _parse_timestamp(event.occurred_at) < _parse_timestamp(
            self.active_checkpoint_expires_at
        ):
            raise InvalidTransitionError("checkpoint cannot expire before its deadline")
        return_status = self.checkpoint_return_status
        if return_status is None:
            return_status = {
                "analysis_review": "analyzing",
                "plan_review": "planning",
                "code_review": "coding",
                "execution_review": "executing",
                "quality_review": "quality_review",
            }.get(self.status)
        if return_status is None:
            raise InvalidTransitionError("active checkpoint has an invalid review status")
        return self._replace(
            event,
            status=return_status,
            active_checkpoint_id=None,
            active_checkpoint_expires_at=None,
            checkpoint_return_status=None,
            expired_checkpoint_ids=self.expired_checkpoint_ids + (self.active_checkpoint_id,),
        )

    def _record_decision(self, event: RunEvent) -> "RunState":
        """Apply an authorized decision to the active checkpoint."""
        if self.active_checkpoint_id is None or event.checkpoint_id != self.active_checkpoint_id:
            raise SupersessionError("decision does not target the active checkpoint")
        if event.decision not in {"approved", "rejected", "blocked", "stated_gap"}:
            raise InvalidTransitionError("decision is required")
        if (
            not event.actor_id
            or not event.actor_role
            or not event.authorization_ref
            or not event.decision_id
        ):
            raise InvalidTransitionError(
                "decisions require actor_id, actor_role, authorization_ref, and decision_id"
            )
        if event.decision == "approved":
            next_status = self._APPROVED_NEXT.get(self.status)
            if next_status is None:
                raise InvalidTransitionError("active checkpoint has an invalid review status")
            reason = None
        else:
            next_status = "blocked" if event.decision == "rejected" else event.decision
            reason = event.reason
        return self._replace(
            event,
            status=next_status,
            active_checkpoint_id=None,
            active_checkpoint_expires_at=None,
            checkpoint_return_status=None,
            terminal_reason=reason,
        )

    def to_document(self) -> dict:
        """Serialize the durable state projection for storage or evidence."""

        if not self.updated_at:
            raise ValueError("updated_at is required to serialize run state")
        document = {
            "contract": "atg.run_state",
            "version": "1.0",
            "run_id": self.run_id,
            "status": self.status,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "superseded_checkpoint_ids": list(self.superseded_checkpoint_ids),
            "processed_event_ids": list(self.processed_event_ids),
            "event_fingerprints": dict(self.event_fingerprints),
            "admission_blocked": self.admission_blocked,
        }
        optional = {
            "active_checkpoint_id": self.active_checkpoint_id,
            "active_checkpoint_expires_at": self.active_checkpoint_expires_at,
            "checkpoint_return_status": self.checkpoint_return_status,
            "terminal_reason": self.terminal_reason,
            "active_plan_id": self.active_plan_id,
            "active_plan_revision": self.active_plan_revision,
            "active_plan_artifact_id": self.active_plan_artifact_id,
            "active_plan_artifact_digest": self.active_plan_artifact_digest,
            "active_feedback_id": self.active_feedback_id,
            "active_generation_id": self.active_generation_id,
            "generation_in_progress": self.generation_in_progress,
            "completed_generation_id": self.completed_generation_id,
            "completed_generation_artifact_id": self.completed_generation_artifact_id,
            "completed_generation_artifact_digest": self.completed_generation_artifact_digest,
            "expired_checkpoint_ids": list(self.expired_checkpoint_ids),
        }
        document.update({key: value for key, value in optional.items() if value is not None})
        return document

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> "RunState":
        """Restore state without dropping replay/idempotency metadata."""

        if not isinstance(document, Mapping):
            raise ValueError("run-state document must be an object")
        if document.get("contract") != "atg.run_state" or document.get("version") != "1.0":
            raise ValueError("unsupported run-state document")
        run_id = document.get("run_id")
        status = document.get("status")
        revision = document.get("revision")
        updated_at = document.get("updated_at")
        validate_identifier(run_id, "run_id")
        if not isinstance(status, str) or status not in STATE_STATUSES:
            raise ValueError("status is invalid")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("revision must be a non-negative integer")
        admission_blocked = document.get("admission_blocked", False)
        if not isinstance(admission_blocked, bool):
            raise ValueError("admission_blocked must be a boolean")
        if not isinstance(updated_at, str):
            raise ValueError("updated_at must be a timestamp string")
        _parse_timestamp(updated_at)
        for field_name in ("superseded_checkpoint_ids", "expired_checkpoint_ids", "processed_event_ids"):
            values = document.get(field_name, [])
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError(f"{field_name} must be an array of strings")
            for value in values:
                validate_identifier(value, field_name)
        fingerprints = document.get("event_fingerprints", {})
        if not isinstance(fingerprints, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in fingerprints.items()
        ):
            raise ValueError("event_fingerprints must be an object of strings")
        for field_name in ("active_checkpoint_id", "active_checkpoint_expires_at", "checkpoint_return_status", "terminal_reason", "active_plan_id", "active_plan_artifact_id", "active_plan_artifact_digest", "active_feedback_id", "active_generation_id", "completed_generation_id", "completed_generation_artifact_id", "completed_generation_artifact_digest"):
            value = document.get(field_name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field_name} must be a string when provided")
        if document.get("active_checkpoint_id") is not None:
            validate_identifier(document["active_checkpoint_id"], "active_checkpoint_id")
        for field_name in ("active_plan_id", "active_plan_artifact_id"):
            if document.get(field_name) is not None:
                validate_identifier(document[field_name], field_name)
        if document.get("active_plan_revision") is not None and (not isinstance(document["active_plan_revision"], int) or document["active_plan_revision"] < 1):
            raise ValueError("active_plan_revision must be a positive integer")
        if document.get("active_plan_artifact_digest") is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", document["active_plan_artifact_digest"]):
            raise ValueError("active_plan_artifact_digest must be a sha256 digest")
        if document.get("completed_generation_artifact_digest") is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", document["completed_generation_artifact_digest"]):
            raise ValueError("completed_generation_artifact_digest must be a sha256 digest")
        if not isinstance(document.get("generation_in_progress", False), bool):
            raise ValueError("generation_in_progress must be a boolean")
        for key in fingerprints:
            validate_identifier(key, "event_fingerprints key")
        if any(not re.fullmatch(r"[A-Fa-f0-9]{64}", value) for value in fingerprints.values()):
            raise ValueError("event_fingerprints values must be SHA-256 hex digests")
        if document.get("active_checkpoint_expires_at") is not None:
            _parse_timestamp(document["active_checkpoint_expires_at"])
        return cls(
            run_id=run_id,
            status=status,
            revision=revision,
            updated_at=updated_at,
            active_checkpoint_id=document.get("active_checkpoint_id") or None,
            active_checkpoint_expires_at=document.get("active_checkpoint_expires_at") or None,
            checkpoint_return_status=document.get("checkpoint_return_status") or None,
            superseded_checkpoint_ids=tuple(document.get("superseded_checkpoint_ids", [])),
            processed_event_ids=tuple(document.get("processed_event_ids", [])),
            event_fingerprints=fingerprints,
            terminal_reason=document.get("terminal_reason") or None,
            active_plan_id=document.get("active_plan_id") or None,
            active_plan_revision=document.get("active_plan_revision"),
            active_plan_artifact_id=document.get("active_plan_artifact_id") or None,
            active_plan_artifact_digest=document.get("active_plan_artifact_digest") or None,
            active_feedback_id=document.get("active_feedback_id") or None,
            active_generation_id=document.get("active_generation_id") or None,
            generation_in_progress=document.get("generation_in_progress", False),
            completed_generation_id=document.get("completed_generation_id") or None,
            completed_generation_artifact_id=document.get("completed_generation_artifact_id") or None,
            completed_generation_artifact_digest=document.get("completed_generation_artifact_digest") or None,
            admission_blocked=admission_blocked,
            expired_checkpoint_ids=tuple(document.get("expired_checkpoint_ids", [])),
        )

    def _replace(self, event: RunEvent, **changes: object) -> "RunState":
        """Return the next immutable state projection for an accepted event."""
        return RunState(
            run_id=self.run_id,
            status=str(changes.get("status", self.status)),
            revision=self.revision + 1,
            updated_at=event.occurred_at,
            active_checkpoint_id=changes.get("active_checkpoint_id", self.active_checkpoint_id),
            active_checkpoint_expires_at=changes.get(
                "active_checkpoint_expires_at", self.active_checkpoint_expires_at
            ),
            checkpoint_return_status=changes.get("checkpoint_return_status", self.checkpoint_return_status),
            active_plan_id=changes.get("active_plan_id", self.active_plan_id),
            active_plan_revision=changes.get("active_plan_revision", self.active_plan_revision),
            active_plan_artifact_id=changes.get("active_plan_artifact_id", self.active_plan_artifact_id),
            active_plan_artifact_digest=changes.get("active_plan_artifact_digest", self.active_plan_artifact_digest),
            active_feedback_id=changes.get("active_feedback_id", self.active_feedback_id),
            active_generation_id=changes.get("active_generation_id", self.active_generation_id),
            generation_in_progress=changes.get("generation_in_progress", self.generation_in_progress),
            completed_generation_id=changes.get("completed_generation_id", self.completed_generation_id),
            completed_generation_artifact_id=changes.get("completed_generation_artifact_id", self.completed_generation_artifact_id),
            completed_generation_artifact_digest=changes.get("completed_generation_artifact_digest", self.completed_generation_artifact_digest),
            superseded_checkpoint_ids=changes.get(
                "superseded_checkpoint_ids", self.superseded_checkpoint_ids
            ),
            expired_checkpoint_ids=changes.get("expired_checkpoint_ids", self.expired_checkpoint_ids),
            processed_event_ids=self.processed_event_ids + (event.event_id,),
            terminal_reason=changes.get("terminal_reason", self.terminal_reason),
            admission_blocked=self.admission_blocked or event.event_type == "admission_blocked",
        )


def event_fingerprint(event: RunEvent) -> str:
    """Return the SHA-256 fingerprint of an event payload."""
    payload = json.dumps(event.__dict__, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    """Parse a timezone-aware RFC 3339 timestamp."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidTransitionError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise InvalidTransitionError(f"timestamp must include a UTC offset: {value!r}")
    return parsed


def _validate_event_timestamp(value: object, field: str) -> None:
    """Validate an event timestamp field."""
    try:
        _parse_timestamp(value)  # type: ignore[arg-type]
    except InvalidEventError:
        raise
    except (InvalidTransitionError, TypeError, ValueError) as exc:
        raise InvalidEventError(f"{field} must be a timezone-aware RFC 3339 timestamp") from exc


def validate_timestamp(value: object, field: str = "timestamp") -> None:
    """Validate a timestamp at service ingress without constructing an event."""

    _validate_event_timestamp(value, field)


def validate_identifier(value: object, field: str = "identifier") -> None:
    """Validate a v1 identifier used in event and run-state contracts."""

    if not isinstance(value, str) or not value or len(value) > 256 or ID_PATTERN.fullmatch(value) is None:
        raise InvalidEventError(f"{field} must be a valid v1 identifier")
