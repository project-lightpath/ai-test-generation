"""Stage-zero request admission with fail-closed and idempotent semantics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Iterable, Mapping

from atg.domain.run_state import RunEvent

CHECK_STATUSES = frozenset({"pass", "fail", "warn", "not_run"})
CHECK_CATEGORIES = frozenset({"self_verification", "repository", "source", "toolchain", "model", "execution", "policy", "evidence", "signing"})


@dataclass(frozen=True)
class AdmissionRequest:
    request_id: str
    run_id: str
    submitted_at: str
    idempotency_key: str
    requested_capabilities: tuple[str, ...] = ()
    tenant_id: str | None = None
    authorization_ref: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)

    def digest(self) -> str:
        document = {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "submitted_at": self.submitted_at,
            "requested_capabilities": list(self.requested_capabilities),
            "tenant_id": self.tenant_id,
            "authorization_ref": self.authorization_ref,
            "payload": self.payload,
        }
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CheckResult:
    name: str
    required_check: bool
    status: str
    category: str = "self_verification"
    detail: str | None = None
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in CHECK_STATUSES:
            raise ValueError(f"unsupported check status: {self.status}")
        if self.category not in CHECK_CATEGORIES:
            raise ValueError(f"unsupported check category: {self.category}")

    def to_document(self) -> dict[str, object]:
        result = asdict(self)
        result["evidence_refs"] = list(self.evidence_refs)
        return {key: value for key, value in result.items() if value is not None and value != []}


@dataclass(frozen=True)
class AdmissionResult:
    admission_id: str
    request_id: str
    run_id: str
    evaluated_at: str
    checks: tuple[CheckResult, ...]
    status: str
    request_digest: str
    platform_readiness_id: str | None = None
    failure_register_refs: tuple[str, ...] = ()
    reason: str | None = None
    limitations: tuple[Mapping[str, object], ...] = ()

    def to_document(self) -> dict[str, object]:
        document = {
            "contract": "atg.run_admission",
            "version": "1.0",
            "admission_id": self.admission_id,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "evaluated_at": self.evaluated_at,
            "checks": [check.to_document() for check in self.checks],
            "status": self.status,
            "request_digest": self.request_digest,
        }
        optional = {
            "platform_readiness_id": self.platform_readiness_id,
            "failure_register_refs": list(self.failure_register_refs),
            "reason": self.reason,
            "limitations": list(self.limitations),
        }
        document.update({key: value for key, value in optional.items() if value is not None and value != []})
        return document


class AdmissionConflictError(ValueError):
    """An idempotency key was reused for a different request payload."""


class AdmissionService:
    def __init__(self, store, checks: Iterable[object]) -> None:
        self._store = store
        self._checks = tuple(checks)

    def admit(self, request: AdmissionRequest) -> AdmissionResult:
        digest = request.digest()
        previous = self._store.get_admission(request.idempotency_key)
        if previous is not None:
            if previous.request_digest != digest:
                raise AdmissionConflictError("idempotency key is bound to a different request")
            return previous

        checks = tuple(check.evaluate(request) for check in self._checks)
        failed = tuple(check for check in checks if check.required_check and check.status in {"fail", "not_run"})
        status = "blocked" if failed else "admitted"
        reason = "; ".join(check.detail or check.name for check in failed) if failed else None
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        result = AdmissionResult(
            admission_id=f"admission-{request.request_id}",
            request_id=request.request_id,
            run_id=request.run_id,
            evaluated_at=now,
            checks=checks,
            status=status,
            request_digest=digest,
            reason=reason,
        )
        event = RunEvent(
            event_id=f"{result.admission_id}-event",
            run_id=request.run_id,
            event_type="admit" if status == "admitted" else "admission_blocked",
            expected_revision=0,
            occurred_at=now,
            reason=reason,
        )
        if hasattr(self._store, "record_admission"):
            return self._store.record_admission(request.idempotency_key, digest, result, event)
        self._store.create_run(request.run_id, request.submitted_at)
        self._store.apply(event)
        self._store.save_admission(request.idempotency_key, digest, result)
        return result
