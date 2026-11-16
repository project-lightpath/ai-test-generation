"""Persistence boundary for durable Plan-stage output."""

from __future__ import annotations

from typing import Protocol, TYPE_CHECKING

from atg.domain.run_state import RunEvent, RunState

if TYPE_CHECKING:
    from atg.application.planning import PlanRecord


class PlanningStore(Protocol):
    """Persist plan bundles and their artifact-bound lifecycle transitions."""

    def get_run(self, run_id: str) -> RunState:
        """Return the current run projection."""

    def get_analysis(self, analysis_id: str):
        """Return the source analysis bundle."""

    def get_plan(self, plan_id: str) -> "PlanRecord | None":
        """Return a persisted plan bundle by stable identity."""

    def get_checkpoint(self, checkpoint_id: str):
        """Return the durable checkpoint projection by identity."""

    def get_feedback(self, feedback_id: str):
        """Return persisted feedback by identity."""

    def record_plan(
        self,
        record: "PlanRecord",
        submission_event: RunEvent,
        checkpoint_event: RunEvent,
    ) -> "PlanRecord":
        """Atomically persist plan output, submission, and review checkpoint."""

    def record_feedback_revision(self, feedback, record, return_event, submission_event, checkpoint_event):
        """Atomically persist feedback, return-to-plan, and a superseding plan."""
