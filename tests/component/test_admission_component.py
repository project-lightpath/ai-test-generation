import http.client
import json
import pathlib
import sys
import threading
import unittest
from jsonschema import Draft202012Validator, FormatChecker, RefResolver
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.component.run_state_server import MAX_REQUEST_BYTES, create_server  # noqa: E402


CONTRACT_ROOT = pathlib.Path(__file__).parents[2] / "contracts" / "v1"


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

    def assert_admission_document(self, document):
        """Validate an HTTP admission response against the public schema."""
        schemas = {path.name: json.loads(path.read_text()) for path in CONTRACT_ROOT.glob("*.schema.json")}
        schema = schemas["run-admission.schema.json"]
        store = {**schemas, schemas["common.schema.json"]["$id"]: schemas["common.schema.json"]}
        resolver = RefResolver.from_schema(schema, store=store)
        Draft202012Validator(schema, resolver=resolver, format_checker=FormatChecker()).validate(document)

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
        self.assert_admission_document(first)
        self.assertEqual(first["status"], "admitted")
        self.assertTrue(first["platform_readiness_id"].startswith("readiness-"))
        self.assertEqual(first["identities"]["contract"]["kind"], "atg.request")
        self.assertEqual(first["identities"]["repository"]["id"], "repo-1")
        self.assertIn("atg.failure_register:1.0", first["failure_register_refs"])
        status, duplicate = self.post(body)
        self.assertEqual(status, 200)
        self.assertEqual(duplicate, first)
        self.assert_admission_document(duplicate)
        status, alternate_key = self.post(body, idempotency_key="idem-http-alternate")
        self.assertEqual(status, 200)
        self.assertEqual(alternate_key, first)

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
        self.assertEqual(body, {"error": "admission requires request, requester, tenant, and authorization identities"})

    def test_admission_rejects_missing_structural_fields(self):
        base = {
            "contract": "atg.request", "version": "1.0", "request_id": "request-missing",
            "run_id": "run-missing", "submitted_at": "2026-11-04T18:00:00Z",
            "requested_by": "user-1", "tenant_id": "tenant-1", "authorization_ref": "auth-1",
            "source": {"identity": {"kind": "vpe", "id": "vpe-1"}, "locator": "https://example.invalid"},
            "repository": {"kind": "git", "id": "repo-1"}, "objective_ids": ["objective-1"],
        }
        for field in ("source", "repository", "objective_ids"):
            with self.subTest(field=field):
                request = dict(base)
                request.pop(field)
                status, body = self.post(request, idempotency_key=f"missing-{field}")
                self.assertEqual(status, 400)
                self.assertEqual(body, {"error": "source and repository must be objects"} if field in {"source", "repository"} else {"error": "objective_ids must be a non-empty list of strings"})

    def test_admission_rejects_conflict_and_version(self):
        body = {"contract": "atg.request", "version": "1.0", "request_id": "request-conflict", "run_id": "run-conflict", "submitted_at": "2026-11-04T18:00:00Z", "source": {"identity": {"kind": "vpe", "id": "vpe-1"}, "locator": "https://example.invalid"}, "repository": {"kind": "git", "id": "repo-1"}, "objective_ids": ["objective-1"], "requested_by": "user-1", "tenant_id": "tenant-1", "authorization_ref": "auth-1"}
        self.assertEqual(self.post(body, idempotency_key="conflict")[0], 201)
        conflict = dict(body, run_id="run-other")
        status, response = self.post(conflict, idempotency_key="conflict")
        self.assertEqual((status, response), (409, {"error": "idempotency key is bound to a different request"}))
        status, response = self.post({"contract": "atg.request", "version": "2.0"})
        self.assertEqual((status, response), (400, {"error": "admission requires atg.request version 1.0"}))

    def test_admission_rejects_oversized_body(self):
        connection = http.client.HTTPConnection(*self.server.server_address)
        connection.putrequest("POST", "/admissions")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Idempotency-Key", "oversized")
        connection.putheader("Content-Length", str(MAX_REQUEST_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        self.assertEqual(json.loads(response.read()), {"error": f"request body exceeds {MAX_REQUEST_BYTES} bytes"})
        connection.close()

    def test_admission_rejects_invalid_identity_properties(self):
        base = {"contract": "atg.request", "version": "1.0", "request_id": "request-identity", "run_id": "run-identity", "submitted_at": "2026-11-04T18:00:00Z", "source": {"identity": {"kind": "vpe", "id": "vpe-1"}, "locator": "https://example.invalid"}, "repository": {"kind": "git", "id": "repo-1"}, "objective_ids": ["objective-1"], "requested_by": "user-1", "tenant_id": "tenant-1", "authorization_ref": "auth-1"}
        cases = (
            ("repository-extra", "repository", {"kind": "git", "id": "repo-1", "extra": "value"}, "repository has unsupported identity fields: extra"),
            ("repository-version", "repository", {"kind": "git", "id": "repo-1", "version": ""}, "repository.version must be a non-empty string"),
            ("repository-digest", "repository", {"kind": "git", "id": "repo-1", "digest": "sha256:not-hex"}, "repository.digest must match algorithm:hex with at least 8 hex characters"),
            ("source-extra", "source", {"kind": "vpe", "id": "vpe-1", "extra": "value"}, "source.identity has unsupported identity fields: extra"),
            ("source-version", "source", {"kind": "vpe", "id": "vpe-1", "version": 1}, "source.identity.version must be a non-empty string"),
            ("source-digest", "source", {"kind": "vpe", "id": "vpe-1", "digest": "invalid"}, "source.identity.digest must match algorithm:hex with at least 8 hex characters"),
        )
        for name, location, identity, error in cases:
            with self.subTest(name=name):
                body = dict(base, request_id=f"request-{name}", run_id=f"run-{name}")
                if location == "source":
                    body["source"] = {"identity": identity, "locator": "https://example.invalid"}
                else:
                    body[location] = identity
                status, response = self.post(body, idempotency_key=f"identity-{name}")
                self.assertEqual((status, response), (400, {"error": error}))

    def test_admission_rejects_request_contract_violations(self):
        base = {"contract": "atg.request", "version": "1.0", "request_id": "request-contract", "run_id": "run-contract", "submitted_at": "2026-11-04T18:00:00Z", "source": {"identity": {"kind": "vpe", "id": "vpe-1"}, "locator": "https://example.invalid"}, "repository": {"kind": "git", "id": "repo-1"}, "objective_ids": ["objective-1"], "requested_by": "user-1", "tenant_id": "tenant-1", "authorization_ref": "auth-1"}
        cases = (
            ("mode", {"mode": 12345}, "mode must be analyze or full"),
            ("constraints", {"constraints": "not-a-list"}, "constraints must be a list of strings"),
            ("capability", {"requested_capabilities": ["!!!bad id!!!"]}, "requested_capability must be a valid v1 identifier"),
            ("duplicate-capability", {"requested_capabilities": ["java", "java"]}, "requested_capabilities must contain unique identifiers"),
            ("duplicate-objective", {"objective_ids": ["objective-1", "objective-1"]}, "objective_ids must contain unique identifiers"),
            ("source-extra", {"source": {"identity": {"kind": "vpe", "id": "vpe-1"}, "locator": "https://example.invalid", "banana": True}}, "source has unsupported fields: banana"),
            ("source-time", {"source": {"identity": {"kind": "vpe", "id": "vpe-1"}, "locator": "https://example.invalid", "retrieved_at": "invalid"}}, "source.retrieved_at must be a timezone-aware RFC 3339 timestamp"),
            ("source-digest", {"source": {"identity": {"kind": "vpe", "id": "vpe-1"}, "locator": "https://example.invalid", "content_digest": "invalid"}}, "source.content_digest must match algorithm:hex with at least 8 hex characters"),
        )
        for name, changes, error in cases:
            with self.subTest(name=name):
                body = dict(base, request_id=f"request-{name}", run_id=f"run-{name}", **changes)
                status, response = self.post(body, idempotency_key=f"contract-{name}")
                self.assertEqual((status, response), (400, {"error": error}))

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
