import pathlib
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.adapters.persistence.sqlite_run_state import SQLiteRunStateStore  # noqa: E402
from atg.application.admission import AdmissionConflictError, AdmissionRequest, AdmissionService, CheckResult  # noqa: E402
from atg.domain.run_state import InvalidTransitionError  # noqa: E402


class FixedCheck:
    def __init__(self, status="pass", required=True, detail=None):
        self.status = status
        self.required = required
        self.detail = detail

    def evaluate(self, request):
        return CheckResult(
            name="fixed-check",
            category="self_verification",
            required_check=self.required,
            status=self.status,
            detail=self.detail,
        )


def request(key="key-1", run_id="run-1"):
    return AdmissionRequest(
        request_id="request-1",
        run_id=run_id,
        submitted_at="2026-11-04T18:00:00Z",
        idempotency_key=key,
        requested_capabilities=("java",),
    )


class AdmissionTests(unittest.TestCase):
    def test_required_failure_creates_terminal_blocked_run(self):
        with TemporaryDirectory() as directory:
            store = SQLiteRunStateStore(pathlib.Path(directory) / "state.sqlite3")
            result = AdmissionService(store, (FixedCheck("fail", detail="registry unavailable"),)).admit(request())

            self.assertEqual(result.status, "blocked")
            self.assertEqual(store.get_run("run-1").status, "blocked")
            self.assertEqual(store.get_run("run-1").terminal_reason, "registry unavailable")
            with self.assertRaises(InvalidTransitionError):
                from atg.domain.run_state import RunEvent
                store.apply(RunEvent("analysis", "run-1", "analysis_started", 1, "2026-11-04T18:00:01Z"))

            self.assertEqual([event.event_type for event in store.events("run-1")], ["admission_blocked"])

    def test_invalid_check_result_is_rejected(self):
        with self.assertRaises(ValueError):
            CheckResult("bad", True, "unknown")

    def test_optional_failure_does_not_block_admission(self):
        with TemporaryDirectory() as directory:
            store = SQLiteRunStateStore(pathlib.Path(directory) / "state.sqlite3")
            result = AdmissionService(store, (FixedCheck("fail", required=False),)).admit(request())
            self.assertEqual(result.status, "admitted")
            self.assertEqual(store.get_run("run-1").status, "admitted")

    def test_same_request_is_idempotent_and_different_payload_conflicts(self):
        with TemporaryDirectory() as directory:
            store = SQLiteRunStateStore(pathlib.Path(directory) / "state.sqlite3")
            service = AdmissionService(store, (FixedCheck(),))
            first = service.admit(request())
            duplicate = service.admit(request())
            self.assertEqual(first, duplicate)
            self.assertEqual(store.get_run("run-1").revision, 1)

            with self.assertRaises(AdmissionConflictError):
                service.admit(request(key="key-1", run_id="other-run"))

    def test_blocked_admission_survives_restart(self):
        with TemporaryDirectory() as directory:
            database = pathlib.Path(directory) / "state.sqlite3"
            store = SQLiteRunStateStore(database)
            service = AdmissionService(store, (FixedCheck("not_run", detail="provider unavailable"),))
            result = service.admit(request())
            store.close()

            restarted = SQLiteRunStateStore(database)
            self.assertEqual(restarted.get_admission("key-1"), result)
            self.assertEqual(restarted.replay("run-1").status, "blocked")

    def test_two_store_instances_return_one_durable_admission(self):
        with TemporaryDirectory() as directory:
            database = pathlib.Path(directory) / "state.sqlite3"
            first = SQLiteRunStateStore(database)
            second = SQLiteRunStateStore(database)
            services = [AdmissionService(first, (FixedCheck(),)), AdmissionService(second, (FixedCheck(),))]
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda service: service.admit(request()), services))
            self.assertEqual(results[0], results[1])
            self.assertEqual(first.get_run("run-1").revision, 1)
            first.close()
            second.close()


if __name__ == "__main__":
    unittest.main()
