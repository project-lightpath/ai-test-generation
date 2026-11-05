import json
import pathlib
import sys
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.component.run_state_server import create_server  # noqa: E402


class AdmissionComponentTests(unittest.TestCase):
    def setUp(self):
        self.server = create_server(capabilities=("java",))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server.store.close()

    def post(self, body, idempotency_key="idem-http"):
        headers = {"Content-Type": "application/json"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(
            self.base + "/admissions",
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            try:
                return error.code, json.load(error)
            finally:
                error.close()

    def test_admission_is_idempotent_and_unsupported_capability_blocks(self):
        body = {
            "contract": "atg.request",
            "version": "1.0",
            "request_id": "request-http",
            "run_id": "run-http",
            "submitted_at": "2026-11-04T18:00:00Z",
            "source": {"identity": {"kind": "vpe", "id": "vpe-1"}, "locator": "https://example.invalid/request"},
            "repository": {"kind": "git", "id": "repo-1"},
            "objective_ids": ["objective-1"],
            "requested_by": "user-1",
            "tenant_id": "tenant-1",
            "authorization_ref": "authorization-1",
            "policy_decision": {"kind": "policy-decision", "id": "decision-1", "version": "1"},
            "environment": {"kind": "atg-platform", "id": "development", "version": "1"},
            "requested_capabilities": ["java"],
        }
        status, first = self.post(body)
        self.assertEqual(status, 201)
        self.assertEqual(first["status"], "admitted")
        self.assertEqual(first["platform_readiness_id"], "readiness-current")
        self.assertEqual(first["identities"]["contract"]["kind"], "atg.request")
        self.assertEqual(first["identities"]["repository"]["id"], "repo-1")
        self.assertIn("atg.failure_register:1.0", first["failure_register_refs"])
        status, duplicate = self.post(body)
        self.assertEqual(status, 200)
        self.assertEqual(duplicate, first)

        blocked = dict(body, request_id="request-blocked", run_id="run-blocked", requested_capabilities=["go"])
        status, result = self.post(blocked, idempotency_key="idem-blocked")
        self.assertEqual(status, 201)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("Unsupported capabilities", result["reason"])
        self.assertEqual(self.server.store.get_run("run-blocked").status, "blocked")

    def test_readiness_is_separate_from_request_admission(self):
        with urlopen(self.base + "/readiness") as response:
            self.assertEqual(response.status, 200)
            document = json.load(response)
        self.assertEqual(document["contract"], "atg.platform_readiness")
        self.assertEqual(document["status"], "ready")

    def test_admission_rejects_incomplete_canonical_request(self):
        status, body = self.post({"contract": "atg.request", "version": "1.0", "request_id": "incomplete"})
        self.assertEqual(status, 400)
        self.assertIn("admission requires", body["error"])

    def test_admission_requires_transport_idempotency_header(self):
        status, body = self.post({"contract": "atg.request", "version": "1.0"}, idempotency_key=None)
        self.assertEqual(status, 400)
        self.assertIn("Idempotency-Key", body["error"])

    def test_admission_rejects_transport_metadata_in_request_document(self):
        status, body = self.post({
            "contract": "atg.request",
            "version": "1.0",
            "idempotency_key": "must-be-a-header",
        })
        self.assertEqual(status, 400)
        self.assertIn("unsupported request fields", body["error"])


if __name__ == "__main__":
    unittest.main()
