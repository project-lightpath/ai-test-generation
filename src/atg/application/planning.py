"""Deterministic Plan-stage orchestration and durable review artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import threading
from typing import Mapping

from atg.domain.schema_validation import ContractValidationError, validate_contract_document
from atg.domain.run_state import InvalidTransitionError, RunEvent, StaleEventError, validate_identifier, validate_timestamp
from atg.application.feedback import FeedbackConflictError, FeedbackRequest


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
            checkpoint = self._store.get_checkpoint(analysis.checkpoint["checkpoint_id"])
            if checkpoint is None or checkpoint.get("state") != "approved" or checkpoint.get("decision", {}).get("value") != "approved":
                raise PlanningConflictError("plan requires an approved analysis checkpoint")
            analysis_artifact_id = f"artifact-{analysis.manifest['analysis_id']}"
            reviewed = analysis.checkpoint.get("reviewed_artifact", {})
            source_artifact = next((item["artifact"] for item in analysis.artifacts if item["artifact"].get("artifact_id") == analysis_artifact_id), None)
            if reviewed != source_artifact:
                raise PlanningConflictError("plan requires the exact approved analysis artifact")
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

    def revise_with_feedback(self, feedback: FeedbackRequest) -> PlanRecord:
        """Create and persist a superseding plan revision from feedback."""
        existing_feedback = self._store.get_feedback(feedback.feedback_id)
        if existing_feedback is not None:
            expected_feedback = feedback.to_document()
            existing_identity = dict(existing_feedback)
            existing_identity["resolution"] = expected_feedback["resolution"]
            if existing_identity != expected_feedback:
                raise FeedbackConflictError("feedback identity is bound to different inputs")
            replay = self._store.get_plan(f"{feedback.plan_id}-r{feedback.plan_revision + 1}")
            if replay is not None:
                return PlanRecord(replay.request_digest, replay.manifest, replay.checkpoint, replay.provenance, replay.artifacts, True)
            raise FeedbackConflictError("feedback was persisted without its revision")
        old = self._store.get_plan(feedback.plan_id)
        if old is None or old.manifest.get("plan_revision") != feedback.plan_revision:
            raise FeedbackConflictError("feedback plan revision does not exist")
        if old.manifest.get("status") != "approved":
            raise FeedbackConflictError("feedback requires an approved plan")
        if old.manifest.get("run_id") != feedback.run_id:
            raise FeedbackConflictError("feedback run does not match the plan")
        if old.checkpoint.get("checkpoint_id") != feedback.checkpoint_id:
            raise FeedbackConflictError("feedback checkpoint does not match the plan")
        checkpoint = self._store.get_checkpoint(feedback.checkpoint_id)
        if checkpoint is None or checkpoint.get("state") != "approved":
            raise FeedbackConflictError("feedback requires an approved plan checkpoint")
        approved_artifact = next((artifact["artifact"] for artifact in old.artifacts if artifact["artifact"].get("artifact_id") == f"artifact-{feedback.plan_id}"), None)
        if approved_artifact is None:
            raise FeedbackConflictError("approved plan artifact is missing")
        if approved_artifact.get("digest") != feedback.plan_artifact_digest:
            raise FeedbackConflictError("feedback plan digest does not match the approved plan")
        new_id = f"{feedback.plan_id}-r{feedback.plan_revision + 1}"
        manifest = dict(old.manifest)
        manifest.pop("approved_by", None)
        manifest.update({"plan_id": new_id, "plan_revision": feedback.plan_revision + 1, "status": "draft", "supersedes": feedback.plan_id, "feedback_refs": [feedback.feedback_id], "created_at": feedback.created_at})
        artifact = _artifact(f"artifact-{new_id}", "application/vnd.atg.plan+json", manifest)
        manifest["artifact_refs"] = [artifact["artifact"]["artifact_id"]]
        artifact = _artifact(f"artifact-{new_id}", "application/vnd.atg.plan+json", manifest)
        provenance = dict(old.provenance)
        provenance.update({"provenance_id": f"provenance-{new_id}", "recorded_at": feedback.created_at, "artifacts": [artifact["artifact"]]})
        checkpoint = {"contract": "atg.checkpoint", "version": "1.0", "checkpoint_id": f"checkpoint-{new_id}", "run_id": feedback.run_id, "kind": "plan_review", "revision": feedback.plan_revision + 1, "state": "pending", "created_at": feedback.created_at, "supersedes": feedback.checkpoint_id, "reviewed_artifact": artifact["artifact"]}
        record = PlanRecord(feedback.digest(), manifest, checkpoint, provenance, (artifact,))
        validate_plan_record(record)
        state = self._store.get_run(feedback.run_id)
        return_event = RunEvent(
            f"{feedback.feedback_id}-return", feedback.run_id, "return_to_plan", state.revision, feedback.created_at,
            reason=feedback.message, feedback_id=feedback.feedback_id,
        )
        submission = RunEvent(
            f"{new_id}-submitted", feedback.run_id, "plan_submitted", state.revision + 1, feedback.created_at,
            plan_id=new_id, plan_revision=feedback.plan_revision + 1,
            plan_artifact_id=artifact["artifact"]["artifact_id"], plan_artifact_digest=artifact["artifact"]["digest"],
        )
        checkpoint_event = RunEvent(
            f"{new_id}-review-created", feedback.run_id, "checkpoint_created", state.revision + 2, feedback.created_at,
            checkpoint_id=checkpoint["checkpoint_id"], checkpoint_kind="plan_review",
        )
        return self._store.record_feedback_revision(feedback, record, return_event, submission, checkpoint_event)


def _build_plan(request: PlanningRequest, analysis) -> PlanRecord:
    """Build a minimal deterministic plan with exhaustive objective coverage."""
    analysis_artifact = next(
        artifact["artifact"] for artifact in analysis.artifacts
        if artifact["artifact"].get("artifact_id") == f"artifact-{analysis.manifest['analysis_id']}"
    )
    objective_refs = [str(objective["objective_id"]) for objective in analysis.objectives]
    candidates = {candidate.get("objective_ref"): candidate for candidate in analysis.manifest.get("objective_candidates", [])}
    coverage = []
    feasibility = []
    for objective_id in objective_refs:
        candidate = candidates.get(objective_id, {})
        disposition = candidate.get("disposition", "candidate")
        if disposition == "candidate":
            coverage_disposition, feasibility_status = "planned", "available"
        elif disposition == "stated_gap":
            coverage_disposition, feasibility_status = "stated_gap", "inconclusive"
        elif disposition == "needs_evidence":
            coverage_disposition, feasibility_status = "not_evaluable", "not_evaluable"
        else:
            coverage_disposition, feasibility_status = "excluded", "infeasible"
        reason = "; ".join(candidate.get("limitations", [])) or ("Objective candidate is not supported by approved evidence." if coverage_disposition != "planned" else None)
        row = {"objective_ref": objective_id, "disposition": coverage_disposition}
        if reason:
            row["reason"] = reason
        coverage.append(row)
        feasibility_row = {"objective_ref": objective_id, "strategy": "static", "status": feasibility_status}
        if reason:
            feasibility_row["reason"] = reason
        feasibility.append(feasibility_row)
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
        "coverage_matrix": coverage, "feasibility": feasibility,
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
        if stored.get("content", {}).get("steps") != record.manifest.get("steps"):
            raise ContractValidationError("plan artifact steps differ from manifest steps")
    if record.manifest["artifact_refs"] != list(artifacts):
        raise ContractValidationError("plan artifact references are not closed")
    if record.checkpoint["reviewed_artifact"] != artifacts[f"artifact-{record.manifest['plan_id']}"]:
        raise ContractValidationError("plan checkpoint artifact mismatch")
