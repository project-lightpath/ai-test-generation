"""Persistence boundary for durable Analyze-stage output."""

from __future__ import annotations

from typing import Protocol, TYPE_CHECKING

from atg.application.admission import AdmissionResult
from atg.domain.run_state import RunEvent, RunState, TransitionResult

if TYPE_CHECKING:
    from atg.application.analysis import AnalysisRecord


class AnalysisStore(Protocol):
    """Persist analysis output and its lifecycle transitions atomically."""

    def get_analysis(self, analysis_id: str) -> "AnalysisRecord | None":
        """Return a previously completed analysis by stable identity."""

    def get_run(self, run_id: str) -> RunState:
        """Return the current durable run projection."""

    def record_analysis(
        self,
        record: "AnalysisRecord",
        start_event: RunEvent,
        outcome_event: RunEvent,
        checkpoint_event: RunEvent | None,
    ) -> "AnalysisRecord":
        """Atomically persist output and its gated lifecycle outcome."""

    def get_admission_for_run(self, run_id: str) -> AdmissionResult | None:
        """Return the durable admission that owns a run."""

    def apply(self, event: RunEvent) -> TransitionResult:
        """Atomically journal and apply one lifecycle event."""
