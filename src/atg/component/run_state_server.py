"""Small HTTP component for exercising the durable run-state boundary.

The server is an initial development component. It uses the same JSON
documents and persistence port that the service must expose and can later be
replaced or split as deployment requirements become clearer.
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from atg.adapters.failure_register import FailureRegisterCheck
from atg.adapters.persistence.sqlite_run_state import SQLiteRunStateStore
from atg.adapters.repository.local import LocalRepositoryContextAdapter
from atg.application.admission import AdmissionConflictError, AdmissionRequest, AdmissionService, CheckResult
from atg.application.analysis import AnalysisConflictError, AnalysisProcessingError, AnalysisRequest, AnalysisService
from atg.application.planning import PlanningConflictError, PlanningRequest, PlanningService
from atg.application.readiness import PlatformReadinessService
from atg.domain.repository_analysis import RepositorySubject
from atg.domain.schema_validation import ContractValidationError, contract_asset_path
from atg.domain.run_state import (
    InvalidEventError,
    RunEvent,
    RunStateError,
    validate_identifier,
    validate_timestamp,
)

MAX_REQUEST_BYTES = 1024 * 1024
REQUEST_FIELDS = frozenset({
    "contract", "version", "request_id", "run_id", "submitted_at", "source", "repository",
    "policy_decision", "objective_ids", "requested_by", "mode", "constraints",
    "requested_capabilities", "tenant_id", "authorization_ref", "environment",
})
ANALYSIS_FIELDS = frozenset({
    "analysis_id", "request_id", "run_id", "repository", "expected_revision", "occurred_at",
    "objective_refs", "requested_capabilities", "supplied_evidence", "supplied_objectives",
})
PLAN_FIELDS = frozenset({"plan_id", "analysis_id", "run_id", "expected_revision", "occurred_at"})
IDENTITY_FIELDS = frozenset({"kind", "id", "version", "digest"})
SOURCE_FIELDS = frozenset({"identity", "locator", "retrieved_at", "content_digest"})
DIGEST_PATTERN = re.compile(r"^[A-Za-z0-9_-]+:[A-Fa-f0-9]{8,}$")


class BadRequestError(ValueError):
    """Raised when an HTTP request is malformed."""


class RequestTooLargeError(ValueError):
    """Raised when an HTTP request exceeds the component limit."""


def create_server(
    database: str = ":memory:",
    host: str = "127.0.0.1",
    port: int = 0,
    capabilities=(),
    repository_roots=None,
    analyzers=(),
):
    """Create the local HTTP development component."""
    store = SQLiteRunStateStore(database)

    class CapabilityCheck:
        def evaluate(self, request):
            """Check that every requested capability is locally available."""
            missing = sorted(set(request.requested_capabilities) - set(capabilities))
            return CheckResult(
                name="requested-capabilities",
                category="policy",
                required_check=True,
                status="fail" if missing else "pass",
                detail=f"Unsupported capabilities: {', '.join(missing)}" if missing else "Requested capabilities are supported.",
            )

    class IdentityCheck:
        def evaluate(self, request):
            """Check that tenant and authorization identities are present."""
            missing = [name for name in ("tenant_id", "authorization_ref") if not getattr(request, name)]
            return CheckResult(
                name="request-identity",
                category="policy",
                required_check=True,
                status="fail" if missing else "pass",
                detail=f"Missing identities: {', '.join(missing)}" if missing else "Tenant and authorization identities are present.",
            )

    class ContractCheck:
        name = "contract-registry"
        required = True

        def evaluate(self, request):
            """Verify that the versioned contract files are present and parseable."""
            contracts = contract_asset_path("v1")
            try:
                paths = tuple(contracts.glob("*.schema.json"))
                if not paths:
                    raise ValueError("no contract schemas found")
                for path in paths:
                    json.loads(path.read_text())
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return CheckResult(
                    name=self.name, category="self_verification", required_check=True,
                    status="not_run", detail=f"contract verification unavailable: {exc}",
                )
            return CheckResult(
                name="contract-registry", category="self_verification", required_check=True,
                status="pass", detail="ATG v1 contracts are loaded.",
            )

    failure_register = FailureRegisterCheck(
        contract_asset_path("failure-register/v1") / "cases.json"
    )
    readiness_service = PlatformReadinessService((ContractCheck(), failure_register))
    admission_service = AdmissionService(store, (CapabilityCheck(), IdentityCheck()), readiness=readiness_service)
    repository_roots = dict(repository_roots or {})
    analysis_service = AnalysisService(store, LocalRepositoryContextAdapter(), tuple(analyzers))
    planning_service = PlanningService(store)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            """Suppress access logging in the development component."""
            return

        def do_GET(self):  # noqa: N802
            """Serve readiness, run state, and event journal documents."""
            parts = self._parts()
            try:
                if len(parts) == 2 and parts[0] == "runs":
                    self._respond(200, store.get_run(parts[1]).to_document())
                elif len(parts) == 2 and parts[0] == "analyses":
                    record = store.get_analysis(parts[1])
                    if record is None:
                        self._respond(404, {"error": "analysis not found"})
                    else:
                        self._respond(200, _analysis_document(record))
                elif len(parts) == 2 and parts[0] == "plans":
                    record = store.get_plan(parts[1])
                    if record is None:
                        self._respond(404, {"error": "plan not found"})
                    else:
                        self._respond(200, _plan_document(record))
                elif len(parts) == 2 and parts[0] == "checkpoints":
                    checkpoint = store.get_checkpoint(parts[1])
                    if checkpoint is None:
                        self._respond(404, {"error": "checkpoint not found"})
                    else:
                        self._respond(200, checkpoint)
                elif len(parts) == 3 and parts[0] == "runs" and parts[2] == "events":
                    self._respond(200, {"events": [_event_document(event) for event in store.events(parts[1])]})
                elif parts == ["readiness"]:
                    self._respond(200, readiness_service.evaluate().to_document())
                else:
                    self._respond(404, {"error": "not found"})
            except KeyError:
                self._respond(404, {"error": "run not found"})
            except ContractValidationError:
                self._respond(500, {"error": "persisted analysis failed integrity validation"})

        def do_POST(self):  # noqa: N802
            """Accept run, event, and admission requests."""
            parts = self._parts()
            try:
                body = self._body()
                if parts == ["runs"]:
                    if not isinstance(body, dict):
                        raise BadRequestError("request body must be a JSON object")
                    if not isinstance(body.get("run_id"), str) or not body["run_id"]:
                        raise BadRequestError("run creation requires run_id and created_at")
                    try:
                        validate_identifier(body["run_id"], "run_id")
                    except ValueError as exc:
                        raise BadRequestError(str(exc)) from exc
                    try:
                        validate_timestamp(body.get("created_at"), "created_at")
                    except ValueError as exc:
                        raise BadRequestError(str(exc)) from exc
                    state = store.create_run(body["run_id"], body["created_at"])
                    self._respond(201, state.to_document())
                    return
                if parts == ["admissions"]:
                    if not isinstance(body, dict):
                        raise BadRequestError("admission body must be a JSON object")
                    if body.get("contract") != "atg.request" or body.get("version") != "1.0":
                        raise BadRequestError("admission requires atg.request version 1.0")
                    unknown = sorted(set(body) - REQUEST_FIELDS)
                    if unknown:
                        raise BadRequestError(f"unsupported request fields: {', '.join(unknown)}")
                    idempotency_key = self.headers.get("Idempotency-Key")
                    if not idempotency_key:
                        raise BadRequestError("admission requires Idempotency-Key header")
                    required = ("request_id", "run_id", "submitted_at", "requested_by", "tenant_id", "authorization_ref")
                    if any(not isinstance(body.get(field), str) or not body[field] for field in required):
                        raise BadRequestError("admission requires request, requester, tenant, and authorization identities")
                    for field in ("request_id", "run_id", "requested_by", "tenant_id", "authorization_ref"):
                        try:
                            validate_identifier(body[field], field)
                        except ValueError as exc:
                            raise BadRequestError(str(exc)) from exc
                    try:
                        validate_identifier(idempotency_key, "Idempotency-Key")
                    except ValueError as exc:
                        raise BadRequestError(str(exc)) from exc
                    try:
                        validate_timestamp(body["submitted_at"], "submitted_at")
                    except ValueError as exc:
                        raise BadRequestError(str(exc)) from exc
                    requested = body.get("requested_capabilities", [])
                    if not isinstance(requested, list) or not all(isinstance(value, str) for value in requested):
                        raise BadRequestError("requested_capabilities must be a list of strings")
                    if len(requested) != len(set(requested)):
                        raise BadRequestError("requested_capabilities must contain unique identifiers")
                    for capability in requested:
                        try:
                            validate_identifier(capability, "requested_capability")
                        except ValueError as exc:
                            raise BadRequestError(str(exc)) from exc
                    if not isinstance(body.get("source"), dict) or not isinstance(body.get("repository"), dict):
                        raise BadRequestError("source and repository must be objects")
                    unknown_source = sorted(set(body["source"]) - SOURCE_FIELDS)
                    if unknown_source:
                        raise BadRequestError(f"source has unsupported fields: {', '.join(unknown_source)}")
                    _validate_identity(body.get("source", {}).get("identity"), "source.identity")
                    if not isinstance(body.get("source", {}).get("locator"), str) or not body["source"]["locator"]:
                        raise BadRequestError("source.locator must be a non-empty string")
                    if "retrieved_at" in body["source"]:
                        try:
                            validate_timestamp(body["source"]["retrieved_at"], "source.retrieved_at")
                        except ValueError as exc:
                            raise BadRequestError(str(exc)) from exc
                    if "content_digest" in body["source"]:
                        _validate_digest(body["source"]["content_digest"], "source.content_digest")
                    _validate_identity(body["repository"], "repository")
                    for field in ("policy_decision", "environment"):
                        if field in body:
                            _validate_identity(body[field], field)
                    objective_ids = body.get("objective_ids", [])
                    if not isinstance(objective_ids, list) or not all(isinstance(value, str) for value in objective_ids):
                        raise BadRequestError("objective_ids must be a list of strings")
                    if body.get("mode") != "analyze" and not objective_ids:
                        raise BadRequestError("full admission requires at least one objective_id")
                    if len(objective_ids) != len(set(objective_ids)):
                        raise BadRequestError("objective_ids must contain unique identifiers")
                    for objective_id in objective_ids:
                        try:
                            validate_identifier(objective_id, "objective_id")
                        except ValueError as exc:
                            raise BadRequestError(str(exc)) from exc
                    if "mode" in body and (
                        not isinstance(body["mode"], str) or body["mode"] not in {"analyze", "full"}
                    ):
                        raise BadRequestError("mode must be analyze or full")
                    if "constraints" in body and (
                        not isinstance(body["constraints"], list)
                        or not all(isinstance(value, str) for value in body["constraints"])
                    ):
                        raise BadRequestError("constraints must be a list of strings")
                    request = AdmissionRequest(
                        request_id=body["request_id"],
                        run_id=body["run_id"],
                        submitted_at=body["submitted_at"],
                        idempotency_key=idempotency_key,
                        requested_capabilities=tuple(requested),
                        tenant_id=body.get("tenant_id"),
                        authorization_ref=body.get("authorization_ref"),
                        payload=body,
                    )
                    existing = store.get_admission(request.idempotency_key)
                    result = admission_service.admit(request)
                    created = existing is None and store.get_admission(request.idempotency_key) is not None
                    self._respond(201 if created else 200, result.to_document())
                    return
                if parts == ["analyses"]:
                    if not isinstance(body, dict):
                        raise BadRequestError("analysis body must be a JSON object")
                    unknown = sorted(set(body) - ANALYSIS_FIELDS)
                    if unknown:
                        raise BadRequestError(f"unsupported analysis fields: {', '.join(unknown)}")
                    repository = body.get("repository")
                    _validate_identity(repository, "repository")
                    if not isinstance(repository.get("version"), str) or not repository["version"]:
                        raise BadRequestError("repository.version is required for analysis")
                    repository_root = repository_roots.get((repository["id"], repository["version"]))
                    if repository_root is None:
                        raise BadRequestError("repository is not materialized for local analysis")
                    for field in ("analysis_id", "request_id", "run_id"):
                        try:
                            validate_identifier(body.get(field), field)
                        except ValueError as exc:
                            raise BadRequestError(str(exc)) from exc
                    try:
                        validate_timestamp(body.get("occurred_at"), "occurred_at")
                    except ValueError as exc:
                        raise BadRequestError(str(exc)) from exc
                    if (
                        not isinstance(body.get("expected_revision"), int)
                        or isinstance(body.get("expected_revision"), bool)
                        or body["expected_revision"] < 0
                    ):
                        raise BadRequestError("expected_revision must be a non-negative integer")
                    for field in ("objective_refs", "requested_capabilities"):
                        values = body.get(field, [])
                        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                            raise BadRequestError(f"{field} must be a list of strings")
                    for field in ("supplied_evidence", "supplied_objectives"):
                        values = body.get(field, [])
                        if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
                            raise BadRequestError(f"{field} must be a list of objects")
                    try:
                        request = AnalysisRequest(
                            analysis_id=body["analysis_id"],
                            request_id=body["request_id"],
                            run_id=body["run_id"],
                            repository_root=str(repository_root),
                            subject=RepositorySubject(repository["id"], repository["version"], repository.get("digest")),
                            expected_revision=body["expected_revision"],
                            occurred_at=body["occurred_at"],
                            objective_refs=tuple(body.get("objective_refs", [])),
                            supplied_evidence=tuple(body.get("supplied_evidence", [])),
                            supplied_objectives=tuple(body.get("supplied_objectives", [])),
                            requested_capabilities=tuple(body.get("requested_capabilities", [])),
                        )
                    except ValueError as exc:
                        raise BadRequestError(str(exc)) from exc
                    result = analysis_service.analyze(request)
                    self._respond(200 if result.duplicate else 201, _analysis_document(result))
                    return
                if parts == ["plans"]:
                    if not isinstance(body, dict):
                        raise BadRequestError("plan body must be a JSON object")
                    unknown = sorted(set(body) - PLAN_FIELDS)
                    if unknown:
                        raise BadRequestError(f"unsupported plan fields: {', '.join(unknown)}")
                    for field in ("plan_id", "analysis_id", "run_id"):
                        try:
                            validate_identifier(body.get(field), field)
                        except ValueError as exc:
                            raise BadRequestError(str(exc)) from exc
                    try:
                        validate_timestamp(body.get("occurred_at"), "occurred_at")
                    except ValueError as exc:
                        raise BadRequestError(str(exc)) from exc
                    if not isinstance(body.get("expected_revision"), int) or isinstance(body.get("expected_revision"), bool) or body["expected_revision"] < 0:
                        raise BadRequestError("expected_revision must be a non-negative integer")
                    request = PlanningRequest(
                        body["plan_id"], body["analysis_id"], body["run_id"], body["expected_revision"], body["occurred_at"]
                    )
                    result = planning_service.plan(request)
                    self._respond(200 if result.duplicate else 201, _plan_document(result))
                    return
                if len(parts) == 3 and parts[0] == "checkpoints" and parts[2] == "decision":
                    if not isinstance(body, dict):
                        raise BadRequestError("decision body must be a JSON object")
                    required = ("run_id", "expected_revision", "occurred_at", "decision", "decision_id", "actor_id", "actor_role", "authorization_ref")
                    if any(field not in body for field in required):
                        raise BadRequestError("decision requires run, revision, timestamp, decision, reviewer, and authorization fields")
                    try:
                        event = RunEvent(
                            event_id=f"{body['decision_id']}-event",
                            run_id=body["run_id"],
                            event_type="decision_recorded",
                            expected_revision=body["expected_revision"],
                            occurred_at=body["occurred_at"],
                            checkpoint_id=parts[1],
                            decision=body["decision"],
                            decision_id=body["decision_id"],
                            actor_id=body["actor_id"],
                            actor_role=body["actor_role"],
                            authorization_ref=body["authorization_ref"],
                            reason=body.get("reason"),
                        )
                    except (TypeError, ValueError) as exc:
                        raise BadRequestError(str(exc)) from exc
                    result = store.apply(event)
                    self._respond(200, {"duplicate": result.duplicate, "state": result.state.to_document()})
                    return
                if len(parts) == 3 and parts[0] == "runs" and parts[2] == "events":
                    if not isinstance(body, dict):
                        raise BadRequestError("request body must be a JSON object")
                    event_payload = dict(body)
                    if event_payload.pop("contract", None) != "atg.run_event":
                        self._respond(400, {"error": "unsupported event contract"})
                        return
                    if event_payload.pop("version", None) != "1.0":
                        self._respond(400, {"error": "unsupported event version"})
                        return
                    if event_payload.get("run_id") != parts[1]:
                        self._respond(400, {"error": "event run_id does not match URL"})
                        return
                    try:
                        event = RunEvent(**event_payload)
                    except TypeError as exc:
                        raise BadRequestError("invalid event payload") from exc
                    except ValueError as exc:
                        raise BadRequestError(str(exc)) from exc
                    result = store.apply(event)
                    self._respond(200, {"duplicate": result.duplicate, "state": result.state.to_document()})
                    return
                self._respond(404, {"error": "not found"})
            except KeyError:
                self._respond(404, {"error": "run not found"})
            except BadRequestError as exc:
                self._respond(400, {"error": str(exc)})
            except RequestTooLargeError as exc:
                self._respond(413, {"error": str(exc)})
            except InvalidEventError as exc:
                self._respond(400, {"error": str(exc)})
            except RunStateError as exc:
                self._respond(409, {"error": str(exc)})
            except AdmissionConflictError as exc:
                self._respond(409, {"error": str(exc)})
            except AnalysisConflictError as exc:
                self._respond(409, {"error": str(exc)})
            except PlanningConflictError as exc:
                self._respond(409, {"error": str(exc)})
            except AnalysisProcessingError:
                self._respond(422, {"error": "analysis processing failed"})
            except ContractValidationError:
                self._respond(500, {"error": "persisted analysis failed integrity validation"})

        def _parts(self):
            """Return decoded non-empty URL path components."""
            return [unquote(part) for part in urlsplit(self.path).path.strip("/").split("/") if part]

        def _body(self):
            """Read and decode a bounded JSON request body."""
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError as exc:
                raise BadRequestError("Content-Length must be an integer") from exc
            if length < 0:
                raise BadRequestError("Content-Length cannot be negative")
            if length > MAX_REQUEST_BYTES:
                raise RequestTooLargeError(f"request body exceeds {MAX_REQUEST_BYTES} bytes")
            try:
                body = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BadRequestError("request body must contain valid JSON") from exc
            return body

        def _respond(self, status, payload):
            """Write a JSON response with an explicit content length."""
            encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer((host, port), Handler)
    server.store = store
    server.admission_service = admission_service
    server.analysis_service = analysis_service
    server.readiness_service = readiness_service
    return server


def _event_document(event: RunEvent) -> dict:
    """Serialize a run event with its public contract envelope."""
    document = {
        "contract": "atg.run_event",
        "version": "1.0",
    }
    document.update({key: value for key, value in event.__dict__.items() if value is not None})
    return document


def _analysis_document(record) -> dict:
    """Serialize normalized analysis output without exposing raw replay content."""
    return {
        "duplicate": record.duplicate,
        "manifest": dict(record.manifest),
        "checkpoint": dict(record.checkpoint) if record.checkpoint is not None else None,
        "evidence": [dict(document) for document in record.evidence],
        "objectives": [dict(document) for document in record.objectives],
        "provenance": dict(record.provenance),
        "artifacts": [dict(document["artifact"]) for document in record.artifacts],
    }


def _plan_document(record) -> dict:
    """Serialize a normalized plan bundle without exposing internal raw state."""
    return {
        "duplicate": record.duplicate,
        "manifest": dict(record.manifest),
        "checkpoint": dict(record.checkpoint),
        "provenance": dict(record.provenance),
        "artifacts": [dict(document["artifact"]) for document in record.artifacts],
    }


def _validate_identity(value, field_name: str) -> None:
    """Validate the required shape of an identity object."""
    if not isinstance(value, dict):
        raise BadRequestError(f"{field_name} must be an identity object")
    unknown = sorted(set(value) - IDENTITY_FIELDS)
    if unknown:
        raise BadRequestError(f"{field_name} has unsupported identity fields: {', '.join(unknown)}")
    if not isinstance(value.get("kind"), str) or not value["kind"]:
        raise BadRequestError(f"{field_name}.kind must be a non-empty string")
    try:
        validate_identifier(value.get("id"), f"{field_name}.id")
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    if "version" in value and (not isinstance(value["version"], str) or not value["version"]):
        raise BadRequestError(f"{field_name}.version must be a non-empty string")
    if "digest" in value:
        _validate_digest(value["digest"], f"{field_name}.digest")


def _validate_digest(value, field_name: str) -> None:
    """Validate a digest using the common contract definition."""
    if not isinstance(value, str) or DIGEST_PATTERN.fullmatch(value) is None:
        raise BadRequestError(f"{field_name} must match algorithm:hex with at least 8 hex characters")


def main() -> None:
    """Run the local HTTP development component from the command line."""
    parser = argparse.ArgumentParser(description="Run the local ATG run-state development component")
    parser.add_argument("--database", default="atg-run-state.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = create_server(args.database, args.host, args.port)
    try:
        server.serve_forever()
    finally:
        server.store.close()
        server.server_close()


if __name__ == "__main__":
    main()
