"""Persistence boundary for durable ATG run state."""

from __future__ import annotations

from typing import Protocol

from atg.domain.run_state import RunEvent, RunState, TransitionResult


class RunStateStore(Protocol):
    """The minimal store needed by workflow/application code."""

    def create_run(self, run_id: str, created_at: str) -> RunState:
        """Create or return a run without changing an existing run."""

    def get_run(self, run_id: str) -> RunState:
        """Load the latest committed state projection."""

    def apply(self, event: RunEvent) -> TransitionResult:
        """Atomically validate, journal, and apply one event."""

    def replay(self, run_id: str) -> RunState:
        """Rebuild state from the append-only event journal."""
