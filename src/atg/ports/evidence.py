"""Boundary for downstream evidence publication."""

from __future__ import annotations

from typing import Mapping, Protocol


class EvidencePublicationBlockedError(ValueError):
    """Raised when a run is not allowed to publish downstream evidence."""


class EvidencePublisher(Protocol):
    def publish(self, run_id: str, evidence: Mapping[str, object]) -> str:
        """Publish evidence and return its stable reference."""


class GuardedEvidencePublisher:
    """Development publisher that enforces terminal-run suppression."""

    def __init__(self, run_store) -> None:
        self._run_store = run_store
        self.records: dict[str, Mapping[str, object]] = {}

    def publish(self, run_id: str, evidence: Mapping[str, object]) -> str:
        state = self._run_store.get_run(run_id)
        if state.status in {"blocked", "cancelled", "inconclusive", "stated_gap"}:
            raise EvidencePublicationBlockedError(f"run is not allowed to publish evidence: {state.status}")
        reference = f"evidence-{run_id}-{len(self.records) + 1}"
        self.records[reference] = dict(evidence)
        return reference
