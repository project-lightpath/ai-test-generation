import json
import pathlib
import sys
import threading
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.component.run_state_server import create_server  # noqa: E402
from atg.adapters.repository.local import LocalRepositoryContextAdapter  # noqa: E402
from atg.domain.repository_analysis import RepositorySubject  # noqa: E402
from atg.ports.repository import RepositoryContextRequest  # noqa: E402


class AnalysisHTTPComponentTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "pyproject.toml").write_text("[project]\nname = 'http-analysis'\n")
        (self.root / "src" / "app.py").write_text("def value():\n    return 1\n")
        (self.root / "tests" / "test_app.py").write_text("def test_value():\n    assert True\n")
        self.repository_id = "https://git.example/http-analysis.git"
        self.repository_digest = LocalRepositoryContextAdapter().collect(RepositoryContextRequest(
            RepositorySubject(self.repository_id, "abc123"), str(self.root)
        )).profile.scanned_content_digest
        self.server = create_server(
            capabilities=("repository-discovery",),
            repository_roots={(self.repository_id, "abc123"): str(self.root)},
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server.store.close()
        self.directory.cleanup()

    def request(self, method, path, body=None, headers=None):
        """Issue one JSON request and return its status and document."""
        request = Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
            method=method,
        )
        try:
            with urlopen(request) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            try:
                return error.code, json.load(error)
            finally:
                error.close()

    def admit(self):
        """Admit the Analyze-only HTTP fixture request."""
        body = {
            "contract": "atg.request",
            "version": "1.0",
            "request_id": "request-http-analysis",
            "run_id": "run-http-analysis",
            "submitted_at": "2026-11-09T20:00:00Z",
            "source": {"identity": {"kind": "vpe", "id": "vpe-1"}, "locator": "https://example.invalid/request"},
            "repository": {"kind": "git-repository", "id": self.repository_id, "version": "abc123", "digest": self.repository_digest},
            "requested_by": "user-1",
            "tenant_id": "tenant-1",
            "authorization_ref": "authorization-1",
            "mode": "analyze",
            "requested_capabilities": ["repository-discovery"],
        }
        status, document = self.request("POST", "/admissions", body, {"Idempotency-Key": "analysis-http-admission"})
        self.assertEqual(status, 201)
        return document

    def analysis_body(self):
        """Return a valid development Analyze HTTP request."""
        return {
            "analysis_id": "analysis-http-1",
            "request_id": "request-http-analysis",
            "run_id": "run-http-analysis",
            "repository": {"kind": "git-repository", "id": self.repository_id, "version": "abc123", "digest": self.repository_digest},
            "expected_revision": 1,
            "occurred_at": "2099-11-09T20:01:00Z",
            "requested_capabilities": ["repository-discovery"],
        }

    def test_analysis_start_retrieval_and_duplicate_are_durable(self):
        self.admit()
        status, created = self.request("POST", "/analyses", self.analysis_body())
        self.assertEqual(status, 201)
        self.assertFalse(created["duplicate"])
        self.assertEqual(created["manifest"]["outcome"], "needs_human_review")
        self.assertEqual(created["checkpoint"]["kind"], "analysis_review")
        self.assertTrue(created["artifacts"])
        self.assertTrue(all("content" not in artifact for artifact in created["artifacts"]))
        identity_kinds = {identity["kind"] for identity in created["provenance"]["identities"]}
        self.assertTrue({"git-repository", "vpe", "tenant", "authorization"}.issubset(identity_kinds))

        status, fetched = self.request("GET", "/analyses/analysis-http-1")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["manifest"], created["manifest"])

        status, duplicate = self.request("POST", "/analyses", self.analysis_body())
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["duplicate"])

    def test_analysis_rejects_unmaterialized_repository(self):
        self.admit()
        body = self.analysis_body()
        body["repository"] = {"kind": "git-repository", "id": "https://git.example/missing.git", "version": "abc123"}
        status, document = self.request("POST", "/analyses", body)
        self.assertEqual(status, 400)
        self.assertEqual(document, {"error": "repository is not materialized for local analysis"})

    def test_analysis_rejects_invalid_supplied_contract_without_state_change(self):
        self.admit()
        body = self.analysis_body()
        body["supplied_evidence"] = [{"evidence_id": "evidence-invalid"}]
        status, document = self.request("POST", "/analyses", body)
        self.assertEqual(status, 400)
        self.assertEqual(document, {"error": "invalid evidence contract document"})
        status, state = self.request("GET", "/runs/run-http-analysis")
        self.assertEqual(status, 200)
        self.assertEqual(state["status"], "admitted")
        self.assertEqual(state["revision"], 1)

    def test_analysis_get_unknown_id_returns_not_found(self):
        status, document = self.request("GET", "/analyses/missing-analysis")
        self.assertEqual(status, 404)
        self.assertEqual(document, {"error": "analysis not found"})

    def test_plan_and_checkpoint_unknown_ids_return_not_found(self):
        status, document = self.request("GET", "/plans/missing-plan")
        self.assertEqual(status, 404)
        self.assertEqual(document, {"error": "plan not found"})
        status, document = self.request("GET", "/checkpoints/missing-checkpoint")
        self.assertEqual(status, 404)
        self.assertEqual(document, {"error": "checkpoint not found"})
        status, document = self.request("GET", "/feedback/missing-feedback")
        self.assertEqual(status, 404)
        self.assertEqual(document, {"error": "feedback not found"})

    def test_plan_submission_rejects_malformed_body(self):
        status, document = self.request("POST", "/plans", {"plan_id": "plan-1"})
        self.assertEqual(status, 400)
        self.assertIn("identifier", document["error"])

    def test_analysis_reuses_id_only_for_identical_inputs(self):
        self.admit()
        status, _ = self.request("POST", "/analyses", self.analysis_body())
        self.assertEqual(status, 201)
        body = self.analysis_body()
        body["expected_revision"] = 2
        status, document = self.request("POST", "/analyses", body)
        self.assertEqual(status, 409)
        self.assertIn("different inputs", document["error"])

    def test_analysis_requires_prior_admission(self):
        status, document = self.request("POST", "/analyses", self.analysis_body())
        self.assertEqual(status, 409)
        self.assertEqual(document, {"error": "analysis request does not match the admitted run"})

    def test_analysis_rejects_malformed_json(self):
        request = Request(
            self.base + "/analyses",
            data=b"not-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as malformed:
            urlopen(request)
        self.assertEqual(malformed.exception.code, 400)
        malformed.exception.close()


if __name__ == "__main__":
    unittest.main()
