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

from atg.adapters.persistence.sqlite_run_state import SQLiteRunStateStore
from atg.domain.run_state import (
    DuplicateEventError,
    InvalidTransitionError,
    RunEvent,
    RunStateError,
    StaleEventError,
    SupersessionError,
)


def create_server(database: str = ":memory:", host: str = "127.0.0.1", port: int = 0):
    store = SQLiteRunStateStore(database)

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
                else:
                    self._respond(404, {"error": "not found"})
            except KeyError:
                self._respond(404, {"error": "run not found"})

        def do_POST(self):  # noqa: N802
            parts = self._parts()
            try:
                body = self._body()
                if parts == ["runs"]:
                    state = store.create_run(body["run_id"], body["created_at"])
                    self._respond(201, state.to_document())
                    return
                if len(parts) == 3 and parts[0] == "runs" and parts[2] == "events":
                    event_payload = dict(body)
                    event_payload.pop("contract", None)
                    event_payload.pop("version", None)
                    if event_payload.get("run_id") != parts[1]:
                        self._respond(400, {"error": "event run_id does not match URL"})
                        return
                    result = store.apply(RunEvent(**event_payload))
                    self._respond(200, {"duplicate": result.duplicate, "state": result.state.to_document()})
                    return
                self._respond(404, {"error": "not found"})
            except KeyError as exc:
                self._respond(400, {"error": f"missing field: {exc.args[0]}"})
            except (ValueError, RunStateError) as exc:
                self._respond(409, {"error": str(exc)})

        def _parts(self):
            return [unquote(part) for part in urlsplit(self.path).path.strip("/").split("/") if part]

        def _body(self):
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length))

        def _respond(self, status, payload):
            encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer((host, port), Handler)
    server.store = store
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
