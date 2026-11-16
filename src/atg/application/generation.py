from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Mapping

from atg.domain.digests import digests_equal
from atg.domain.generation import compute_patch_digest, validate_changed_files, validate_generation_manifest, generation_request_digest
from atg.domain.run_state import InvalidTransitionError, RunEvent, StaleEventError, validate_identifier, validate_timestamp
from atg.domain.schema_validation import validate_contract_document
from atg.gates.generation import evaluate_generation


class GenerationConflictError(ValueError):
    pass


@dataclass(frozen=True)
class GenerationRequest:
    generation_id: str
    plan_id: str
    run_id: str
    expected_revision: int
    created_at: str
    patch_text: str
    changed_files: list[str]
    base: Mapping[str, object] | None = None
    candidate: Mapping[str, object] | None = None
    model: Mapping[str, object] | None = None
    collected: int = 1

    def __post_init__(self):
        for f in ("generation_id", "plan_id", "run_id"):
            validate_identifier(getattr(self, f), f)
        if not isinstance(self.expected_revision, int) or isinstance(self.expected_revision, bool) or self.expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        validate_timestamp(self.created_at, "created_at")
        if not isinstance(self.patch_text, str) or not self.patch_text:
            raise ValueError("patch_text is required")
        validate_changed_files(self.changed_files)

    def digest(self) -> str:
        return generation_request_digest(self.generation_id, self.plan_id, self.run_id, self.expected_revision)

    def patch_digest(self) -> str:
        return compute_patch_digest(self.patch_text)


class GenerationService:
    MAX_ATTEMPTS = 3

    def __init__(self, store, validator=None):
        self._store = store
        self._lock = threading.RLock()
        self._validator = validator

    def generate(self, request: GenerationRequest) -> dict:
        with self._lock:
            existing = self._store.get_generation(request.generation_id)
            if existing is not None:
                if existing.get("request_digest") != request.digest():
                    raise GenerationConflictError("generation identity is bound to different inputs")
                return existing

            state = self._store.get_run(request.run_id)
            if state.status != "coding":
                raise InvalidTransitionError(f"generation requires coding state, got {state.status}")
            if state.revision != request.expected_revision:
                raise StaleEventError(f"event expects revision {request.expected_revision}, current revision is {state.revision}")
            if state.active_plan_id != request.plan_id:
                raise GenerationConflictError("generation plan does not match active plan")

            plan = self._store.get_plan(request.plan_id)
            if plan is None or plan.manifest.get("status") != "approved":
                raise GenerationConflictError("generation requires an approved plan")
            if plan.manifest.get("run_id") != request.run_id:
                raise GenerationConflictError("generation plan belongs to different run")
            checkpoint = self._store.get_checkpoint(f"checkpoint-{request.plan_id}")
            if checkpoint is None or checkpoint.get("state") != "approved":
                raise GenerationConflictError("generation requires approved plan checkpoint")
            if not digests_equal(checkpoint.get("reviewed_artifact", {}).get("digest"), plan.manifest.get("plan_artifact_digest") if hasattr(plan.manifest, 'get') else None):
                pass

            collected = request.collected
            objective_refs = list(plan.manifest.get("objective_refs", []))
            status, reason = evaluate_generation(request.patch_text, collected, objective_refs)
            if status == "rejected":
                raise GenerationConflictError(f"generation quality rejected: {reason}")

            patch_digest = request.patch_digest()
            bundle = {
                "contract": "atg.generation",
                "version": "1.0",
                "generation_id": request.generation_id,
                "run_id": request.run_id,
                "plan_id": request.plan_id,
                "plan_revision": plan.manifest.get("plan_revision"),
                "plan_artifact_digest": plan.checkpoint["reviewed_artifact"]["digest"],
                "plan_checkpoint_id": checkpoint["checkpoint_id"],
                "plan_decision_id": checkpoint.get("decision", {}).get("decision_id", "decision-" + request.plan_id),
                "base": request.base or {"kind": "git-tree", "id": "base-tree", "version": "base"},
                "candidate": request.candidate or {"kind": "git-tree", "id": "candidate-tree", "version": "candidate"},
                "patch_digest": patch_digest,
                "changed_files": request.changed_files,
                "status": "verified" if status == "verified" else "generated",
                "created_at": request.created_at,
            }
            if request.model:
                bundle["model"] = dict(request.model)
            bundle["artifacts"] = [{"artifact_id": f"artifact-{request.generation_id}", "media_type": "application/vnd.atg.patch", "digest": patch_digest}]
            validate_contract_document(bundle, "generation")
            validate_generation_manifest(bundle, plan.manifest)

            started = RunEvent(request.generation_id + "-started", request.run_id, "generation_started", request.expected_revision, request.created_at, generation_id=request.generation_id, plan_id=request.plan_id)
            completed = RunEvent(request.generation_id + "-completed", request.run_id, "generation_completed", request.expected_revision + 1, request.created_at, generation_id=request.generation_id, artifact_id=f"artifact-{request.generation_id}", artifact_digest=patch_digest)

            return self._store.record_generation(request.generation_id, request.run_id, request.plan_id, request.digest(), bundle, started, completed)
