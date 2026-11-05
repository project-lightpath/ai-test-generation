"""Application-owned boundaries for stage-zero admission."""

from __future__ import annotations

from typing import Protocol

from atg.application.admission import AdmissionRequest, AdmissionResult, CheckResult
from atg.domain.run_state import RunEvent


class AdmissionCheck(Protocol):
    def evaluate(self, request: AdmissionRequest) -> CheckResult:
        """Return a deterministic result for one admission check."""


class AdmissionStore(Protocol):
    def get_admission(self, idempotency_key: str) -> AdmissionResult | None:
        """Return the prior result for an idempotency key."""

    def get_terminal(self, run_id: str) -> dict | None:
        """Return the durable terminal admission record for a run."""

    def save_admission(self, key: str, request_digest: str, result: AdmissionResult) -> None:
        """Persist an admission result."""

    def create_run(self, run_id: str, created_at: str): ...

    def apply(self, event: RunEvent): ...

    def record_admission(self, key: str, request_digest: str, result: AdmissionResult, event: RunEvent) -> AdmissionResult:
        """Atomically persist an admission result and its run transition."""
