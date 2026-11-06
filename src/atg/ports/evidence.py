"""Boundary for downstream evidence publication."""

from __future__ import annotations

from typing import Mapping, Protocol


class EvidencePublicationBlockedError(ValueError):
    """Raised when a run is not allowed to publish downstream evidence."""


EVIDENCE_PUBLICATION_STATUSES = frozenset({
    "admitted", "analyzing", "analysis_review", "planning", "plan_review",
    "coding", "code_review", "executing", "execution_review", "quality_review", "completed",
})


class EvidencePublisher(Protocol):
    def publish(self, run_id: str, evidence: Mapping[str, object]) -> str:
        """Publish evidence and return its stable reference."""


class GuardedEvidencePublisher:
    """Development publisher that enforces terminal-run suppression."""

    def __init__(self, run_store) -> None:
        """Create a guarded development publisher backed by run state."""
        self._run_store = run_store
        self.records: dict[str, Mapping[str, object]] = {}

    def publish(self, run_id: str, evidence: Mapping[str, object]) -> str:
        """Publish only from an explicit evidence-capable lifecycle state."""
        state = self._run_store.get_run(run_id)
        if state.status not in EVIDENCE_PUBLICATION_STATUSES:
            raise EvidencePublicationBlockedError(f"run is not allowed to publish evidence: {state.status}")
        reference = f"evidence-{run_id}-{len(self.records) + 1}"
        self.records[reference] = dict(evidence)
        return reference
