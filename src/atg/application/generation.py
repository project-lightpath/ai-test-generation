"""Bounded Code-stage orchestration with exact generated-test provenance."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import threading
from typing import Mapping

from atg.adapters.generation.deterministic import (
    DeterministicValidationAdapter,
    RequestGenerationAdapter,
)
from atg.domain.digests import digests_equal
from atg.domain.generation import (
    artifact_digest,
    compute_materialized_digest,
    compute_patch_digest,
    validate_artifact_bundle,
    validate_candidate_paths,
    validate_generation_manifest,
    validate_identity,
)
from atg.domain.run_state import (
    InvalidTransitionError,
    RunEvent,
    StaleEventError,
    validate_identifier,
    validate_timestamp,
)
from atg.domain.schema_validation import ContractValidationError, validate_contract_document
from atg.gates.generation import evaluate_generation
from atg.ports.generation import PatchCandidate


class GenerationConflictError(ValueError):
    """Raised when a generation identity or approved plan binding conflicts."""


@dataclass(frozen=True)
class GenerationRequest:
    """Request containing bounded development candidates and attribution."""

    generation_id: str
    plan_id: str
    run_id: str
    expected_revision: int
    created_at: str
    idempotency_key: str
    patch_text: str
    materialized_files: Mapping[str, str]
    base: Mapping[str, object]
    candidate: Mapping[str, object]
    model: Mapping[str, object]
    tool: Mapping[str, object]
    worker: Mapping[str, object]
    base_files: Mapping[str, str] = field(default_factory=dict)
    repair_candidates: tuple[PatchCandidate, ...] = ()

    def __post_init__(self) -> None:
        """Validate identities and bound the caller-supplied development replay."""
        for field in ("generation_id", "plan_id", "run_id", "idempotency_key"):
            validate_identifier(getattr(self, field), field)
        if (
            not isinstance(self.expected_revision, int)
            or isinstance(self.expected_revision, bool)
            or self.expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")
        validate_timestamp(self.created_at, "created_at")
        if not isinstance(self.patch_text, str) or not self.patch_text:
            raise ValueError("patch_text is required")
        _validate_materialized_files(self.materialized_files)
        _validate_materialized_files(self.base_files, allow_empty=True)
        for identity in ("base", "candidate", "model", "tool", "worker"):
            validate_identity(getattr(self, identity), identity)
        if len(self.repair_candidates) > 2:
            raise ValueError("repair candidates exceed maximum attempts")
        for candidate in self.repair_candidates:
            if not isinstance(candidate, PatchCandidate):
                raise ValueError("repair_candidates must contain PatchCandidate values")
            _validate_materialized_files(candidate.materialized_files)
            _validate_materialized_files(candidate.base_files, allow_empty=True)

    @property
    def candidates(self) -> tuple[PatchCandidate, ...]:
        """Return the initial output followed by bounded repairs."""
        return (
            PatchCandidate(self.patch_text, self.materialized_files, self.base_files),
            *self.repair_candidates,
        )

    def digest(self) -> str:
        """Bind every material input that can affect generated output."""
        payload = {
            "generation_id": self.generation_id,
            "plan_id": self.plan_id,
            "run_id": self.run_id,
            "expected_revision": self.expected_revision,
            "created_at": self.created_at,
            "idempotency_key": self.idempotency_key,
            "base": dict(self.base),
            "candidate": dict(self.candidate),
            "model": dict(self.model),
            "tool": dict(self.tool),
            "worker": dict(self.worker),
            "candidates": [
                {
                    "patch_digest": compute_patch_digest(candidate.patch_text),
                    "materialized_digest": compute_materialized_digest(candidate.materialized_files),
                    "base_digest": compute_materialized_digest(candidate.base_files),
                }
                for candidate in self.candidates
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class GenerationRecord:
    """Durable generation manifest, attempt history, and exact artifacts."""

    request_digest: str
    manifest: Mapping[str, object]
    artifacts: tuple[Mapping[str, object], ...]
    attempts: tuple[Mapping[str, object], ...]
    duplicate: bool = False

    def to_storage_document(self) -> dict[str, object]:
        """Serialize exact generated content for durable persistence."""
        return {
            "request_digest": self.request_digest,
            "manifest": dict(self.manifest),
            "artifacts": [dict(artifact) for artifact in self.artifacts],
            "attempts": [dict(attempt) for attempt in self.attempts],
        }

    @classmethod
    def from_storage_document(
        cls, document: Mapping[str, object], *, duplicate: bool = False
    ) -> "GenerationRecord":
        """Restore and verify a persisted generation bundle."""
        record = cls(
            request_digest=str(document["request_digest"]),
            manifest=dict(document["manifest"]),  # type: ignore[arg-type]
            artifacts=tuple(dict(item) for item in document.get("artifacts", [])),  # type: ignore[arg-type]
            attempts=tuple(dict(item) for item in document.get("attempts", [])),  # type: ignore[arg-type]
            duplicate=duplicate,
        )
        validate_generation_record(record)
        return record


class GenerationService:
    """Generate, repair, validate, and persist native tests within three attempts."""

    MAX_ATTEMPTS = 3

    def __init__(self, store, generator=None, validator=None) -> None:
        self._store = store
        self._lock = threading.RLock()
        self._generator = generator or RequestGenerationAdapter()
        self._validator = validator or DeterministicValidationAdapter()

    def generate(self, request: GenerationRequest) -> GenerationRecord:
        """Run bounded generation against the exact approved plan artifact."""
        with self._lock:
            existing = self._store.get_generation(request.generation_id)
            if existing is not None:
                if existing.request_digest != request.digest():
                    raise GenerationConflictError("generation identity is bound to different inputs")
                return GenerationRecord(
                    existing.request_digest,
                    existing.manifest,
                    existing.artifacts,
                    existing.attempts,
                    True,
                )

            state = self._store.get_run(request.run_id)
            if state.status != "coding":
                raise InvalidTransitionError(
                    f"generation requires coding state, got {state.status}"
                )
            is_resume = state.generation_in_progress and state.active_generation_id == request.generation_id
            if is_resume:
                if state.revision != request.expected_revision + 1:
                    raise StaleEventError("in-progress generation revision no longer matches request")
            elif state.revision != request.expected_revision:
                raise StaleEventError(
                    f"event expects revision {request.expected_revision}, current revision is {state.revision}"
                )
            plan = self._approved_plan(request, state)
            checkpoint = self._store.get_checkpoint(plan.checkpoint["checkpoint_id"])
            if checkpoint is None:
                raise GenerationConflictError("approved plan checkpoint is missing")

            analysis = self._store.get_analysis(plan.manifest["analysis_id"])
            target_refs = tuple(
                target
                for objective in (analysis.objectives if analysis is not None else ())
                if objective.get("objective_id") in plan.manifest.get("objective_refs", [])
                for target in objective.get("target_refs", [])
            )
            allowed_paths = tuple(plan.manifest.get("allowed_paths", []))
            started = RunEvent(
                f"{request.generation_id}-started",
                request.run_id,
                "generation_started",
                request.expected_revision,
                request.created_at,
                generation_id=request.generation_id,
                plan_id=request.plan_id,
            )
            if not is_resume:
                self._store.apply(started)
            elif started.event_id not in state.processed_event_ids:
                raise GenerationConflictError("in-progress generation is missing its start event")
            state = self._store.get_run(request.run_id)
            attempts, artifacts = self._store.get_generation_attempts(
                request.generation_id, request.digest()
            )
            attempts = list(attempts)
            artifacts = list(artifacts)
            feedback = tuple(
                check.get("detail", f"{check['name']} check failed")
                for check in attempts[-1].get("checks", [])
                if check.get("status") != "pass"
            ) if attempts else ()
            input_digest = (
                "sha256:" + hashlib.sha256(json.dumps(
                    {"output_digest": attempts[-1]["output_digest"], "findings": feedback},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()).hexdigest()
                if attempts else str(state.active_plan_artifact_digest)
            )
            final_candidate: PatchCandidate | None = None
            final_patch_digest: str | None = None
            final_materialized_digest: str | None = None
            final_status = attempts[-1]["status"] if attempts else "rejected"
            changed_files = tuple(attempts[-1].get("changed_files", [])) if attempts else ()
            if attempts:
                final_patch = next(
                    item["content"] for item in artifacts
                    if item["artifact"]["artifact_id"] == attempts[-1]["patch_artifact_id"]
                )
                final_files = next(
                    item["content"] for item in artifacts
                    if item["artifact"]["artifact_id"] == attempts[-1]["materialized_artifact_id"]
                )
                final_candidate = PatchCandidate(final_patch, final_files)
                final_patch_digest = str(attempts[-1]["output_digest"])
                final_materialized_digest = compute_materialized_digest(final_files)

            stop_after_existing = bool(
                attempts
                and (
                    attempts[-1]["status"] in {"verified", "inconclusive"}
                    or any(check["name"] == "approved_path" and check["status"] == "fail" for check in attempts[-1]["checks"])
                )
            )
            start_attempt = len(attempts) + 1
            attempt_numbers = () if stop_after_existing else range(start_attempt, self.MAX_ATTEMPTS + 1)
            for attempt_number in attempt_numbers:
                candidate = self._generator.generate(request, attempt_number, feedback)
                if candidate is None:
                    break
                final_candidate = candidate
                patch_digest = compute_patch_digest(candidate.patch_text)
                materialized_digest = compute_materialized_digest(candidate.materialized_files)
                path_error = None
                try:
                    changed_files = validate_candidate_paths(
                        candidate.patch_text, candidate.materialized_files, allowed_paths,
                        candidate.base_files,
                    )
                except ValueError as exc:
                    changed_files = tuple(candidate.materialized_files)
                    path_error = str(exc)
                    changed_files = ()
                validation = self._validator.validate(candidate)
                status, feedback, checks = evaluate_generation(
                    candidate, validation, target_refs, path_error
                )
                patch_artifact = _artifact(
                    f"artifact-{request.generation_id}-patch-{attempt_number}",
                    "text/x-diff",
                    candidate.patch_text,
                )
                materialized_artifact = _artifact(
                    f"artifact-{request.generation_id}-tests-{attempt_number}",
                    "application/vnd.atg.materialized-tests+json",
                    dict(candidate.materialized_files),
                )
                attempt = {
                    "attempt": attempt_number,
                    "input_digest": input_digest,
                    "output_digest": patch_digest,
                    "patch_artifact_id": patch_artifact["artifact"]["artifact_id"],
                    "materialized_artifact_id": materialized_artifact["artifact"]["artifact_id"],
                    "changed_files": list(changed_files),
                    "model": dict(request.model),
                    "tool": dict(request.tool),
                    "worker": dict(request.worker),
                    "status": status,
                    "checks": list(checks),
                }
                self._store.record_generation_attempt(
                    request.generation_id,
                    request.run_id,
                    request.digest(),
                    attempt,
                    (patch_artifact, materialized_artifact),
                )
                attempts.append(attempt)
                artifacts.extend((patch_artifact, materialized_artifact))
                final_patch_digest = patch_digest
                final_materialized_digest = materialized_digest
                final_status = status
                if status in {"verified", "inconclusive"} or path_error is not None:
                    break
                input_payload = json.dumps(
                    {"output_digest": patch_digest, "findings": feedback},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                input_digest = "sha256:" + hashlib.sha256(input_payload).hexdigest()

            if final_candidate is None or final_patch_digest is None or final_materialized_digest is None:
                raise GenerationConflictError("generation produced no candidate")
            if final_status != "verified":
                final_status = (
                    "inconclusive"
                    if final_status == "inconclusive" or len(attempts) == self.MAX_ATTEMPTS
                    else "rejected"
                )
            manifest = {
                "contract": "atg.generation",
                "version": "1.0",
                "generation_id": request.generation_id,
                "run_id": request.run_id,
                "plan_id": request.plan_id,
                "plan_revision": plan.manifest["plan_revision"],
                "plan_artifact_digest": state.active_plan_artifact_digest,
                "plan_checkpoint_id": checkpoint["checkpoint_id"],
                "plan_decision_id": checkpoint["decision"]["decision_id"],
                "base": dict(request.base),
                "candidate": dict(request.candidate),
                "patch_digest": final_patch_digest,
                "materialized_test_digest": final_materialized_digest,
                "subject_materializations": [
                    {"subject": dict(request.base), "test_digest": final_materialized_digest},
                    {"subject": dict(request.candidate), "test_digest": final_materialized_digest},
                ],
                "status": final_status,
                "created_at": request.created_at,
                "model": dict(request.model),
                "tool": dict(request.tool),
                "worker": dict(request.worker),
                "attempts": attempts,
                "artifacts": [artifact["artifact"] for artifact in artifacts],
                "limitations": [
                    {
                        "code": "development-static-validation",
                        "description": "Static local prechecks do not prove live framework compilation or collection.",
                        "severity": "warning",
                    }
                ],
            }
            if changed_files:
                manifest["changed_files"] = list(changed_files)
            record = GenerationRecord(
                request.digest(), manifest, tuple(artifacts), tuple(attempts)
            )
            validate_generation_record(record, plan.manifest)
            terminal_type = {
                "verified": "generation_completed",
                "rejected": "generation_rejected",
                "inconclusive": "generation_inconclusive",
            }[final_status]
            terminal = RunEvent(
                f"{request.generation_id}-{terminal_type}",
                request.run_id,
                terminal_type,
                state.revision,
                request.created_at,
                generation_id=request.generation_id,
                artifact_id=attempts[-1]["patch_artifact_id"] if final_status == "verified" else None,
                artifact_digest=final_patch_digest if final_status == "verified" else None,
                reason="; ".join(feedback) if final_status != "verified" else None,
            )
            return self._store.record_generation(record, started, terminal)

    def _approved_plan(self, request: GenerationRequest, state):
        plan = self._store.get_plan(request.plan_id)
        if plan is None or plan.manifest.get("status") != "approved":
            raise GenerationConflictError("generation requires an approved plan")
        if plan.manifest.get("run_id") != request.run_id:
            raise GenerationConflictError("generation plan belongs to different run")
        reviewed = plan.checkpoint.get("reviewed_artifact", {})
        stored_artifact = next(
            (
                item.get("artifact")
                for item in plan.artifacts
                if item.get("artifact", {}).get("artifact_id") == reviewed.get("artifact_id")
            ),
            None,
        )
        checkpoint = self._store.get_checkpoint(plan.checkpoint["checkpoint_id"])
        if (
            state.active_plan_id != request.plan_id
            or state.active_plan_revision != plan.manifest.get("plan_revision")
            or checkpoint is None
            or checkpoint.get("state") != "approved"
            or checkpoint.get("decision", {}).get("value") != "approved"
            or not checkpoint.get("decision", {}).get("decision_id")
            or checkpoint.get("reviewed_artifact") != stored_artifact
            or reviewed != stored_artifact
            or not digests_equal(state.active_plan_artifact_digest, reviewed.get("digest"))
        ):
            raise GenerationConflictError("generation plan approval binding is invalid")
        if not plan.manifest.get("allowed_paths"):
            raise GenerationConflictError("approved plan does not authorize test paths")
        return plan


def validate_generation_record(
    record: GenerationRecord, plan_manifest: Mapping[str, object] | None = None
) -> None:
    """Validate contract, attempt history, and exact artifact closure."""
    validate_contract_document(record.manifest, "generation")
    validate_generation_manifest(record.manifest, plan_manifest)
    if tuple(record.manifest.get("attempts", [])) != record.attempts:
        raise ContractValidationError("generation attempt history differs from manifest")
    try:
        validate_artifact_bundle(record.manifest, record.artifacts)
    except ValueError as exc:
        raise ContractValidationError(str(exc)) from exc


def _artifact(artifact_id: str, media_type: str, content: object) -> dict[str, object]:
    digest, size = artifact_digest(content, media_type)
    return {
        "artifact": {
            "artifact_id": artifact_id,
            "media_type": media_type,
            "digest": digest,
            "size_bytes": size,
        },
        "content": content,
    }


def _validate_materialized_files(files: object, *, allow_empty: bool = False) -> None:
    if not isinstance(files, Mapping) or (not files and not allow_empty):
        raise ValueError("file content must be a non-empty object")
    if not all(isinstance(path, str) and isinstance(content, str) for path, content in files.items()):
        raise ValueError("file content must map paths to text content")
    if len(files) > 10 or any(len(content.encode()) > 1_000_000 for content in files.values()):
        raise ValueError("generated file content exceeds bounds")
    if sum(len(content.encode()) for content in files.values()) > 5_000_000:
        raise ValueError("generated file content exceeds total byte limit")
