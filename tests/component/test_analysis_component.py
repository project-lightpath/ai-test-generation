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
        self.server = create_server(repository_roots={self.repository_id: str(self.root)})
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
            "repository": {"kind": "git-repository", "id": self.repository_id, "version": "abc123"},
            "requested_by": "user-1",
            "tenant_id": "tenant-1",
            "authorization_ref": "authorization-1",
            "mode": "analyze",
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
            "repository": {"kind": "git-repository", "id": self.repository_id, "version": "abc123"},
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


if __name__ == "__main__":
    unittest.main()
