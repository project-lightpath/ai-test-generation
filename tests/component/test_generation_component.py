import json
import pathlib
import sys
import threading
import unittest
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.component.run_state_server import create_server
from atg.adapters.generation.deterministic import DeterministicValidationAdapter
from atg.adapters.repository.local import LocalRepositoryContextAdapter
from atg.domain.generation import canonical_patch
from atg.domain.repository_analysis import RepositorySubject
from atg.ports.repository import RepositoryContextRequest


class GenerationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "pyproject.toml").write_text("[project]\nname='http-gen'\n")
        (self.root / "src" / "app.py").write_text("def value():\n    return 1\n")
        (self.root / "tests" / "test_app.py").write_text("def test_value():\n    assert True\n")
        self.repository_id = "https://git.example/http-gen.git"
        self.repository_digest = LocalRepositoryContextAdapter().collect(RepositoryContextRequest(RepositorySubject(self.repository_id, "abc123"), str(self.root))).profile.scanned_content_digest
        self.validator = RecordingValidationAdapter()
        self.server = create_server(capabilities=("repository-discovery",), repository_roots={(self.repository_id, "abc123"): str(self.root)}, generation_validator=self.validator)
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
        req = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json", **(headers or {})}, method=method)
        try:
            with urlopen(req) as resp:
                return resp.status, json.load(resp)
        except HTTPError as e:
            try:
                return e.code, json.load(e)
            finally:
                e.close()

    def admit_and_analyze(self):
        body = {"contract": "atg.request", "version": "1.0", "request_id": "request-http-gen", "run_id": "run-http-gen", "submitted_at": "2026-11-09T20:00:00Z", "source": {"identity": {"kind": "vpe", "id": "vpe-1"}, "locator": "https://example.invalid/request"}, "repository": {"kind": "git-repository", "id": self.repository_id, "version": "abc123", "digest": self.repository_digest}, "requested_by": "user-1", "tenant_id": "tenant-1", "authorization_ref": "auth-1", "objective_ids": ["objective-1"], "mode": "analyze", "requested_capabilities": ["repository-discovery"]}
        from urllib.request import Request as R, urlopen as U
        import json as J
        headers = {"Content-Type": "application/json", "Idempotency-Key": "key-gen"}
        req = R(self.base + "/admissions", data=J.dumps(body).encode(), headers=headers, method="POST")
        try:
            with U(req) as resp:
                J.load(resp)
        except HTTPError as e:
            try:
                J.load(e)
            finally:
                e.close()
        analysis_body = {"analysis_id": "analysis-http-gen", "request_id": "request-http-gen", "run_id": "run-http-gen", "repository": {"kind": "git-repository", "id": self.repository_id, "version": "abc123", "digest": self.repository_digest}, "expected_revision": 1, "occurred_at": "2099-11-09T20:01:00Z", "requested_capabilities": ["repository-discovery"], "objective_refs": ["objective-1"], "supplied_evidence": [{"contract": "atg.evidence", "version": "1.0", "evidence_id": "evidence-1", "category": "repository", "subject": {"kind": "git-repository", "id": self.repository_id, "version": "abc123", "digest": self.repository_digest}, "source": {"identity": {"kind": "issue", "id": "issue-1"}, "locator": "https://issues.example/1", "retrieved_at": "2099-11-09T20:00:00Z"}, "observed_at": "2099-11-09T20:00:00Z", "claims": ["The behavior is defined."], "artifacts": [], "limitations": []}], "supplied_objectives": [{"contract": "atg.objective", "version": "1.0", "objective_id": "objective-1", "statement": "Validate behavior.", "kind": "regression", "driver_type": "bug_report", "priority": "high", "acceptance": ["Behavior is validated."], "evidence_refs": ["evidence-1"], "target_refs": ["target-handler"], "assumptions": [], "gaps": []}]}
        self.request("POST", "/analyses", analysis_body)
        status, run = self.request("GET", "/runs/run-http-gen")
        checkpoint_id = run.get("active_checkpoint_id") or "checkpoint-analysis-http-gen"
        decision = {"run_id": "run-http-gen", "expected_revision": run["revision"], "occurred_at": "2099-11-09T20:02:00Z", "decision": "approved", "decision_id": "decision-analysis-http-gen", "actor_id": "reviewer-1", "actor_role": "owner", "authorization_ref": "auth-1"}
        self.request("POST", f"/checkpoints/{checkpoint_id}/decision", decision)

    def prepare_coding(self):
        self.admit_and_analyze()
        status, run = self.request("GET", "/runs/run-http-gen")
        plan_body = {"plan_id": "plan-http-gen", "analysis_id": "analysis-http-gen", "run_id": "run-http-gen", "expected_revision": run["revision"], "occurred_at": "2099-11-09T20:03:00Z"}
        self.request("POST", "/plans", plan_body)
        status, run = self.request("GET", "/runs/run-http-gen")
        checkpoint_id = "checkpoint-plan-http-gen"
        decision = {"run_id": "run-http-gen", "expected_revision": run["revision"], "occurred_at": "2099-11-09T20:04:00Z", "decision": "approved", "decision_id": "decision-plan-http-gen", "actor_id": "reviewer-1", "actor_role": "owner", "authorization_ref": "auth-1"}
        self.request("POST", f"/checkpoints/{checkpoint_id}/decision", decision)
        status, run = self.request("GET", "/runs/run-http-gen")
        return run

    def test_generation_http_creates_and_retrieves(self):
        run = self.prepare_coding()
        content = "def test_target_handler():\n    # target-handler\n    actual = target_handler()\n    assert actual == 42\n"
        files = {"tests/test_generated.py": content}
        body = {"generation_id": "gen-http-1", "plan_id": "plan-http-gen", "run_id": "run-http-gen", "expected_revision": run["revision"], "created_at": "2099-11-09T20:05:00Z", "patch_text": canonical_patch({}, files), "materialized_files": files, "base": {"kind": "git-tree", "id": "base-tree", "version": "abc123"}, "candidate": {"kind": "git-tree", "id": "candidate-tree", "version": "def456"}, "model": {"kind": "model", "id": "model-1", "version": "1"}, "tool": {"kind": "generation-tool", "id": "tool-1", "version": "1"}, "worker": {"kind": "worker", "id": "worker-1", "version": "1"}}
        status, doc = self.request("POST", "/generations", body, {"Idempotency-Key": "generation-http-key"})
        self.assertEqual(status, 201)
        self.assertEqual(doc["manifest"]["generation_id"], "gen-http-1")
        self.assertEqual(doc["manifest"]["status"], "verified")
        self.assertEqual(self.validator.calls, 1)
        status, duplicate = self.request("POST", "/generations", body, {"Idempotency-Key": "generation-http-key"})
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["duplicate"])
        status, fetched = self.request("GET", "/generations/gen-http-1")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["manifest"]["generation_id"], "gen-http-1")

    def test_generation_rejects_bad_path(self):
        run = self.prepare_coding()
        content = "def test_target_handler():\n    # target-handler\n    assert True\n"
        files = {"src/main/app.py": content}
        body = {"generation_id": "gen-http-2", "plan_id": "plan-http-gen", "run_id": "run-http-gen", "expected_revision": run["revision"], "created_at": "2099-11-09T20:05:00Z", "patch_text": canonical_patch({}, files), "materialized_files": files, "base": {"kind": "git-tree", "id": "base-tree", "version": "abc123"}, "candidate": {"kind": "git-tree", "id": "candidate-tree", "version": "def456"}, "model": {"kind": "model", "id": "model-1", "version": "1"}, "tool": {"kind": "generation-tool", "id": "tool-1", "version": "1"}, "worker": {"kind": "worker", "id": "worker-1", "version": "1"}}
        status, document = self.request("POST", "/generations", body, {"Idempotency-Key": "generation-http-bad-path"})
        self.assertEqual(status, 201)
        self.assertEqual(document["manifest"]["status"], "rejected")

    def test_generation_requires_approved_plan(self):
        self.request("POST", "/runs", {"run_id": "run-http-gen", "created_at": "2099-11-09T20:00:00Z"})
        content = "def test_target_handler():\n    assert value == 1\n"
        files = {"tests/test_generated.py": content}
        body = {"generation_id": "gen-http-3", "plan_id": "plan-missing", "run_id": "run-http-gen", "expected_revision": 0, "created_at": "2099-11-09T20:04:00Z", "patch_text": canonical_patch({}, files), "materialized_files": files, "base": {"kind": "git-tree", "id": "base-tree", "version": "abc123"}, "candidate": {"kind": "git-tree", "id": "candidate-tree", "version": "def456"}, "model": {"kind": "model", "id": "model-1", "version": "1"}, "tool": {"kind": "generation-tool", "id": "tool-1", "version": "1"}, "worker": {"kind": "worker", "id": "worker-1", "version": "1"}}
        status, _ = self.request("POST", "/generations", body, {"Idempotency-Key": "generation-http-missing-plan"})
        self.assertEqual(status, 409)

    def test_generation_requires_idempotency_key(self):
        run = self.prepare_coding()
        content = "def test_target_handler():\n    # target-handler\n    assert value == 1\n"
        files = {"tests/test_generated.py": content}
        body = {"generation_id": "gen-http-4", "plan_id": "plan-http-gen", "run_id": "run-http-gen", "expected_revision": run["revision"], "created_at": "2099-11-09T20:05:00Z", "patch_text": canonical_patch({}, files), "materialized_files": files, "base": {"kind": "git-tree", "id": "base-tree", "version": "abc123"}, "candidate": {"kind": "git-tree", "id": "candidate-tree", "version": "def456"}, "model": {"kind": "model", "id": "model-1", "version": "1"}, "tool": {"kind": "generation-tool", "id": "tool-1", "version": "1"}, "worker": {"kind": "worker", "id": "worker-1", "version": "1"}}
        status, document = self.request("POST", "/generations", body)
        self.assertEqual(status, 400)
        self.assertIn("Idempotency-Key", document["error"])

if __name__ == "__main__":
    unittest.main()


class RecordingValidationAdapter(DeterministicValidationAdapter):
    def __init__(self):
        self.calls = 0

    def validate(self, candidate):
        self.calls += 1
        return super().validate(candidate)
