import json
import http.client
import pathlib
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.adapters.persistence.sqlite_run_state import SQLiteRunStateStore  # noqa: E402
from atg.component.run_state_server import MAX_REQUEST_BYTES, create_server  # noqa: E402
from atg.domain.run_state import DuplicateEventError, RunEvent, StaleEventError  # noqa: E402


def admit_event(run_id="run-1", event_id="event-1", revision=0):
    return RunEvent(
        event_id=event_id,
        run_id=run_id,
        event_type="admit",
        expected_revision=revision,
        occurred_at="2026-11-03T17:00:00Z",
    )


class SQLiteRunStateComponentTests(unittest.TestCase):
    def test_restart_replays_journal_and_preserves_duplicate_protection(self):
        with TemporaryDirectory() as directory:
            database = str(pathlib.Path(directory) / "run-state.sqlite3")
            first = SQLiteRunStateStore(database)
            first.create_run("run-1", "2026-11-03T17:00:00Z")
            event = admit_event()
            first.apply(event)
            first.close()

            second = SQLiteRunStateStore(database)
            state = second.replay("run-1")
            self.assertEqual(state.run_id, "run-1")
            self.assertEqual(state.status, "admitted")
            self.assertEqual(state.revision, 1)
            self.assertIn("event-1", state.event_fingerprints)
            self.assertEqual(tuple(state.processed_event_ids), ("event-1",))
            self.assertTrue(second.apply(event).duplicate)
            with self.assertRaises(DuplicateEventError):
                second.apply(admit_event(event_id="event-1", revision=1))
            with self.assertRaises(StaleEventError):
                second.apply(admit_event(event_id="event-2", revision=0))
            second.close()

    def test_review_expiry_and_retry_are_persisted(self):
        with TemporaryDirectory() as directory:
            database = str(pathlib.Path(directory) / "run-state.sqlite3")
            store = SQLiteRunStateStore(database)
            store.create_run("run-1", "2026-11-03T17:00:00Z")
            store.apply(admit_event())
            store.apply(
                RunEvent(
                    event_id="event-checkpoint",
                    run_id="run-1",
                    event_type="checkpoint_created",
                    expected_revision=1,
                    occurred_at="2026-11-03T17:00:01Z",
                    checkpoint_id="checkpoint-1",
                    checkpoint_kind="plan_review",
                    expires_at="2026-11-03T17:01:00Z",
                )
            )
            store.apply(
                RunEvent(
                    event_id="event-expire",
                    run_id="run-1",
                    event_type="checkpoint_expired",
                    expected_revision=2,
                    occurred_at="2026-11-03T17:01:01Z",
                    checkpoint_id="checkpoint-1",
                )
            )
            store.apply(
                RunEvent(
                    event_id="event-checkpoint-2",
                    run_id="run-1",
                    event_type="checkpoint_created",
                    expected_revision=3,
                    occurred_at="2026-11-03T17:02:00Z",
                    checkpoint_id="checkpoint-2",
                    checkpoint_kind="plan_review",
                )
            )
            store.apply(
                RunEvent(
                    event_id="event-block",
                    run_id="run-1",
                    event_type="decision_recorded",
                    expected_revision=4,
                    occurred_at="2026-11-03T17:02:01Z",
                    checkpoint_id="checkpoint-2",
                    decision="blocked",
                    decision_id="decision-1",
                    actor_id="reviewer-1",
                    actor_role="owner",
                    authorization_ref="approval-policy-1",
                    reason="dependency unavailable",
                )
            )
            store.apply(
                RunEvent(
                    event_id="event-retry",
                    run_id="run-1",
                    event_type="retry_requested",
                    expected_revision=5,
                    occurred_at="2026-11-03T17:03:00Z",
                    reason="dependency restored",
                )
            )
            store.close()

            restarted = SQLiteRunStateStore(database)
            state = restarted.replay("run-1")
            self.assertEqual(state.status, "admitted")
            self.assertEqual(state.revision, 6)
            self.assertIn("event-retry", state.event_fingerprints)
            restarted.close()

    def test_separate_store_instances_observe_compare_and_swap_conflicts(self):
        with TemporaryDirectory() as directory:
            database = str(pathlib.Path(directory) / "run-state.sqlite3")
            first = SQLiteRunStateStore(database)
            second = SQLiteRunStateStore(database)
            first.create_run("run-1", "2026-11-03T17:00:00Z")
            second.create_run("run-1", "2026-11-03T17:00:00Z")
            first.apply(admit_event())
            with self.assertRaises(StaleEventError):
                second.apply(admit_event(event_id="event-stale", revision=0))
            self.assertTrue(second.apply(admit_event()).duplicate)
            first.close()
            second.close()


class RunStateHTTPComponentTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.database = str(pathlib.Path(self.directory.name) / "run-state.sqlite3")
        self.server = create_server(self.database)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://{self.server.server_address[0]}:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.store.close()
        self.server.server_close()
        self.directory.cleanup()

    def request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        return self.raw_request(method, path, data)

    def raw_request(self, method, path, data):
        request = Request(f"{self.base_url}{path}", data=data, method=method)
        request.add_header("Content-Type", "application/json")
        with urlopen(request) as response:
            return response.status, json.loads(response.read())

    def test_process_boundary_creates_applies_and_reads_run_state(self):
        status, state = self.request(
            "POST",
            "/runs",
            {"run_id": "run-http", "created_at": "2026-11-03T17:00:00Z"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(state["run_id"], "run-http")
        self.assertEqual(state["revision"], 0)
        self.assertEqual(state["updated_at"], "2026-11-03T17:00:00Z")
        self.assertEqual(state["status"], "requested")
        event = admit_event(run_id="run-http")
        event_payload = {"contract": "atg.run_event", "version": "1.0", **event.__dict__}
        status, applied = self.request("POST", "/runs/run-http/events", event_payload)
        self.assertEqual(status, 200)
        self.assertFalse(applied["duplicate"])
        self.assertEqual(applied["state"]["status"], "admitted")
        status, duplicate = self.request("POST", "/runs/run-http/events", event_payload)
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["duplicate"])
        status, fetched = self.request("GET", "/runs/run-http")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["run_id"], "run-http")
        self.assertEqual(fetched["status"], "admitted")
        self.assertEqual(fetched["revision"], 1)

    def test_process_boundary_returns_conflict_for_stale_event(self):
        self.request("POST", "/runs", {"run_id": "run-http", "created_at": "2026-11-03T17:00:00Z"})
        event = admit_event(run_id="run-http")
        event_payload = {"contract": "atg.run_event", "version": "1.0", **event.__dict__}
        self.request("POST", "/runs/run-http/events", event_payload)
        stale = {**event_payload, "event_id": "event-stale"}
        try:
            self.request("POST", "/runs/run-http/events", stale)
        except HTTPError as error:
            self.assertEqual(error.code, 409)
            body = json.loads(error.read())
            self.assertEqual(
                body,
                {"error": "event expects revision 0, current revision is 1"},
            )
            error.close()
        else:
            self.fail("stale event should return HTTP 409")

    def test_event_ingress_rejects_bad_contract_version_and_malformed_payloads(self):
        self.request("POST", "/runs", {"run_id": "run-http", "created_at": "2026-11-03T17:00:00Z"})
        event = admit_event(run_id="run-http")
        payload = {"contract": "atg.wrong", "version": "1.0", **event.__dict__}
        for field, expected in (("contract", "unsupported event contract"), ("version", "unsupported event version")):
            invalid = {**payload, field: "wrong"}
            if field == "version":
                invalid["contract"] = "atg.run_event"
            try:
                self.request("POST", "/runs/run-http/events", invalid)
            except HTTPError as error:
                self.assertEqual(error.code, 400)
                self.assertEqual(json.loads(error.read()), {"error": expected})
                error.close()
            else:
                self.fail(f"invalid {field} should return HTTP 400")

        missing_id = {"contract": "atg.run_event", "version": "1.0", **event.__dict__}
        del missing_id["event_id"]
        try:
            self.request("POST", "/runs/run-http/events", missing_id)
        except HTTPError as error:
            self.assertEqual(error.code, 400)
            self.assertEqual(json.loads(error.read()), {"error": "invalid event payload"})
            error.close()
        else:
            self.fail("malformed event should return HTTP 400")

        try:
            self.raw_request("POST", "/runs/run-http/events", b"{")
        except HTTPError as error:
            self.assertEqual(error.code, 400)
            self.assertEqual(json.loads(error.read()), {"error": "request body must contain valid JSON"})
            error.close()
        else:
            self.fail("invalid JSON should return HTTP 400")

    def test_event_ingress_rejects_unknown_run_and_oversized_body(self):
        event = admit_event(run_id="missing-run")
        payload = {"contract": "atg.run_event", "version": "1.0", **event.__dict__}
        try:
            self.request("POST", "/runs/missing-run/events", payload)
        except HTTPError as error:
            self.assertEqual(error.code, 404)
            self.assertEqual(json.loads(error.read()), {"error": "run not found"})
            error.close()
        else:
            self.fail("unknown run should return HTTP 404")

        connection = http.client.HTTPConnection(*self.server.server_address)
        connection.putrequest("POST", "/runs/missing-run/events")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(MAX_REQUEST_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        self.assertEqual(
            json.loads(response.read()),
            {"error": "request body exceeds 1048576 bytes"},
        )
        connection.close()


if __name__ == "__main__":
    unittest.main()
