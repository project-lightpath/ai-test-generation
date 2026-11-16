import json
import http.client
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.adapters.persistence.sqlite_run_state import SQLiteRunStateStore  # noqa: E402
from atg.application.admission import AdmissionRequest, AdmissionResult, CheckResult  # noqa: E402
from atg.component.run_state_server import MAX_REQUEST_BYTES, create_server  # noqa: E402
from atg.domain.run_state import DuplicateEventError, InvalidTransitionError, RunEvent, StaleEventError  # noqa: E402
from atg.domain.schema_validation import ContractValidationError  # noqa: E402


def admit_event(run_id="run-1", event_id="event-1", revision=0):
    return RunEvent(
        event_id=event_id,
        run_id=run_id,
        event_type="admit",
        expected_revision=revision,
        occurred_at="2026-11-03T17:00:00Z",
    )


def record_admission(store, event):
    """Record the admission required before applying a persisted admit event."""
    result = AdmissionResult(
        admission_id=f"admission-{event.run_id}",
        request_id=f"request-{event.run_id}",
        run_id=event.run_id,
        evaluated_at=event.occurred_at,
        checks=(CheckResult(name="contract", required_check=True, status="pass"),),
        status="admitted",
        request_digest=f"digest-{event.run_id}",
    )
    return store.record_admission(f"key-{event.run_id}", result.request_digest, result, event)


class SQLiteRunStateComponentTests(unittest.TestCase):
    def test_checkpoint_id_cannot_be_reused_by_another_run(self):
        store = SQLiteRunStateStore()
        for run_id in ("run-1", "run-2"):
            store.create_run(run_id, "2026-11-03T17:00:00Z")
            record_admission(store, admit_event(run_id=run_id, event_id=f"admit-{run_id}"))
            store.apply(RunEvent(f"start-{run_id}", run_id, "analysis_started", 1, "2026-11-03T17:00:01Z"))
            store.apply(RunEvent(f"complete-{run_id}", run_id, "analysis_completed", 2, "2026-11-03T17:00:02Z"))
            if run_id == "run-1":
                store.apply(RunEvent("checkpoint-1", run_id, "checkpoint_created", 3, "2026-11-03T17:00:03Z", checkpoint_id="shared", checkpoint_kind="analysis_review"))
            else:
                with self.assertRaises(ContractValidationError):
                    store.apply(RunEvent("checkpoint-2", run_id, "checkpoint_created", 3, "2026-11-03T17:00:03Z", checkpoint_id="shared", checkpoint_kind="analysis_review"))
        store.close()

    def test_restart_replays_journal_and_preserves_duplicate_protection(self):
        with TemporaryDirectory() as directory:
            database = str(pathlib.Path(directory) / "run-state.sqlite3")
            first = SQLiteRunStateStore(database)
            first.create_run("run-1", "2026-11-03T17:00:00Z")
            event = admit_event()
            record_admission(first, event)
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
            record_admission(store, admit_event())
            store.apply(RunEvent("analysis-start", "run-1", "analysis_started", 1, "2026-11-03T17:00:00.1Z"))
            store.apply(RunEvent("analysis-complete", "run-1", "analysis_completed", 2, "2026-11-03T17:00:00.2Z"))
            store.apply(RunEvent("analysis-review", "run-1", "checkpoint_created", 3, "2026-11-03T17:00:00.3Z", checkpoint_id="analysis-checkpoint", checkpoint_kind="analysis_review"))
            store.apply(RunEvent("analysis-approve", "run-1", "decision_recorded", 4, "2026-11-03T17:00:00.4Z", checkpoint_id="analysis-checkpoint", decision="approved", decision_id="decision-analysis", actor_id="reviewer", actor_role="owner", authorization_ref="auth-analysis"))
            store.apply(RunEvent("plan-submit", "run-1", "plan_submitted", 5, "2026-11-03T17:00:00.5Z", plan_id="plan-fixture", plan_revision=1, plan_artifact_id="artifact-plan-fixture", plan_artifact_digest="sha256:" + "a" * 64))
            store.apply(
                RunEvent(
                    event_id="event-checkpoint",
                    run_id="run-1",
                    event_type="checkpoint_created",
                    expected_revision=6,
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
                    expected_revision=7,
                    occurred_at="2026-11-03T17:01:01Z",
                    checkpoint_id="checkpoint-1",
                )
            )
            store.apply(
                RunEvent(
                    event_id="event-checkpoint-2",
                    run_id="run-1",
                    event_type="checkpoint_created",
                    expected_revision=8,
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
                    expected_revision=9,
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
                    expected_revision=10,
                    occurred_at="2026-11-03T17:03:00Z",
                    reason="dependency restored",
                )
            )
            store.close()

            restarted = SQLiteRunStateStore(database)
            state = restarted.replay("run-1")
            self.assertEqual(state.status, "admitted")
            self.assertEqual(state.revision, 11)
            self.assertIn("event-retry", state.event_fingerprints)
            restarted.close()

    def test_separate_store_instances_observe_compare_and_swap_conflicts(self):
        with TemporaryDirectory() as directory:
            database = str(pathlib.Path(directory) / "run-state.sqlite3")
            first = SQLiteRunStateStore(database)
            second = SQLiteRunStateStore(database)
            first.create_run("run-1", "2026-11-03T17:00:00Z")
            second.create_run("run-1", "2026-11-03T17:00:00Z")
            record_admission(first, admit_event())
            with self.assertRaises(StaleEventError):
                second.apply(admit_event(event_id="event-stale", revision=0))
            self.assertTrue(second.apply(admit_event()).duplicate)
            first.close()
            second.close()

    def test_direct_admission_event_requires_recorded_result(self):
        store = SQLiteRunStateStore()
        store.create_run("run-1", "2026-11-03T17:00:00Z")
        with self.assertRaises(InvalidTransitionError):
            store.apply(admit_event())
        store.close()


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

    def record_http_admission(self, run_id="run-http"):
        """Create the durable admission required by the event endpoint."""
        return self.server.admission_service.admit(AdmissionRequest(
            request_id=f"request-{run_id}",
            run_id=run_id,
            submitted_at="2026-11-03T17:00:00Z",
            idempotency_key=f"key-{run_id}",
            tenant_id="tenant-1",
            authorization_ref="authorization-1",
            payload={
                "request_id": f"request-{run_id}",
                "run_id": run_id,
                "requested_by": "user-1",
                "tenant_id": "tenant-1",
                "authorization_ref": "authorization-1",
                "source": {"identity": {"kind": "vpe", "id": "vpe-1"}, "locator": "https://example.invalid"},
                "repository": {"kind": "git", "id": "repo-1"},
                "objective_ids": ["objective-1"],
            },
        ))

    def next_event_timestamp(self):
        """Return a current timestamp safely after the admission transition."""
        return (datetime.now(timezone.utc) + timedelta(seconds=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")

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
        self.record_http_admission()
        event = RunEvent(
            event_id="event-analysis",
            run_id="run-http",
            event_type="analysis_started",
            expected_revision=1,
            occurred_at=self.next_event_timestamp(),
        )
        event_payload = {"contract": "atg.run_event", "version": "1.0", **event.__dict__}
        status, applied = self.request("POST", "/runs/run-http/events", event_payload)
        self.assertEqual(status, 200)
        self.assertFalse(applied["duplicate"])
        self.assertEqual(applied["state"]["status"], "analyzing")
        status, duplicate = self.request("POST", "/runs/run-http/events", event_payload)
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["duplicate"])
        status, fetched = self.request("GET", "/runs/run-http")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["run_id"], "run-http")
        self.assertEqual(fetched["status"], "analyzing")
        self.assertEqual(fetched["revision"], 2)
        status, events = self.request("GET", "/runs/run-http/events")
        self.assertEqual(status, 200)
        self.assertEqual(len(events["events"]), 2)
        self.assertEqual(
            events["events"][0]["event_id"],
            "admission-request-run-http-event",
        )
        self.assertEqual(events["events"][0]["event_type"], "admit")
        self.assertEqual(events["events"][0]["run_id"], "run-http")

    def test_process_boundary_returns_conflict_for_stale_event(self):
        self.request("POST", "/runs", {"run_id": "run-http", "created_at": "2026-11-03T17:00:00Z"})
        self.record_http_admission()
        event = RunEvent(
            event_id="event-analysis",
            run_id="run-http",
            event_type="analysis_started",
            expected_revision=1,
            occurred_at=self.next_event_timestamp(),
        )
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
                {"error": "event expects revision 1, current revision is 2"},
            )
            error.close()
        else:
            self.fail("stale event should return HTTP 409")

    def test_event_ingress_rejects_bad_contract_version_and_malformed_payloads(self):
        for payload, expected in (
            ({"run_id": 123, "created_at": "2026-11-03T17:00:00Z"}, "run creation requires run_id and created_at"),
            ({"run_id": "bad-time", "created_at": 12345}, "created_at must be a timezone-aware RFC 3339 timestamp"),
            ({"run_id": "../", "created_at": "2026-11-03T17:00:00Z"}, "run_id must be a valid v1 identifier"),
            ({"run_id": "a" * 257, "created_at": "2026-11-03T17:00:00Z"}, "run_id must be a valid v1 identifier"),
        ):
            try:
                self.request("POST", "/runs", payload)
            except HTTPError as error:
                self.assertEqual(error.code, 400)
                self.assertEqual(json.loads(error.read()), {"error": expected})
                error.close()
            else:
                self.fail("invalid run creation input should return HTTP 400")

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
            self.assertEqual(
                json.loads(error.read()),
                {"error": "invalid event payload"},
            )
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

        try:
            self.raw_request("POST", "/runs/run-http/events", b"\xff\xfe")
        except HTTPError as error:
            self.assertEqual(error.code, 400)
            self.assertEqual(json.loads(error.read()), {"error": "request body must contain valid JSON"})
            error.close()
        else:
            self.fail("non-UTF-8 JSON should return HTTP 400")

        invalid_value = {"contract": "atg.run_event", "version": "1.0", **event.__dict__, "event_id": ""}
        try:
            self.request("POST", "/runs/run-http/events", invalid_value)
        except HTTPError as error:
            self.assertEqual(error.code, 400)
            self.assertEqual(
                json.loads(error.read()),
                {"error": "event_id must be a valid v1 identifier"},
            )
            error.close()
        else:
            self.fail("empty event ID should return HTTP 400")

        invalid_timestamp = {"contract": "atg.run_event", "version": "1.0", **event.__dict__, "occurred_at": 12345}
        try:
            self.request("POST", "/runs/run-http/events", invalid_timestamp)
        except HTTPError as error:
            self.assertEqual(error.code, 400)
            self.assertEqual(
                json.loads(error.read()),
                {"error": "occurred_at must be a timezone-aware RFC 3339 timestamp"},
            )
            error.close()
        else:
            self.fail("numeric timestamp should return HTTP 400")

        invalid_field_type = {
            "contract": "atg.run_event",
            "version": "1.0",
            **event.__dict__,
            "event_id": "event-invalid-checkpoint",
            "checkpoint_id": 123,
        }
        try:
            self.request("POST", "/runs/run-http/events", invalid_field_type)
        except HTTPError as error:
            self.assertEqual(error.code, 400)
            self.assertEqual(
                json.loads(error.read()),
                {"error": "checkpoint_id must be a valid v1 identifier"},
            )
            error.close()
        else:
            self.fail("numeric checkpoint ID should return HTTP 400")

        invalid_revision = {
            "contract": "atg.run_event",
            "version": "1.0",
            **event.__dict__,
            "event_id": "event-negative-revision",
            "expected_revision": -1,
        }
        try:
            self.request("POST", "/runs/run-http/events", invalid_revision)
        except HTTPError as error:
            self.assertEqual(error.code, 400)
            self.assertEqual(
                json.loads(error.read()),
                {"error": "expected_revision must be a non-negative integer"},
            )
            error.close()
        else:
            self.fail("negative expected revision should return HTTP 400")

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

        try:
            self.request("GET", "/runs/missing-run/events")
        except HTTPError as error:
            self.assertEqual(error.code, 404)
            self.assertEqual(json.loads(error.read()), {"error": "run not found"})
            error.close()
        else:
            self.fail("unknown run event listing should return HTTP 404")

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
