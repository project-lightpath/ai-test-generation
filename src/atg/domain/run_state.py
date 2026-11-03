"""Durable run-state transitions for the ATG lifecycle.

The state machine is deliberately independent of persistence, HTTP, Tekton, or
any model/provider SDK. A caller can persist the returned state and event
fingerprint in the same transaction as its workflow journal.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from typing import Mapping


class RunStateError(ValueError):
    """Base class for invalid run-state operations."""


class DuplicateEventError(RunStateError):
    """Raised when an event id is reused with a different payload."""


class StaleEventError(RunStateError):
    """Raised when an event was based on an old state revision."""


class InvalidTransitionError(RunStateError):
    """Raised when an event is not valid for the current status."""


class SupersessionError(RunStateError):
    """Raised when a checkpoint replacement does not name the active checkpoint."""


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

    def __post_init__(self) -> None:
        if not self.event_id or not self.run_id or not self.event_type:
            raise ValueError("event_id, run_id, and event_type are required")
        if self.expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")


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
    superseded_checkpoint_ids: tuple[str, ...] = ()
    processed_event_ids: tuple[str, ...] = ()
    event_fingerprints: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)
    terminal_reason: str | None = None

    _TERMINAL = frozenset({"completed", "blocked", "inconclusive", "stated_gap", "cancelled"})
    _CHECKPOINT_STATUSES = {
        "plan_review": "plan_review",
        "code_review": "code_review",
        "execution_review": "execution_review",
        "quality_review": "quality_review",
    }
    _CHECKPOINT_PREREQUISITES = {
        "plan_review": "admitted",
        "code_review": "planning",
        "execution_review": "coding",
        "quality_review": "executing",
    }
    _CHECKPOINT_KIND_BY_STATUS = {value: key for key, value in _CHECKPOINT_STATUSES.items()}
    _APPROVED_NEXT = {
        "plan_review": "planning",
        "code_review": "coding",
        "execution_review": "executing",
        "quality_review": "completed",
    }
    _ALLOWED = {
        "admit": {"requested": "admitted"},
        "analysis_started": {"admitted": "analyzing"},
        "analysis_completed": {"analyzing": "admitted"},
        "planning_completed": {"planning": "planning"},
        "generation_started": {"coding": "coding"},
        "generation_completed": {"coding": "coding"},
        "execution_started": {"executing": "executing"},
        "execution_completed": {"executing": "executing"},
        "quality_passed": {"quality_review": "completed"},
        "quality_blocked": {"quality_review": "blocked"},
        "quality_inconclusive": {"quality_review": "inconclusive"},
        "quality_gap": {"quality_review": "stated_gap"},
        "retry_requested": {"blocked": "admitted", "inconclusive": "admitted"},
    }

    @classmethod
    def initial(cls, run_id: str) -> "RunState":
        if not run_id:
            raise ValueError("run_id is required")
        return cls(run_id=run_id)

    def apply(self, event: RunEvent) -> TransitionResult:
        """Apply an event, treating exact retries as idempotent no-ops."""

        if event.run_id != self.run_id:
            raise InvalidTransitionError("event belongs to a different run")

        fingerprint = _fingerprint(event)
        previous = self.event_fingerprints.get(event.event_id)
        if previous is not None:
            if previous != fingerprint:
                raise DuplicateEventError(f"event id {event.event_id!r} has a different payload")
            return TransitionResult(self, duplicate=True)

        if event.expected_revision != self.revision:
            raise StaleEventError(
                f"event expects revision {event.expected_revision}, current revision is {self.revision}"
            )
        retrying = event.event_type == "retry_requested" and self.status in {"blocked", "inconclusive"}
        if self.status in self._TERMINAL and not retrying:
            raise InvalidTransitionError(f"run is terminal: {self.status}")

        if event.event_type == "checkpoint_created":
            next_state = self._create_checkpoint(event)
        elif event.event_type == "decision_recorded":
            next_state = self._record_decision(event)
        elif event.event_type == "cancel":
            if not event.reason:
                raise InvalidTransitionError("cancellation requires a reason")
            next_state = self._replace(event, status="cancelled", terminal_reason=event.reason)
        elif event.event_type == "checkpoint_expired":
            next_state = self._expire_checkpoint(event)
        else:
            if event.event_type == "retry_requested" and not event.reason:
                raise InvalidTransitionError("retry requires a reason")
            try:
                next_status = self._ALLOWED[event.event_type][self.status]
            except KeyError as exc:
                raise InvalidTransitionError(
                    f"cannot apply {event.event_type!r} while run is {self.status!r}"
                ) from exc
            next_state = self._replace(
                event,
                status=next_status,
                terminal_reason=None if event.event_type == "retry_requested" else self.terminal_reason,
            )

        fingerprints = dict(self.event_fingerprints)
        fingerprints[event.event_id] = fingerprint
        return TransitionResult(replace(next_state, event_fingerprints=fingerprints), duplicate=False)

    def _create_checkpoint(self, event: RunEvent) -> "RunState":
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
        if not replacement and self.status != self._CHECKPOINT_PREREQUISITES[event.checkpoint_kind]:
            raise InvalidTransitionError(
                f"{event.checkpoint_kind} requires status "
                f"{self._CHECKPOINT_PREREQUISITES[event.checkpoint_kind]!r}"
            )
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
            superseded_checkpoint_ids=superseded,
        )

    def _expire_checkpoint(self, event: RunEvent) -> "RunState":
        if self.active_checkpoint_id is None or event.checkpoint_id != self.active_checkpoint_id:
            raise SupersessionError("expiration does not target the active checkpoint")
        if self.active_checkpoint_expires_at and event.occurred_at < self.active_checkpoint_expires_at:
            raise InvalidTransitionError("checkpoint cannot expire before its deadline")
        return self._replace(
            event,
            status={
                "plan_review": "admitted",
                "code_review": "planning",
                "execution_review": "coding",
                "quality_review": "executing",
            }[self.status],
            active_checkpoint_id=None,
            active_checkpoint_expires_at=None,
        )

    def _record_decision(self, event: RunEvent) -> "RunState":
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
            next_status = self._APPROVED_NEXT[self.status]
            reason = None
        else:
            next_status = "blocked" if event.decision == "rejected" else event.decision
            reason = event.reason
        return self._replace(
            event,
            status=next_status,
            active_checkpoint_id=None,
            active_checkpoint_expires_at=None,
            terminal_reason=reason,
        )

    def to_document(self) -> dict:
        """Serialize the durable state projection for storage or evidence."""

        if not self.updated_at:
            raise ValueError("updated_at is required to serialize run state")
        return {
            "contract": "atg.run_state",
            "version": "1.0",
            "run_id": self.run_id,
            "status": self.status,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "active_checkpoint_id": self.active_checkpoint_id,
            "active_checkpoint_expires_at": self.active_checkpoint_expires_at,
            "superseded_checkpoint_ids": list(self.superseded_checkpoint_ids),
            "processed_event_ids": list(self.processed_event_ids),
            "event_fingerprints": dict(self.event_fingerprints),
            "terminal_reason": self.terminal_reason,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> "RunState":
        """Restore state without dropping replay/idempotency metadata."""

        if document.get("contract") != "atg.run_state" or document.get("version") != "1.0":
            raise ValueError("unsupported run-state document")
        return cls(
            run_id=str(document["run_id"]),
            status=str(document["status"]),
            revision=int(document["revision"]),
            updated_at=str(document["updated_at"]),
            active_checkpoint_id=document.get("active_checkpoint_id") or None,
            active_checkpoint_expires_at=document.get("active_checkpoint_expires_at") or None,
            superseded_checkpoint_ids=tuple(document.get("superseded_checkpoint_ids", [])),
            processed_event_ids=tuple(document.get("processed_event_ids", [])),
            event_fingerprints=dict(document.get("event_fingerprints", {})),
            terminal_reason=document.get("terminal_reason") or None,
        )

    def _replace(self, event: RunEvent, **changes: object) -> "RunState":
        return RunState(
            run_id=self.run_id,
            status=str(changes.get("status", self.status)),
            revision=self.revision + 1,
            updated_at=event.occurred_at,
            active_checkpoint_id=changes.get("active_checkpoint_id", self.active_checkpoint_id),
            active_checkpoint_expires_at=changes.get(
                "active_checkpoint_expires_at", self.active_checkpoint_expires_at
            ),
            superseded_checkpoint_ids=changes.get(
                "superseded_checkpoint_ids", self.superseded_checkpoint_ids
            ),
            processed_event_ids=self.processed_event_ids + (event.event_id,),
            terminal_reason=changes.get("terminal_reason", self.terminal_reason),
        )


def _fingerprint(event: RunEvent) -> str:
    payload = json.dumps(event.__dict__, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
