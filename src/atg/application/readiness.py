"""Platform-level readiness, independent from any customer request."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from atg.application.admission import CheckResult


@dataclass(frozen=True)
class PlatformReadinessResult:
    readiness_id: str
    evaluated_at: str
    checks: tuple[CheckResult, ...]
    service_id: str = "atg-local"

    @property
    def status(self) -> str:
        required_failures = [check for check in self.checks if check.required_check and check.status in {"fail", "not_run"}]
        return "blocked" if required_failures else "degraded" if any(check.status == "warn" for check in self.checks) else "ready"

    def to_document(self) -> dict[str, object]:
        return {
            "contract": "atg.platform_readiness",
            "version": "1.0",
            "readiness_id": self.readiness_id,
            "evaluated_at": self.evaluated_at,
            "checks": [check.to_document() for check in self.checks],
            "status": self.status,
            "service": {"kind": "atg-service", "id": self.service_id, "version": "dev"},
            "limitations": [],
        }


class PlatformReadinessService:
    def __init__(self, checks):
        self._checks = tuple(checks)

    def evaluate(self) -> PlatformReadinessResult:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return PlatformReadinessResult("readiness-current", now, tuple(check.evaluate(None) for check in self._checks))
