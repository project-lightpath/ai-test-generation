"""Deterministic development failure-register adapter."""

from __future__ import annotations

import json
from pathlib import Path

from atg.application.admission import CheckResult


class FailureRegisterCheck:
    """Evaluate deterministic failure-register cases for development."""

    def __init__(self, fixture: str | Path, failed_case_ids=()):
        """Load a register fixture and validate configured failed case IDs."""
        document = json.loads(Path(fixture).read_text())
        self.register_id = f"{document['register']}:{document['version']}"
        self.cases = tuple(document["cases"])
        self.failed_case_ids = frozenset(failed_case_ids)
        known_ids = {case["id"] for case in self.cases}
        unknown_ids = self.failed_case_ids - known_ids
        if unknown_ids:
            raise ValueError(f"unknown failure-register case IDs: {', '.join(sorted(unknown_ids))}")

    def evaluate(self, request):
        """Return the deterministic result for the configured failed cases."""
        failures = [case["id"] for case in self.cases if case["id"] in self.failed_case_ids and case.get("required", True)]
        return CheckResult(
            name="failure-register",
            category="self_verification",
            required_check=True,
            status="fail" if failures else "pass",
            detail=f"Failure cases: {', '.join(failures)}" if failures else self.register_id,
            evidence_refs=(self.register_id, *failures),
        )
