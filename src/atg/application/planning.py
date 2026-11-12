"""Deterministic Plan-stage orchestration and durable review artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import threading
from typing import Mapping

from atg.domain.schema_validation import ContractValidationError, validate_contract_document
from atg.domain.run_state import InvalidTransitionError, RunEvent, StaleEventError, validate_identifier, validate_timestamp


class PlanningConflictError(ValueError):
    """Raised when a plan identity or source analysis is reused inconsistently."""


@dataclass(frozen=True)
class PlanningRequest:
    """Request to derive one plan revision from an approved Analyze output."""

    plan_id: str
    analysis_id: str
    run_id: str
    expected_revision: int
    occurred_at: str

    def __post_init__(self) -> None:
        """Validate plan, analysis, run, revision, and timestamp identities."""
        for field in ("plan_id", "analysis_id", "run_id"):
            validate_identifier(getattr(self, field), field)
        if not isinstance(self.expected_revision, int) or isinstance(self.expected_revision, bool) or self.expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        validate_timestamp(self.occurred_at, "occurred_at")

    def digest(self) -> str:
        """Return the deterministic planning request digest."""
        payload = {"plan_id": self.plan_id, "analysis_id": self.analysis_id, "run_id": self.run_id, "expected_revision": self.expected_revision}
        return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class PlanRecord:
    """Durable plan manifest, checkpoint, provenance, and artifact bundle."""

    request_digest: str
    manifest: Mapping[str, object]
    checkpoint: Mapping[str, object]
    provenance: Mapping[str, object]
    artifacts: tuple[Mapping[str, object], ...]
    duplicate: bool = False

    def to_storage_document(self) -> dict[str, object]:
        """Serialize the complete plan bundle."""
        return {
            "request_digest": self.request_digest,
            "manifest": dict(self.manifest),
            "checkpoint": dict(self.checkpoint),
            "provenance": dict(self.provenance),
            "artifacts": [dict(artifact) for artifact in self.artifacts],
        }

    @classmethod
    def from_storage_document(cls, document: Mapping[str, object], *, duplicate: bool = False) -> "PlanRecord":
        """Restore and validate a persisted plan bundle."""
        record = cls(
            request_digest=str(document["request_digest"]),
            manifest=dict(document["manifest"]),  # type: ignore[arg-type]
            checkpoint=dict(document["checkpoint"]),  # type: ignore[arg-type]
            provenance=dict(document["provenance"]),  # type: ignore[arg-type]
            artifacts=tuple(dict(item) for item in document.get("artifacts", [])),  # type: ignore[arg-type]
            duplicate=duplicate,
        )
        validate_plan_record(record)
        return record


class PlanningService:
    """Build and persist deterministic plans from Analyze output."""

    def __init__(self, store) -> None:
        """Create a planning service from a durable planning store."""
        self._store = store
        self._lock = threading.RLock()

    def plan(self, request: PlanningRequest) -> PlanRecord:
        """Create or replay an artifact-bound plan revision."""
        with self._lock:
            existing = self._store.get_plan(request.plan_id)
            if existing is not None:
                if existing.request_digest != request.digest():
                    raise PlanningConflictError("plan identity is bound to different inputs")
                return PlanRecord(existing.request_digest, existing.manifest, existing.checkpoint, existing.provenance, existing.artifacts, True)
            state = self._store.get_run(request.run_id)
            if state.status != "planning":
                raise InvalidTransitionError(f"planning requires planning state, got {state.status}")
            if state.revision != request.expected_revision:
                raise StaleEventError(f"event expects revision {request.expected_revision}, current revision is {state.revision}")
            analysis = self._store.get_analysis(request.analysis_id)
            if analysis is None or analysis.manifest.get("run_id") != request.run_id:
                raise PlanningConflictError("plan analysis does not belong to the run")
            if analysis.checkpoint is None:
                raise PlanningConflictError("plan requires a reviewable analysis")
            objectives = analysis.objectives
            if not objectives:
                raise PlanningConflictError("plan requires at least one objective document")
            record = _build_plan(request, analysis)
            submission = RunEvent(
                event_id=f"{request.plan_id}-submitted",
                run_id=request.run_id,
                event_type="plan_submitted",
                expected_revision=request.expected_revision,
                occurred_at=request.occurred_at,
                plan_id=request.plan_id,
                plan_revision=1,
                plan_artifact_id=f"artifact-{request.plan_id}",
                plan_artifact_digest=record.checkpoint["reviewed_artifact"]["digest"],
            )
            checkpoint = RunEvent(
                event_id=f"{request.plan_id}-review-created",
                run_id=request.run_id,
                event_type="checkpoint_created",
                expected_revision=request.expected_revision + 1,
                occurred_at=request.occurred_at,
                checkpoint_id=f"checkpoint-{request.plan_id}",
                checkpoint_kind="plan_review",
            )
            return self._store.record_plan(record, submission, checkpoint)


def _build_plan(request: PlanningRequest, analysis) -> PlanRecord:
    """Build a minimal deterministic plan with exhaustive objective coverage."""
    analysis_artifact = next(
        artifact["artifact"] for artifact in analysis.artifacts
        if artifact["artifact"].get("artifact_id") == f"artifact-{analysis.manifest['analysis_id']}"
    )
    objective_refs = [str(objective["objective_id"]) for objective in analysis.objectives]
    steps = [
        {"step_id": f"step-{objective_id}", "stage": "plan", "action": f"Define validation strategy for {objective_id}.", "expected_evidence": list(next(objective for objective in analysis.objectives if objective["objective_id"] == objective_id).get("evidence_refs", []))}
        for objective_id in objective_refs
    ]
    manifest = {
        "contract": "atg.plan", "version": "1.0", "plan_id": request.plan_id, "run_id": request.run_id,
        "analysis_id": analysis.manifest["analysis_id"], "analysis_artifact": analysis_artifact,
        "analysis_revision": analysis.manifest["revision"], "subject": analysis.manifest["subject"], "plan_revision": 1,
        "objective_refs": objective_refs, "steps": steps, "status": "draft", "created_at": request.occurred_at,
        "provenance_ref": f"provenance-{request.plan_id}", "artifact_refs": [f"artifact-{request.plan_id}"],
        "assumptions": list(analysis.manifest.get("assumptions", [])), "gaps": list(analysis.manifest.get("gaps", [])),
        "coverage_matrix": [{"objective_ref": objective_id, "disposition": "planned"} for objective_id in objective_refs],
    }
    artifact = _artifact(f"artifact-{request.plan_id}", "application/vnd.atg.plan+json", manifest)
    provenance = {
        "contract": "atg.provenance", "version": "1.0", "provenance_id": f"provenance-{request.plan_id}", "run_id": request.run_id,
        "recorded_at": request.occurred_at,
        "identities": [manifest["subject"]], "artifacts": [artifact["artifact"]], "contract_versions": {"atg.plan": "1.0"}, "limitations": [],
    }
    checkpoint = {
        "contract": "atg.checkpoint", "version": "1.0", "checkpoint_id": f"checkpoint-{request.plan_id}", "run_id": request.run_id,
        "kind": "plan_review", "revision": 1, "state": "pending", "created_at": request.occurred_at, "reviewed_artifact": artifact["artifact"],
    }
    record = PlanRecord(request.digest(), manifest, checkpoint, provenance, (artifact,))
    validate_plan_record(record)
    return record


def _artifact(artifact_id: str, media_type: str, content: Mapping[str, object]) -> dict[str, object]:
    """Create content-addressed plan artifact metadata."""
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return {"artifact": {"artifact_id": artifact_id, "media_type": media_type, "digest": "sha256:" + hashlib.sha256(encoded).hexdigest(), "size_bytes": len(encoded)}, "content": content}


def validate_plan_record(record: PlanRecord) -> None:
    """Validate plan, checkpoint, provenance, and artifact identity closure."""
    validate_contract_document(record.manifest, "plan")
    validate_contract_document(record.checkpoint, "checkpoint")
    validate_contract_document(record.provenance, "provenance")
    artifacts = {artifact["artifact"]["artifact_id"]: artifact["artifact"] for artifact in record.artifacts}
    for stored in record.artifacts:
        metadata = stored["artifact"]
        encoded = json.dumps(stored["content"], sort_keys=True, separators=(",", ":")).encode()
        if metadata["digest"] != "sha256:" + hashlib.sha256(encoded).hexdigest() or metadata["size_bytes"] != len(encoded):
            raise ContractValidationError("plan artifact metadata mismatch")
    if record.manifest["artifact_refs"] != list(artifacts):
        raise ContractValidationError("plan artifact references are not closed")
    if record.checkpoint["reviewed_artifact"] != artifacts[f"artifact-{record.manifest['plan_id']}"]:
        raise ContractValidationError("plan checkpoint artifact mismatch")
