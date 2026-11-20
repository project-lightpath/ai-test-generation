"""Failure boundaries for the FLPATH-5050 comparison runner.

These tests use temporary files and fake outcomes; they never call a model.
"""

import argparse
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "experiments" / "harness"))

import run_comparison as comparison  # noqa: E402


class FakeProcess:
    """A fake Popen result with a real pipe for the runner's selector."""

    def __init__(self, output=b"", exit_code=0, hold_open=False):
        reader, writer = os.pipe()
        self.stdout = os.fdopen(reader, "rb", buffering=0)
        self.writer = writer
        self.pid = 4242
        self.exit_code = exit_code
        if output:
            os.write(writer, output)
        if not hold_open:
            self.close_writer()

    def close_writer(self):
        if self.writer is not None:
            os.close(self.writer)
            self.writer = None

    def wait(self, timeout=None):
        return self.exit_code


class ComparisonRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary_directory.name)
        self.fixture = self.root / "fixture"
        self.fixture.mkdir()
        for relative in comparison.SOURCE_PATHS + ("pom.xml",):
            file = self.fixture / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("source for " + relative)
        self.cache = self.root / "cache"
        self.cache.mkdir()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def args(self, output="output", candidates=None):
        return argparse.Namespace(
            fixture_root=self.fixture, maven_cache=self.cache,
            output_root=self.root / output, candidate=candidates or ["aider"],
            check_only=False, api_key_stdin=True,
            pydantic_venv=None, langgraph_venv=None,
            claude_runtime_dir=None,
        )

    def workspace(self):
        folder = self.root / ("candidate-" + str(len(list(self.root.glob("candidate-*")))))
        folder.mkdir()
        # The live runner changes ownership for a rootless container user.
        # Keep this file-scope test independent of host chown privileges.
        with patch.object(comparison.os, "chown"):
            return comparison.workspace_for(self.args(), "claude-code", folder)

    def add_test(self, workspace, relative="src/test/java/example/CandidateTest.java"):
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("class CandidateTest {}\n")
        return target

    def test_preflight_rejects_existing_output_before_external_checks(self):
        args = self.args()
        args.output_root.mkdir()
        with patch.object(comparison, "tree_digest") as digest:
            with self.assertRaisesRegex(ValueError, "must be new"):
                comparison.preflight(args, ["aider"])
        digest.assert_not_called()

    def test_preflight_rejects_wrong_fixture_before_external_checks(self):
        with patch.object(comparison, "tree_digest", return_value="sha256:wrong"):
            with patch.object(comparison, "image_id") as image:
                with self.assertRaisesRegex(ValueError, "fixture tree differs"):
                    comparison.preflight(self.args(), ["aider"])
        image.assert_not_called()

    def test_preflight_rejects_changed_objective_before_external_checks(self):
        stale = comparison.QUARKUS._replace(objective_sha256="sha256:" + "0" * 64)
        with patch.object(comparison, "tree_digest", return_value=comparison.FIXTURE_DIGEST):
            with patch.object(comparison, "get_profile", return_value=stale):
                with patch.object(comparison, "image_id") as image:
                    with self.assertRaisesRegex(ValueError, "objective differs"):
                        comparison.preflight(self.args(), ["aider"])
        image.assert_not_called()

    def test_live_output_gate_accepts_dedicated_small_mount(self):
        args = self.args()
        with patch.object(comparison.os.path, "ismount", return_value=True), \
                patch.object(comparison.os, "statvfs",
                             return_value=SimpleNamespace(f_blocks=1024, f_frsize=1024)):
            self.assertEqual(comparison.output_mount_capacity(args.output_root), 1024 * 1024)

    def test_live_output_gate_rejects_shared_or_oversized_mount(self):
        args = self.args()
        with patch.object(comparison.os.path, "ismount", return_value=False):
            with self.assertRaisesRegex(ValueError, "dedicated mounted filesystem"):
                comparison.output_mount_capacity(args.output_root)
        oversized = comparison.MAX_OUTPUT_MOUNT_BYTES + 1
        with patch.object(comparison.os.path, "ismount", return_value=True), \
                patch.object(comparison.os, "statvfs",
                             return_value=SimpleNamespace(f_blocks=oversized, f_frsize=1)):
            with self.assertRaisesRegex(ValueError, "at most 8 GiB"):
                comparison.output_mount_capacity(args.output_root)

    def test_generated_test_accepts_one_new_java_test(self):
        workspace = self.workspace()
        target = self.add_test(workspace)
        actual, relative = comparison.generated_test(workspace, self.args(), "claude-code")
        self.assertEqual(actual, target)
        self.assertEqual(relative, "src/test/java/example/CandidateTest.java")

    def test_generated_test_rejects_modified_supplied_source(self):
        workspace = self.workspace()
        self.add_test(workspace)
        (workspace / comparison.SOURCE_PATHS[0]).write_text("changed")
        with self.assertRaisesRegex(ValueError, "changed supplied file"):
            comparison.generated_test(workspace, self.args(), "claude-code")

    def test_generated_test_rejects_second_file(self):
        workspace = self.workspace()
        self.add_test(workspace)
        (workspace / "notes.txt").write_text("unexpected")
        with self.assertRaisesRegex(ValueError, "exactly one test"):
            comparison.generated_test(workspace, self.args(), "claude-code")

    def test_generated_test_rejects_file_outside_java_tests(self):
        workspace = self.workspace()
        self.add_test(workspace, "CandidateTest.java")
        with self.assertRaisesRegex(ValueError, "outside the native test path"):
            comparison.generated_test(workspace, self.args(), "claude-code")

    def test_generated_test_rejects_symlink(self):
        workspace = self.workspace()
        target = workspace / "src/test/java/example/CandidateTest.java"
        target.parent.mkdir(parents=True)
        target.symlink_to(self.fixture / "pom.xml")
        with self.assertRaisesRegex(ValueError, "linked or nonregular workspace output"):
            comparison.generated_test(workspace, self.args(), "claude-code")

    def test_saved_command_redacts_credential_but_keeps_endpoint(self):
        folder = self.root / "candidate"
        folder.mkdir()
        comparison.save_command(
            folder, ["podman", "run", "--network=host"],
            ["ENMAAS_API_KEY", "OPENAI_API_BASE"],
            {"ENMAAS_API_KEY": "unit-test-secret", "OPENAI_API_BASE": "https://example.test/v1"},
        )
        record = (folder / "execution-command.json").read_text()
        self.assertNotIn("unit-test-secret", record)
        self.assertEqual(json.loads(record)["environment"],
                         {"ENMAAS_API_KEY": "[REDACTED]",
                          "OPENAI_API_BASE": "https://example.test/v1"})

    def test_evidence_scan_accepts_clean_nested_output(self):
        output = self.root / "output"
        test = output / "candidate/src/test/java/CandidateTest.java"
        artifact = output / "candidate/evaluation/evaluation.json"
        test.parent.mkdir(parents=True)
        artifact.parent.mkdir(parents=True)
        test.write_text("class CandidateTest {}")
        artifact.write_text('{"result":"discriminates"}')
        comparison.scan_evidence_for_key(output, "unit-test-secret")
        self.assertTrue(test.exists())
        self.assertTrue(artifact.exists())

    def test_evidence_scan_blocks_key_in_generated_test(self):
        output = self.root / "output"
        test = output / "candidate/src/test/java/CandidateTest.java"
        test.parent.mkdir(parents=True)
        test.write_text('String key = "unit-test-secret";')
        with self.assertRaisesRegex(ValueError, "credential found"):
            comparison.scan_evidence_for_key(output, "unit-test-secret")
        self.assertFalse(test.exists())

    def test_evidence_scan_blocks_key_in_nested_artifact(self):
        output = self.root / "output"
        artifact = output / "candidate/evaluation/logs/result.txt"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"prefix unit-test-secret suffix")
        with self.assertRaisesRegex(ValueError, "credential found"):
            comparison.scan_evidence_for_key(output, "unit-test-secret")
        self.assertFalse(artifact.exists())

    def test_evidence_scan_blocks_key_split_across_file_read_boundary(self):
        output = self.root / "output"
        output.mkdir()
        artifact = output / "large-result.bin"
        artifact.write_bytes(b"x" * (65536 - len(b"unit-")) + b"unit-" + b"test-secret")
        with self.assertRaisesRegex(ValueError, "credential found"):
            comparison.scan_evidence_for_key(output, "unit-test-secret")
        self.assertFalse(artifact.exists())

    def test_evidence_scan_blocks_key_in_file_or_directory_name(self):
        for kind in ("file", "directory"):
            with self.subTest(kind=kind):
                output = self.root / ("output-" + kind)
                output.mkdir()
                path = output / ("artifact-unit-test-secret-" + kind)
                if kind == "file":
                    path.write_text("clean contents")
                else:
                    path.mkdir()
                    (path / "clean.txt").write_text("clean contents")
                with self.assertRaisesRegex(ValueError, "credential found in output path") as failure:
                    comparison.scan_evidence_for_key(output, "unit-test-secret")
                self.assertFalse(path.exists())
                self.assertNotIn("unit-test-secret", str(failure.exception))

    def test_evidence_scan_removes_all_key_matches_before_blocking(self):
        output = self.root / "output"
        nested = output / "nested"
        nested.mkdir(parents=True)
        files = [output / "first.txt", nested / "second.txt",
                 output / "name-unit-test-secret.txt"]
        files[0].write_text("unit-test-secret")
        files[1].write_text("prefix unit-test-secret suffix")
        files[2].write_text("clean contents")
        with self.assertRaisesRegex(ValueError, "credential found") as failure:
            comparison.scan_evidence_for_key(output, "unit-test-secret")
        for path in files:
            self.assertFalse(path.exists(), str(path))
        self.assertNotIn("unit-test-secret", str(failure.exception))

    def test_evidence_scan_blocks_symlink_without_following_it(self):
        output = self.root / "output"
        output.mkdir()
        target = self.root / "private.txt"
        target.write_text("do not read")
        link = output / "private-link"
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink in output"):
            comparison.scan_evidence_for_key(output, "unit-test-secret")
        self.assertFalse(link.exists())
        self.assertTrue(target.exists())

    def test_evidence_scan_read_failure_is_not_a_pass(self):
        output = self.root / "output"
        output.mkdir()
        artifact = output / "unreadable.txt"
        artifact.write_text("ordinary")
        original_open = pathlib.Path.open

        def fail_artifact_open(path, *args, **kwargs):
            if path == artifact:
                raise PermissionError("read blocked")
            return original_open(path, *args, **kwargs)

        with patch.object(pathlib.Path, "open", fail_artifact_open):
            with self.assertRaises(PermissionError):
                comparison.scan_evidence_for_key(output, "unit-test-secret")

    def test_run_bounded_redacts_key_from_saved_output(self):
        process = FakeProcess(b"before unit-test-secret after\n")
        log = self.root / "generation.log"
        with patch.object(comparison.subprocess, "Popen", return_value=process):
            status, code, _ = comparison.run_bounded(
                ["fake-generator"], {}, None, log, "unit-test-secret", 1)
        self.assertEqual((status, code), ("completed", 0))
        self.assertEqual(log.read_bytes(), b"before [REDACTED] after\n")

    def test_run_bounded_redacts_key_split_between_reads(self):
        process = FakeProcess(b"ready")
        log = self.root / "generation.log"
        with patch.object(comparison.subprocess, "Popen", return_value=process), \
                patch.object(comparison.os, "read",
                             side_effect=[b"before unit-test-", b"secret after", b""]):
            status, code, _ = comparison.run_bounded(
                ["fake-generator"], {}, None, log, "unit-test-secret", 1)
        self.assertEqual((status, code), ("completed", 0))
        self.assertEqual(log.read_bytes(), b"before [REDACTED] after")

    def test_run_bounded_stops_output_at_limit(self):
        process = FakeProcess(b"a" * 80)
        log = self.root / "generation.log"
        with patch.object(comparison.subprocess, "Popen", return_value=process), \
                patch.object(comparison, "MAX_LOG_BYTES", 20), \
                patch.object(comparison, "stop_process") as stop:
            status, code, _ = comparison.run_bounded(
                ["fake-generator"], {}, None, log, "unit-test-secret", 1)
        self.assertEqual((status, code), ("output_limit", None))
        self.assertIn(b"[OUTPUT_LIMIT]", log.read_bytes())
        self.assertLessEqual(len(log.read_bytes()), 20 + len(b"\n[OUTPUT_LIMIT]\n"))
        stop.assert_called_once_with(process)

    def test_run_bounded_stops_hung_process_at_timeout(self):
        process = FakeProcess(hold_open=True)
        log = self.root / "generation.log"
        try:
            with patch.object(comparison.subprocess, "Popen", return_value=process), \
                    patch.object(comparison, "stop_process") as stop:
                status, code, _ = comparison.run_bounded(
                    ["fake-generator"], {}, None, log, "unit-test-secret", 0.01)
            self.assertEqual((status, code), ("timeout", None))
            stop.assert_called_once_with(process)
        finally:
            process.close_writer()

    def run_main(self, args, generate_side_effect=None, evaluate_side_effect=None):
        parser = argparse.ArgumentParser()
        stdin = io.StringIO("unit-test-secret\n")
        with patch.object(comparison, "parse_arguments", return_value=(args, parser)), \
                patch.object(comparison, "preflight", return_value={"aider": "0.86.2"}), \
                patch.object(comparison, "output_mount_capacity", return_value=1024 * 1024), \
                patch.object(comparison, "checkout_identity", return_value={"checkout_commit": "unit-test"}), \
                patch.object(comparison, "save_dependencies"), \
                patch.object(comparison, "generate", side_effect=generate_side_effect or
                             [(self.root / "CandidateTest.java", "src/test/java/CandidateTest.java")]
                             * len(args.candidate)), \
                patch.object(comparison, "evaluate", side_effect=evaluate_side_effect or
                             ["discriminates"] * len(args.candidate)), \
                patch.object(comparison.sys, "stdin", stdin):
            return comparison.main()

    def test_main_requires_all_candidates_to_discriminate(self):
        args = self.args(candidates=["aider", "pydantic-ai"])
        code = self.run_main(args, evaluate_side_effect=["discriminates", "inconclusive"])
        self.assertEqual(code, 2)
        summary = json.loads((args.output_root / "summary.json").read_text())
        self.assertEqual(summary["candidate_results"],
                         {"aider": "discriminates", "pydantic-ai": "inconclusive"})

    def test_main_returns_success_only_when_every_candidate_discriminates(self):
        args = self.args(candidates=["aider", "pydantic-ai"])
        self.assertEqual(self.run_main(args), 0)
        summary = json.loads((args.output_root / "summary.json").read_text())
        self.assertEqual(set(summary["candidate_results"].values()), {"discriminates"})

    def test_main_keeps_failure_evidence_and_redacts_key(self):
        args = self.args()
        code = self.run_main(args, generate_side_effect=ValueError("unit-test-secret failed"))
        self.assertEqual(code, 2)
        error = (args.output_root / "aider" / "runner-error.json").read_text()
        self.assertIn("[REDACTED]", error)
        self.assertNotIn("unit-test-secret", error)
        self.assertEqual(json.loads((args.output_root / "summary.json").read_text())
                         ["candidate_results"]["aider"], "blocked")

    def test_main_continues_after_one_generator_fails(self):
        args = self.args(candidates=["aider", "pydantic-ai"])
        code = self.run_main(
            args,
            generate_side_effect=[ValueError("first generator failed"),
                                  (self.root / "CandidateTest.java",
                                   "src/test/java/CandidateTest.java")],
            evaluate_side_effect=["discriminates"],
        )
        self.assertEqual(code, 2)
        summary = json.loads((args.output_root / "summary.json").read_text())
        self.assertEqual(summary["candidate_results"],
                         {"aider": "blocked", "pydantic-ai": "discriminates"})

    def test_main_blocks_and_stops_after_evidence_scan_failure(self):
        args = self.args(candidates=["aider", "pydantic-ai"])
        parser = argparse.ArgumentParser()
        stdin = io.StringIO("unit-test-secret\n")
        with patch.object(comparison, "parse_arguments", return_value=(args, parser)), \
                patch.object(comparison, "preflight", return_value={"aider": "0.86.2"}), \
                patch.object(comparison, "output_mount_capacity", return_value=1024 * 1024), \
                patch.object(comparison, "checkout_identity", return_value={"checkout_commit": "unit-test"}), \
                patch.object(comparison, "save_dependencies"), \
                patch.object(comparison, "generate", return_value=(
                    self.root / "CandidateTest.java", "src/test/java/CandidateTest.java")) as generate, \
                patch.object(comparison, "evaluate", return_value="discriminates"), \
                patch.object(comparison, "scan_evidence_for_key",
                             side_effect=ValueError("unit-test-secret in output")), \
                patch.object(comparison.sys, "stdin", stdin):
            code = comparison.main()
        self.assertEqual(code, 2)
        generate.assert_called_once()
        summary = json.loads((args.output_root / "summary.json").read_text())
        self.assertEqual(summary["candidate_results"],
                         {"aider": "blocked", "pydantic-ai": "not_run"})
        self.assertEqual(summary["evidence_scan"], "blocked")
        self.assertNotIn("unit-test-secret",
                         (args.output_root / "aider/evidence-scan-error.json").read_text())


if __name__ == "__main__":
    unittest.main()
