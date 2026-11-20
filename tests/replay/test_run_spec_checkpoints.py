"""Contract checks for versioned run specs and immutable stage evidence."""

import argparse
from datetime import datetime
import io
import json
import pathlib
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "experiments" / "harness"))
import run_comparison as comparison  # noqa: E402


class RunSpecCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.spec = {
            "schema": comparison.RUN_SPEC_SCHEMA,
            "profile": "spring-transfer",
            "candidates": ["langgraph", "aider"],
            "model_id": comparison.MODEL,
            "endpoint_id": comparison.ENDPOINT,
            "external_candidates": [],
            "timeouts_seconds": {
                "analysis": 360, "plan": 360, "code": 600, "execute": 3900,
            },
        }

    def tearDown(self):
        self.temporary.cleanup()

    def write_spec(self):
        path = self.root / "spec.json"
        path.write_text(json.dumps(self.spec))
        return path

    def test_run_spec_preserves_candidate_order_and_budgets(self):
        record = comparison.load_run_spec(self.write_spec())
        self.assertEqual(record["candidates"], ["langgraph", "aider"])
        args = argparse.Namespace(run_spec_data=record)
        self.assertEqual(
            comparison.run_timeout(args, "aider", "code", comparison.get_profile("spring-transfer")), 600)

    def test_run_spec_rejects_unsupported_model_and_unknown_fields(self):
        self.spec["model_id"] = "unvalidated-model"
        with self.assertRaisesRegex(ValueError, "pinned runtime"):
            comparison.load_run_spec(self.write_spec())
        self.spec["model_id"] = comparison.MODEL
        self.spec["api_key"] = "should-not-be-accepted"
        with self.assertRaisesRegex(ValueError, "fields"):
            comparison.load_run_spec(self.write_spec())

    def test_run_spec_rejects_invalid_timeouts_and_duplicate_candidates(self):
        self.spec["timeouts_seconds"]["code"] = True
        with self.assertRaisesRegex(ValueError, "bounded stage timeouts"):
            comparison.load_run_spec(self.write_spec())
        self.spec["timeouts_seconds"]["code"] = 600
        self.spec["candidates"] = ["aider", "aider"]
        with self.assertRaisesRegex(ValueError, "unique known candidates"):
            comparison.load_run_spec(self.write_spec())

    def test_external_adapter_contract_is_pinned_and_key_free(self):
        image = "example.test/agent@sha256:" + "a" * 64
        self.spec["external_candidates"] = [{
            "name": "candidate-five", "version": "1.0",
            "image": image, "image_id": "sha256:" + "b" * 64,
        }]
        self.spec["candidates"] = ["candidate-five"]
        record = comparison.load_run_spec(self.write_spec())
        args = argparse.Namespace(
            run_spec_data=record, profile="spring-transfer",
            fixture_root=self.root / "fixture", output_root=self.root / "output",
            maven_cache=self.root / "maven-cache",
            pydantic_venv=None, langgraph_venv=None, claude_runtime_dir=None,
        )
        args.fixture_root.mkdir()
        args.maven_cache.mkdir()
        folder = args.output_root / "candidate-five"
        command = comparison.adapter_command(args, "candidate-five", folder, "analysis")
        self.assertIn(image, command)
        self.assertIn("/adapter", command)
        self.assertIn("ENMAAS_API_KEY", command)
        self.assertNotIn("unit-test-secret", " ".join(command))
        self.assertIn(str(args.fixture_root) + ":/fixture:ro,Z", command)
        self.assertIn("--memory=2g", command)
        self.assertIn("--read-only", command)
        self.assertIn("/tmp:rw,nosuid,size=64m", command)
        ids = {
            comparison.PYTHON_IMAGE: comparison.PYTHON_IMAGE_ID,
            comparison.get_profile("spring-transfer").maven_image:
                comparison.get_profile("spring-transfer").maven_image_id,
            image: "b" * 64,
        }
        profile = comparison.get_profile("spring-transfer")
        with patch.object(comparison, "tree_digest",
                          return_value=profile.tree_sha256), \
                patch.object(comparison, "image_id", side_effect=ids.get), \
                patch.object(comparison, "offline_maven_readiness") as readiness:
            versions = comparison.preflight(args, ["candidate-five"])
        readiness.assert_called_once_with(args, profile)
        self.assertEqual(versions, {"candidate-five": "1.0"})

    def test_external_adapter_rejects_unpinned_image(self):
        self.spec["external_candidates"] = [{
            "name": "candidate-five", "version": "1.0",
            "image": "example.test/agent:latest", "image_id": "sha256:" + "b" * 64,
        }]
        with self.assertRaisesRegex(ValueError, "digest-pinned"):
            comparison.load_run_spec(self.write_spec())

    def test_prepare_and_summary_share_unique_run_identity_and_order(self):
        fixture = self.root / "fixture"
        for relative in comparison.QUARKUS.source_paths:
            source = fixture / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("fixture")
        args = argparse.Namespace(
            profile="quarkus", candidate=["pydantic-ai", "aider"],
            fixture_root=fixture, maven_cache=self.root / "cache",
            output_root=self.root / "run", check_only=False,
            api_key_stdin=True, run_spec=None,
        )
        parser = argparse.ArgumentParser()
        with patch.object(comparison, "parse_arguments", return_value=(args, parser)), \
                patch.object(comparison, "preflight", return_value={}), \
                patch.object(comparison, "output_mount_capacity", return_value=1024), \
                patch.object(comparison, "checkout_identity", return_value={}), \
                patch.object(comparison, "save_dependencies"), \
                patch.object(comparison, "generate", return_value=(
                    self.root / "unused.java", "src/test/java/UnusedTest.java")), \
                patch.object(comparison, "evaluate", return_value="discriminates"), \
                patch.object(comparison, "scan_evidence_for_key", return_value=10), \
                patch.object(comparison.sys, "stdin", io.StringIO("unit-test-secret\n")):
            self.assertEqual(comparison.main(), 0)
        prepare = json.loads((args.output_root / "prepare.json").read_text())
        summary = json.loads((args.output_root / "summary.json").read_text())
        for field in ("run_id", "started_at", "candidate_order"):
            self.assertEqual(prepare[field], summary[field])
        self.assertEqual(uuid.UUID(summary["run_id"]).version, 4)
        self.assertEqual(summary["candidate_order"], ["pydantic-ai", "aider"])
        self.assertIsNotNone(datetime.fromisoformat(summary["started_at"].replace("Z", "+00:00")).tzinfo)

    def test_all_generator_commands_keep_cid_outside_writable_mount(self):
        folder = self.root / "candidate"
        args = argparse.Namespace(
            profile="spring-transfer", fixture_root=self.root / "fixture",
            pydantic_venv=self.root / "pydantic", langgraph_venv=self.root / "langgraph",
            claude_runtime_dir=self.root / "claude", run_spec_data=None)
        workspace = folder / "workspace"
        commands = [
            comparison.aider_command(workspace, folder, comparison.get_profile("spring-transfer")),
            comparison.claude_command(args, workspace, folder,
                                      comparison.get_profile("spring-transfer")),
            comparison.adapter_command(args, "pydantic-ai", folder, "analysis"),
            comparison.adapter_command(args, "langgraph", folder, "analysis"),
        ]
        for command in commands:
            with self.subTest(command=command[0:5]):
                cid = pathlib.Path(command[command.index("--cidfile") + 1])
                self.assertEqual(cid, comparison.container_id_path(folder))
                self.assertNotIn(folder, cid.parents)

    def test_container_cleanup_rejects_linked_or_invalid_cid(self):
        folder = self.root / "candidate"
        cid = comparison.container_id_path(folder)
        cid.parent.mkdir(parents=True)
        private = self.root / "private.txt"
        private.write_text("a" * 64)
        cid.symlink_to(private)
        with patch.object(comparison.subprocess, "call") as remove:
            with self.assertRaisesRegex(ValueError, "linked or nonregular"):
                comparison.remove_container(folder)
        remove.assert_not_called()
        self.assertEqual(private.read_text(), "a" * 64)
        cid.unlink()
        cid.write_text("not-a-container-id")
        with patch.object(comparison.subprocess, "call") as remove:
            with self.assertRaisesRegex(ValueError, "invalid content"):
                comparison.remove_container(folder)
        remove.assert_not_called()
        self.assertFalse(cid.exists())

    def test_container_cleanup_uses_valid_host_controlled_cid(self):
        folder = self.root / "candidate"
        cid = comparison.container_id_path(folder)
        cid.parent.mkdir(parents=True)
        cid.write_text("b" * 64)
        with patch.object(comparison.subprocess, "call") as remove:
            comparison.remove_container(folder)
        remove.assert_called_once()
        self.assertEqual(remove.call_args.args[0], ["podman", "rm", "-f", "b" * 64])
        self.assertFalse(cid.exists())

    def test_execution_timeout_cannot_be_accepted_from_partial_report(self):
        cases = (
            ("timeout", None, "inconclusive", "blocked_evaluation", "blocked"),
            ("output_limit", None, "inconclusive", "blocked_evaluation", "blocked"),
            ("completed", 0, "inconclusive", "blocked_evaluation", "blocked"),
            ("failed", 2, "inconclusive", "inconclusive", "completed"),
            ("failed", 1, "inconclusive", "blocked_evaluation", "blocked"),
        )
        for index, (status, code, reported, expected, checkpoint_status) in enumerate(cases):
            with self.subTest(status=status, code=code):
                folder = self.root / ("candidate-" + str(index))
                (folder / "generated").mkdir(parents=True)
                (folder / "generated/invocation.json").write_text("{}")
                args = argparse.Namespace(
                    profile="quarkus", fixture_root=self.root / "fixture",
                    maven_cache=self.root / "cache", output_root=self.root,
                    run_spec_data=None)
                test = folder / "generated/ExampleTest.java"
                test.write_text("class ExampleTest {}")
                generation = folder / "generation.json"
                generation.write_text("{}")
                comparison.write_stage_checkpoint(args, "pydantic-ai", folder, "code",
                                                  "completed", [test, generation])
                def fake_evaluator(*unused_args):
                    (folder / "evaluation").mkdir()
                    (folder / "evaluation/evaluation.json").write_text(json.dumps({"result": reported}))
                    (folder / "evaluation-run.log").write_text("evaluator log")
                    return status, code, 1.25
                with patch.object(comparison, "run_bounded", side_effect=fake_evaluator):
                    actual = comparison.evaluate(
                        args, "pydantic-ai", folder, self.root / "unused.java",
                        "src/test/java/UnusedTest.java", "unit-test-secret")
                self.assertEqual(actual, expected)
                checkpoint = json.loads((self.root / "checkpoints/pydantic-ai/execute.json").read_text())
                self.assertEqual(checkpoint["status"], checkpoint_status)
                (self.root / "checkpoints/pydantic-ai/execute.json").unlink()
                (self.root / "checkpoints/pydantic-ai/code.json").unlink()

    def test_candidate_file_read_rejects_oversize_and_symlink(self):
        folder = self.root / "candidate"
        folder.mkdir()
        oversized = folder / "analysis.json"
        oversized.write_bytes(b"x" * (comparison.MAX_CANDIDATE_JSON_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "size limit"):
            comparison.bounded_candidate_bytes(
                folder, oversized, comparison.MAX_CANDIDATE_JSON_BYTES)
        oversized.unlink()
        private = self.root / "private.json"
        private.write_text("private")
        oversized.symlink_to(private)
        with self.assertRaisesRegex(ValueError, "linked or nonregular"):
            comparison.bounded_candidate_bytes(
                folder, oversized, comparison.MAX_CANDIDATE_JSON_BYTES)
        self.assertEqual(private.read_text(), "private")

    def test_candidate_tree_and_evidence_scan_have_entry_limits(self):
        folder = self.root / "many"
        folder.mkdir()
        for index in range(6):
            (folder / (str(index) + ".txt")).write_text("safe")
        with self.assertRaisesRegex(ValueError, "entry limit"):
            comparison.bounded_entries(folder, 5)
        with patch.object(comparison, "MAX_EVIDENCE_ENTRIES", 5):
            with self.assertRaisesRegex(ValueError, "entry limit"):
                comparison.scan_evidence_for_key(folder, "fake-secret")

    def test_evidence_scan_removes_oversized_file_before_read(self):
        folder = self.root / "evidence"
        folder.mkdir()
        oversized = folder / "output.bin"
        oversized.write_bytes(b"x" * 21)
        with patch.object(comparison, "MAX_EVIDENCE_FILE_BYTES", 20):
            with self.assertRaisesRegex(ValueError, "oversized output"):
                comparison.scan_evidence_for_key(folder, "fake-secret")
        self.assertFalse(oversized.exists())

    def test_adapter_result_rejects_oversized_java_without_hashing_it(self):
        folder = self.root / "candidate"
        target = folder / "src/test/java/OversizedTest.java"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x" * (comparison.MAX_CANDIDATE_TEST_BYTES + 1))
        (folder / "result.json").write_text(json.dumps({
            "path": "src/test/java/OversizedTest.java",
            "content_sha256": "sha256:" + "0" * 64,
        }))
        with self.assertRaisesRegex(ValueError, "size limit"):
            comparison.validate_adapter_output(folder, argparse.Namespace())

    def test_summary_binds_run_spec_hash(self):
        output = self.root / "run"
        output.mkdir()
        spec = output / "run-spec.json"
        spec.write_text(json.dumps(self.spec))
        args = argparse.Namespace(
            profile="spring-transfer", output_root=output, run_spec_data=self.spec)
        with patch.object(comparison, "checkout_identity", return_value={}):
            comparison.save_summary(args, {"aider": "blocked"}, False, 1, 1024, {})
        summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["run_spec_sha256"], comparison.sha256_file(spec))

    def test_checkpoint_is_immutable_and_chained(self):
        folder = self.root / "candidate"
        folder.mkdir()
        output = folder / "analysis.json"
        output.write_text('{"feasibility":"feasible"}')
        args = argparse.Namespace(profile="spring-transfer", output_root=self.root,
                                  run_spec_data=None)
        comparison.write_stage_checkpoint(args, "aider", folder, "analysis",
                                          "completed", [output])
        first = self.root / "checkpoints/aider/analysis.json"
        first_hash = comparison.sha256_file(first)
        comparison.write_stage_checkpoint(args, "aider", folder, "plan",
                                          "blocked", [])
        plan = json.loads((self.root / "checkpoints/aider/plan.json").read_text())
        self.assertEqual(plan["previous_checkpoint_sha256"], first_hash)
        self.assertEqual(
            json.loads(first.read_text())["outputs"]["analysis.json"],
            comparison.sha256_file(output))
        with self.assertRaises(FileExistsError):
            comparison.write_stage_checkpoint(args, "aider", folder,
                                              "analysis", "blocked", [])


if __name__ == "__main__":
    unittest.main()
