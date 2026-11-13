"""Durable feedback and plan-revision request values."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from atg.domain.schema_validation import validate_contract_document
from atg.domain.run_state import validate_identifier, validate_timestamp


class FeedbackConflictError(ValueError):
    """Raised when feedback or revision identity is reused inconsistently."""


@dataclass(frozen=True)
class FeedbackRequest:
    """Human or tool feedback bound to one approved plan artifact."""

    feedback_id: str
    run_id: str
    plan_id: str
    plan_revision: int
    plan_artifact_digest: str
    checkpoint_id: str
    created_at: str
    message: str
    category: str = "other"
    severity: str = "info"
    actor_id: str = "reviewer"

    def __post_init__(self) -> None:
        """Validate feedback identity, revision, digest, and message."""
        for field in ("feedback_id", "run_id", "plan_id", "checkpoint_id", "actor_id"):
            validate_identifier(getattr(self, field), field)
        if self.plan_revision < 1:
            raise ValueError("plan_revision must be positive")
        if not self.plan_artifact_digest.startswith("sha256:"):
            raise ValueError("plan_artifact_digest must be a sha256 digest")
        validate_timestamp(self.created_at, "created_at")
        if not self.message:
            raise ValueError("feedback message is required")

    def digest(self) -> str:
        """Return an idempotency digest for the feedback request."""
        payload = self.__dict__.copy()
        return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def to_document(self) -> dict[str, object]:
        """Serialize canonical feedback content."""
        document = {
            "contract": "atg.feedback", "version": "1.0", "feedback_id": self.feedback_id,
            "run_id": self.run_id, "plan_id": self.plan_id, "plan_revision": self.plan_revision,
            "plan_artifact_digest": self.plan_artifact_digest, "checkpoint_id": self.checkpoint_id,
            "source": "human", "actor": {"kind": "person", "id": self.actor_id}, "stage": "plan",
            "category": self.category, "resolution": "open", "created_at": self.created_at,
            "message": self.message, "severity": self.severity, "applies_to": [], "evidence_refs": [],
        }
        validate_contract_document(document, "feedback")
        return document
