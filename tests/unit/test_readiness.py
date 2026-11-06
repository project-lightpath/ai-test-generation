import pathlib
import sys
import unittest
from tempfile import TemporaryDirectory

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.adapters.persistence.sqlite_run_state import SQLiteRunStateStore  # noqa: E402
from atg.application.admission import AdmissionRequest, AdmissionService, CheckResult  # noqa: E402
from atg.application.readiness import PlatformReadinessService  # noqa: E402


class FixedReadinessCheck:
    def __init__(self, status="pass", capability=None):
        self.status = status
        self.capability = capability

    def evaluate(self, request):
        return CheckResult(
            name="execution-provider",
            category="execution",
            required_check=False,
            status=self.status,
            capability=self.capability,
            limitations=({"code": "local-only", "description": "No live provider configured."},) if self.status == "warn" else (),
        )


class ReadinessTests(unittest.TestCase):
    def test_readiness_ids_are_unique(self):
        service = PlatformReadinessService((FixedReadinessCheck("pass", "tekton"),))
        self.assertNotEqual(service.evaluate().readiness_id, service.evaluate().readiness_id)
    def test_readiness_records_version_identity_timestamp_and_limitations(self):
        result = PlatformReadinessService(
            (FixedReadinessCheck("warn", "tekton"),),
            service_id="atg-test",
            service_version="1.2.3",
            limitations=({"code": "development", "description": "Development readiness profile."},),
        ).evaluate()
        document = result.to_document()
        self.assertEqual(document["status"], "degraded")
        self.assertEqual(document["service"]["version"], "1.2.3")
        self.assertEqual(document["checks"][0]["checked_at"], document["evaluated_at"])
        self.assertEqual(document["checks"][0]["component"]["id"], "atg-test")
        self.assertEqual(document["checks"][0]["limitations"][0]["code"], "local-only")
        self.assertEqual(document["limitations"][0]["code"], "development")

    def test_degraded_readiness_blocks_only_requested_capability(self):
        readiness = PlatformReadinessService((FixedReadinessCheck("warn", "tekton"),))
        with TemporaryDirectory() as directory:
            store = SQLiteRunStateStore(pathlib.Path(directory) / "state.sqlite3")
            service = AdmissionService(store, (), readiness=readiness)
            admitted = service.admit(AdmissionRequest(
                "request-1", "run-1", "2026-11-05T16:00:00Z", "key-1", requested_capabilities=("java",)
            ))
            blocked = service.admit(AdmissionRequest(
                "request-2", "run-2", "2026-11-05T16:00:00Z", "key-2", requested_capabilities=("tekton",)
            ))
            self.assertEqual(admitted.status, "admitted")
            self.assertEqual(blocked.status, "blocked")
            self.assertIn("Requested degraded capabilities", blocked.reason)
            store.close()


if __name__ == "__main__":
    unittest.main()
