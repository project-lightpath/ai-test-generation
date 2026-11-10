"""Platform-level readiness, independent from any customer request."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from atg.application.admission import CheckResult, evaluate_check


@dataclass(frozen=True)
class PlatformReadinessResult:
    readiness_id: str
    evaluated_at: str
    checks: tuple[CheckResult, ...]
    service_id: str = "atg-local"
    service_version: str = "dev"
    limitations: tuple[dict[str, object], ...] = ()

    @property
    def service(self) -> dict[str, object]:
        """Return the service identity attached to each readiness result."""
        return {"kind": "atg-service", "id": self.service_id, "version": self.service_version}

    @property
    def degraded_capabilities(self) -> tuple[str, ...]:
        """Return capabilities whose readiness checks are not passing."""
        return tuple(sorted({check.capability for check in self.checks if check.status != "pass" and check.capability}))

    @property
    def status(self) -> str:
        """Return the aggregate readiness status."""
        required_failures = [check for check in self.checks if check.required_check and check.status in {"fail", "not_run"}]
        return "blocked" if required_failures else "degraded" if any(check.status != "pass" for check in self.checks) else "ready"

    def to_document(self) -> dict[str, object]:
        """Serialize readiness and its check observations."""
        check_documents = []
        for check in self.checks:
            document = check.to_document()
            document.setdefault("limitations", [])
            check_documents.append(document)
        return {
            "contract": "atg.platform_readiness",
            "version": "1.0",
            "readiness_id": self.readiness_id,
            "evaluated_at": self.evaluated_at,
            "checks": check_documents,
            "status": self.status,
            "service": self.service,
            "limitations": list(self.limitations),
        }


class PlatformReadinessService:
    """Evaluate platform checks independently of customer requests."""

    def __init__(self, checks, service_id="atg-local", service_version="dev", limitations=()):
        """Create a readiness service with deterministic check adapters."""
        self._checks = tuple(checks)
        if not self._checks:
            raise ValueError("platform readiness requires at least one check")
        self._service_id = service_id
        self._service_version = service_version
        self._limitations = tuple(limitations)

    def evaluate(self) -> PlatformReadinessResult:
        """Return a uniquely identified readiness evaluation."""
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        service = {"kind": "atg-service", "id": self._service_id, "version": self._service_version}
        checks = tuple(evaluate_check(check, None).with_metadata(now, service) for check in self._checks)
        return PlatformReadinessResult(
            f"readiness-{uuid4().hex}",
            now,
            checks,
            service_id=self._service_id,
            service_version=self._service_version,
            limitations=self._limitations,
        )
