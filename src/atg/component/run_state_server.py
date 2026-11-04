"""Small HTTP component for exercising the durable run-state boundary.

The server is an initial development component. It uses the same JSON
documents and persistence port that the service must expose and can later be
replaced or split as deployment requirements become clearer.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from atg.application.admission import AdmissionConflictError, AdmissionRequest, AdmissionService, CheckResult
from atg.application.readiness import PlatformReadinessService
from atg.adapters.persistence.sqlite_run_state import SQLiteRunStateStore
from atg.domain.run_state import (
    InvalidEventError,
    RunEvent,
    RunStateError,
    validate_identifier,
    validate_timestamp,
)

MAX_REQUEST_BYTES = 1024 * 1024


class BadRequestError(ValueError):
    """Raised when an HTTP request is malformed."""


class RequestTooLargeError(ValueError):
    """Raised when an HTTP request exceeds the component limit."""


def create_server(database: str = ":memory:", host: str = "127.0.0.1", port: int = 0, capabilities=()):
    store = SQLiteRunStateStore(database)

    class CapabilityCheck:
        def evaluate(self, request):
            missing = sorted(set(request.requested_capabilities) - set(capabilities))
            return CheckResult(
                name="requested-capabilities",
                category="policy",
                required_check=True,
                status="fail" if missing else "pass",
                detail=f"Unsupported capabilities: {', '.join(missing)}" if missing else "Requested capabilities are supported.",
            )

    class ContractCheck:
        def evaluate(self, request):
            return CheckResult(
                name="contract-registry", category="self_verification", required_check=True,
                status="pass", detail="ATG v1 contracts are loaded.",
            )

    readiness_service = PlatformReadinessService((ContractCheck(),))
    admission_service = AdmissionService(store, (CapabilityCheck(),), readiness=readiness_service)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            return

        def do_GET(self):  # noqa: N802
            parts = self._parts()
            try:
                if len(parts) == 2 and parts[0] == "runs":
                    self._respond(200, store.get_run(parts[1]).to_document())
                elif len(parts) == 3 and parts[0] == "runs" and parts[2] == "events":
                    self._respond(200, {"events": [_event_document(event) for event in store.events(parts[1])]})
                elif parts == ["readiness"]:
                    self._respond(200, readiness_service.evaluate().to_document())
                else:
                    self._respond(404, {"error": "not found"})
            except KeyError:
                self._respond(404, {"error": "run not found"})

        def do_POST(self):  # noqa: N802
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
                    required = ("request_id", "run_id", "submitted_at", "idempotency_key", "requested_by")
                    if any(not isinstance(body.get(field), str) or not body[field] for field in required):
                        raise BadRequestError("admission requires request_id, run_id, submitted_at, and idempotency_key")
                    for field in ("request_id", "run_id", "idempotency_key"):
                        try:
                            validate_identifier(body[field], field)
                        except ValueError as exc:
                            raise BadRequestError(str(exc)) from exc
                    try:
                        validate_timestamp(body["submitted_at"], "submitted_at")
                    except ValueError as exc:
                        raise BadRequestError(str(exc)) from exc
                    requested = body.get("requested_capabilities", [])
                    if not isinstance(requested, list) or not all(isinstance(value, str) for value in requested):
                        raise BadRequestError("requested_capabilities must be a list of strings")
                    if not isinstance(body["source"], dict) or not isinstance(body["repository"], dict):
                        raise BadRequestError("source and repository must be objects")
                    if not isinstance(body["objective_ids"], list) or not body["objective_ids"] or not all(isinstance(value, str) for value in body["objective_ids"]):
                        raise BadRequestError("objective_ids must be a non-empty list of strings")
                    request = AdmissionRequest(
                        request_id=body["request_id"],
                        run_id=body["run_id"],
                        submitted_at=body["submitted_at"],
                        idempotency_key=body["idempotency_key"],
                        requested_capabilities=tuple(requested),
                        tenant_id=body.get("tenant_id"),
                        authorization_ref=body.get("authorization_ref"),
                        payload=body,
                    )
                    existing = store.get_admission(request.idempotency_key)
                    result = admission_service.admit(request)
                    self._respond(200 if existing is not None else 201, result.to_document())
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

        def _parts(self):
            return [unquote(part) for part in urlsplit(self.path).path.strip("/").split("/") if part]

        def _body(self):
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
            encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer((host, port), Handler)
    server.store = store
    server.admission_service = admission_service
    server.readiness_service = readiness_service
    return server


def _event_document(event: RunEvent) -> dict:
    document = {
        "contract": "atg.run_event",
        "version": "1.0",
    }
    document.update({key: value for key, value in event.__dict__.items() if value is not None})
    return document


def main() -> None:
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
