import pathlib
import sys
import unittest
import json
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.adapters.persistence.sqlite_run_state import SQLiteRunStateStore  # noqa: E402
from atg.application.admission import AdmissionConflictError, AdmissionRequest, AdmissionService, CheckResult  # noqa: E402
from atg.domain.run_state import InvalidTransitionError  # noqa: E402
from atg.ports.evidence import EvidencePublicationBlockedError, GuardedEvidencePublisher  # noqa: E402
from atg.adapters.failure_register import FailureRegisterCheck  # noqa: E402
from jsonschema import Draft202012Validator, FormatChecker, RefResolver  # noqa: E402


CONTRACT_ROOT = pathlib.Path(__file__).parents[2] / "contracts" / "v1"


def validate_document(document, schema_name):
    """Validate a generated document against a repository contract schema."""
    schemas = {path.name: json.loads(path.read_text()) for path in CONTRACT_ROOT.glob("*.schema.json")}
    schema = schemas[f"{schema_name}.schema.json"]
    store = {**schemas, schemas["common.schema.json"]["$id"]: schemas["common.schema.json"]}
    resolver = RefResolver.from_schema(schema, store=store)
    Draft202012Validator(schema, resolver=resolver, format_checker=FormatChecker()).validate(document)


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
    def test_admission_requires_checks_or_readiness(self):
        with self.assertRaises(ValueError):
            AdmissionService(object(), ())

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
            store.close()

    def test_invalid_check_result_is_rejected(self):
        with self.assertRaises(ValueError):
            CheckResult("bad", True, "unknown")

    def test_terminal_record_and_evidence_guard_are_explicit(self):
        with TemporaryDirectory() as directory:
            store = SQLiteRunStateStore(pathlib.Path(directory) / "state.sqlite3")
            result = AdmissionService(store, (FixedCheck("fail", detail="blocked"),)).admit(request())
            self.assertEqual(result.terminal_document()["contract"], "atg.run_terminal")
            terminal = store.get_terminal("run-1")
            self.assertEqual(terminal, result.terminal_document())
            self.assertEqual(terminal["reason"], "blocked")
            validate_document(terminal, "run-terminal")
            publisher = GuardedEvidencePublisher(store)
            with self.assertRaises(EvidencePublicationBlockedError):
                publisher.publish("run-1", {"kind": "execution"})
            self.assertEqual(publisher.records, {})
            store.close()

    def test_failure_register_fixture_produces_deterministic_check(self):
        fixture = pathlib.Path(__file__).parents[2] / "contracts" / "failure-register" / "v1" / "cases.json"
        check = FailureRegisterCheck(fixture, failed_case_ids=("FR-ADMISSION-001",))
        result = check.evaluate(None)
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.evidence_refs, ("atg.failure_register:1.0", "FR-ADMISSION-001"))

        with self.assertRaises(ValueError):
            FailureRegisterCheck(fixture, failed_case_ids=("FR-UNKNOWN-001",))

    def test_optional_failure_does_not_block_admission(self):
        with TemporaryDirectory() as directory:
            store = SQLiteRunStateStore(pathlib.Path(directory) / "state.sqlite3")
            result = AdmissionService(store, (FixedCheck("fail", required=False),)).admit(request())
            self.assertEqual(result.status, "admitted")
            self.assertEqual(store.get_run("run-1").status, "admitted")
            store.close()

    def test_same_request_is_idempotent_and_different_payload_conflicts(self):
        with TemporaryDirectory() as directory:
            store = SQLiteRunStateStore(pathlib.Path(directory) / "state.sqlite3")
            service = AdmissionService(store, (FixedCheck(),))
            first = service.admit(request())
            duplicate = service.admit(request())
            self.assertEqual(first, duplicate)
            self.assertEqual(store.get_run("run-1").revision, 1)
            alternate_key = service.admit(request(key="key-2"))
            self.assertEqual(first, alternate_key)
            self.assertIsNone(store.get_admission("key-2"))

            with self.assertRaises(AdmissionConflictError):
                service.admit(request(key="key-1", run_id="other-run"))
            store.close()

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
            self.assertEqual(restarted.get_terminal("run-1")["status"], "blocked")
            restarted.close()

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
