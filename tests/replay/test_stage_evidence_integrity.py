"""Regression tests for candidate-writable stage evidence."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[2] / "experiments" / "harness"))
import run_comparison as runner
import archive_evidence


class StageEvidenceIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "candidate-five"
        self.folder.mkdir()
        self.profile = runner.get_profile("spring-transfer")
        self.args = argparse.Namespace(
            profile="spring-transfer", output_root=self.root,
            fixture_root=self.root / "fixture", maven_cache=self.root / "cache",
            run_spec_data=None, pydantic_venv=None, langgraph_venv=None,
            claude_runtime_dir=None,
        )
        self.args.fixture_root.mkdir()

    def add_checkpoint(self, stage):
        artifact = self.folder / (stage + ".json")
        artifact.write_text(json.dumps({"stage": stage}))
        run_record = self.folder / (stage + "-run.json")
        run_record.write_text('{"status":"completed"}')
        log = self.folder / (stage + ".log")
        log.write_text("host-recorded log")
        runner.write_stage_checkpoint(self.args, "candidate-five", self.folder,
                                      stage, "completed", [artifact, run_record, log])

    def plan_stage(self, status, mutate):
        self.add_checkpoint("analysis")
        env = {"ENMAAS_API_KEY": "secret"}
        def execute(*args, **kwargs):
            if mutate:
                mutate()
            (self.folder / "plan.log").write_text("fake plan log")
            (self.folder / "plan.json").write_text('{"plan":"done"}')
            return status, 0 if status == "completed" else None, 1.0
        with patch.object(runner, "adapter_command", return_value=["adapter"]), \
             patch.object(runner, "prepare_container_control"), \
             patch.object(runner, "remove_container"), \
             patch.object(runner, "run_bounded", side_effect=execute):
            runner.run_stage(self.args, "candidate-five", self.folder,
                             self.folder / "generated", "plan",
                             env, ["ENMAAS_API_KEY"], "secret", self.profile)

    def test_candidate_commands_protect_only_prior_stage_files(self):
        self.args.run_spec_data = {"external_candidates": [{
            "name": "candidate-five", "version": "1.0",
            "image": "example.test/adapter@sha256:" + "a" * 64,
            "image_id": "sha256:" + "b" * 64,
        }]}
        self.args.pydantic_venv = self.root / "pydantic"
        self.args.langgraph_venv = self.root / "langgraph"
        self.args.claude_runtime_dir = self.root / "claude"
        workspace = self.folder / "workspace"
        for stage in ("analysis", "plan", "code"):
            with self.subTest(stage=stage):
                previous = ([] if stage == "analysis" else ["analysis"]
                            if stage == "plan" else ["analysis", "plan"])
                commands = [
                    (runner.aider_command(workspace, self.folder, self.profile, stage),
                     workspace, "/work", False),
                    (runner.claude_command(self.args, workspace, self.folder, self.profile, stage),
                     workspace, "/work", False),
                    (runner.adapter_command(self.args, "pydantic-ai", self.folder, stage),
                     self.folder, "/output", True),
                    (runner.adapter_command(self.args, "langgraph", self.folder, stage),
                     self.folder, "/output", True),
                    (runner.adapter_command(self.args, "candidate-five", self.folder, stage),
                     self.folder, "/output", True),
                ]
                for command, source, target, include_records in commands:
                    for name in ("analysis", "plan"):
                        for suffix in ([".json", "-run.json", ".log"] if include_records else [".json"]):
                            mount = str(source / (name + suffix)) + ":" + target + "/" + name + suffix + ":ro,Z"
                            self.assertEqual(mount in command, name in previous, (stage, mount))
                    volume = str(source) + ":" + target + ":Z"
                    self.assertIn(volume, command)
                    for name in previous:
                        prior = str(source / (name + ".json")) + ":" + target + "/" + name + ".json:ro,Z"
                        self.assertLess(command.index(volume), command.index(prior))

    def test_unchanged_plan_preserves_checkpoint_chain(self):
        self.plan_stage("completed", None)
        runner.verify_stage_checkpoints(self.args, "candidate-five", self.folder,
                                        ["analysis", "plan"])

    def test_plan_cannot_rewrite_analysis_even_when_it_fails(self):
        for status in ("completed", "timeout", "failed"):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as folder:
                    self.root = Path(folder)
                    self.folder = self.root / "candidate-five"
                    self.folder.mkdir()
                    self.args.output_root = self.root
                    with self.assertRaisesRegex(ValueError, "stage evidence changed after analysis"):
                        self.plan_stage(status, lambda: (self.folder / "analysis.json").write_text("{}"))
                    self.assertFalse((self.root / "checkpoints/candidate-five/plan.json").exists())

    def test_plan_cannot_rewrite_prior_status_output(self):
        with self.assertRaisesRegex(ValueError, "stage evidence changed after analysis"):
            self.plan_stage("completed", lambda: (self.folder / "analysis-run.json").write_text("{}"))

    def test_plan_cannot_rewrite_prior_log(self):
        with self.assertRaisesRegex(ValueError, "stage evidence changed after analysis"):
            self.plan_stage("completed", lambda: (self.folder / "analysis.log").write_text("forged"))

    def test_host_json_creation_rejects_preplanted_links_and_parent_symlinks(self):
        victim = self.root / "victim"
        victim.write_text("unchanged")
        target = self.folder / "analysis-run.json"
        for link in ("symlink", "hardlink"):
            with self.subTest(link=link):
                if link == "symlink":
                    target.symlink_to(victim)
                else:
                    os.link(victim, target)
                with self.assertRaises(OSError):
                    runner.write_json(target, {"status": "completed"})
                self.assertEqual(victim.read_text(), "unchanged")
                target.unlink()
        parent = self.root / "redirected"
        parent.symlink_to(self.folder, target_is_directory=True)
        with self.assertRaises(OSError):
            runner.write_json(parent / "new.json", {"status": "blocked"})
        self.assertFalse((self.folder / "new.json").exists())

    def test_candidate_cannot_redirect_host_log_copy(self):
        victim = self.root / "victim"
        victim.write_text("unchanged")
        log = self.folder / "generation.log"
        command = [sys.executable, "-c",
                   "import os,sys; os.symlink(sys.argv[1],sys.argv[2]); print('safe output')",
                   str(victim), str(log)]
        with self.assertRaises(OSError):
            runner.run_bounded(command, dict(os.environ), None, log, "fake-key", 10)
        self.assertEqual(victim.read_text(), "unchanged")
        host_log = self.root / ".runner-logs/candidate-five/generation.log"
        self.assertIn("safe output", host_log.read_text())

    def test_output_limit_log_fits_archive_file_limit(self):
        log = self.folder / "generation.log"
        command = [sys.executable, "-c", "print('x' * 200)"]
        with patch.object(runner, "MAX_LOG_BYTES", 20), \
             patch.object(archive_evidence, "MAX_FILE_BYTES", 20):
            status, code, _ = runner.run_bounded(command, dict(os.environ), None,
                                                 log, "fake-key", 10)
            self.assertEqual((status, code), ("output_limit", None))
            self.assertLessEqual(log.stat().st_size, 20)
            self.assertIn(b"[OUTPUT_LIMIT]", log.read_bytes())
            size, _ = archive_evidence.copy_hashed(log, self.root / "archived.log")
            self.assertEqual(size, log.stat().st_size)

    def test_preplanted_regular_log_blocks_evidence_export(self):
        self.folder.rmdir()
        def malicious(*unused):
            log = self.folder / "generation.log"
            log.write_text("forged output")
            runner.run_bounded([sys.executable, "-c", "print('real output')"],
                               dict(os.environ), None, log, "fake-key", 10)
        with patch.object(runner, "save_dependencies"), \
             patch.object(runner, "generate", side_effect=malicious):
            result, stop = runner.run_one_candidate(self.args, "candidate-five", "fake-key")
        self.assertEqual(result, "blocked")
        self.assertTrue(stop)
        self.assertEqual((self.folder / "generation.log").read_text(), "forged output")
        actual = self.root / ".runner-logs/candidate-five/generation.log"
        self.assertIn("real output", actual.read_text())

    def test_preplanted_error_file_uses_host_owned_fallback(self):
        victim = self.root / "victim"
        victim.write_text("unchanged")
        self.folder.rmdir()
        def malicious(*args, **kwargs):
            (self.folder / "runner-error.json").symlink_to(victim)
            raise ValueError("fake candidate failed")
        with patch.object(runner, "save_dependencies"), \
             patch.object(runner, "generate", side_effect=malicious):
            result, stop = runner.run_one_candidate(self.args, "candidate-five", "fake-key")
        self.assertEqual(result, "blocked")
        self.assertTrue(stop)
        self.assertEqual(victim.read_text(), "unchanged")
        fallback = self.root / ".candidate-errors/candidate-five-runner-error.json"
        self.assertIn("fake candidate failed", fallback.read_text())

    def test_preplanted_scan_error_file_uses_host_owned_fallback(self):
        victim = self.root / "victim"
        victim.write_text("unchanged")
        self.folder.rmdir()
        def malicious(*args, **kwargs):
            (self.folder / "evidence-scan-error.json").symlink_to(victim)
            raise ValueError("candidate failure")
        with patch.object(runner, "save_dependencies"), \
             patch.object(runner, "generate", side_effect=malicious), \
             patch.object(runner, "scan_evidence_for_key", side_effect=ValueError("unsafe evidence")):
            result, stop = runner.run_one_candidate(self.args, "candidate-five", "fake-key")
        self.assertEqual(result, "blocked")
        self.assertTrue(stop)
        self.assertEqual(victim.read_text(), "unchanged")
        fallback = self.root / ".candidate-errors/candidate-five-evidence-scan-error.json"
        self.assertIn("unsafe evidence", fallback.read_text())

    def test_evaluation_rejects_preplanted_directory_links(self):
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                with tempfile.TemporaryDirectory() as temp:
                    self.root = Path(temp)
                    self.folder = self.root / "candidate-five"
                    self.folder.mkdir()
                    self.args.output_root = self.root
                    for stage in ("analysis", "plan", "code"):
                        self.add_checkpoint(stage)
                    generated = self.folder / "generated"
                    generated.mkdir()
                    (generated / "invocation.json").write_text("{}")
                    outside = self.root / "outside"
                    if not dangling:
                        outside.mkdir()
                    (self.folder / "evaluation").symlink_to(outside)
                    with patch.object(runner, "run_bounded") as run:
                        with self.assertRaisesRegex(ValueError, "pre-created"):
                            runner.evaluate(self.args, "candidate-five", self.folder,
                                            generated / "test.java",
                                            "src/test/java/GeneratedTest.java", "fake-key")
                    run.assert_not_called()
                    self.assertEqual(outside.exists(), not dangling)

    def test_missing_or_linked_prior_output_is_blocked(self):
        self.add_checkpoint("analysis")
        original = self.folder / "analysis.json"
        original.unlink()
        with self.assertRaises((ValueError, OSError)):
            runner.verify_stage_checkpoints(self.args, "candidate-five", self.folder, ["analysis"])
        external = self.root / "elsewhere.json"
        external.write_text("{}")
        original.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "linked or nonregular"):
            runner.verify_stage_checkpoints(self.args, "candidate-five", self.folder, ["analysis"])

    def test_hardlinked_checkpoint_output_is_rejected(self):
        artifact = self.folder / "analysis.json"
        artifact.write_text("{}")
        os.link(artifact, self.folder / "alias.json")
        with self.assertRaisesRegex(ValueError, "stage evidence has hard links"):
            runner.write_stage_checkpoint(self.args, "candidate-five", self.folder,
                                          "analysis", "completed", [artifact])
        (self.folder / "alias.json").unlink()
        runner.write_stage_checkpoint(self.args, "candidate-five", self.folder,
                                      "analysis", "completed", [artifact])
        os.link(artifact, self.folder / "new-alias.json")
        with self.assertRaisesRegex(ValueError, "stage evidence has hard links"):
            runner.verify_stage_checkpoints(self.args, "candidate-five", self.folder, ["analysis"])

    def test_hardlinked_tool_workspace_copy_is_rejected(self):
        self.add_checkpoint("analysis")
        workspace = self.folder / "workspace"
        workspace.mkdir()
        original = workspace / "analysis.json"
        original.write_bytes((self.folder / "analysis.json").read_bytes())
        runner.verify_workspace_stages(self.folder, workspace, ["analysis"])
        os.link(original, workspace / ".aider.input.history")
        with self.assertRaisesRegex(ValueError, "workspace stage evidence has hard links"):
            runner.verify_workspace_stages(self.folder, workspace, ["analysis"])

    def test_broken_checkpoint_chain_is_blocked(self):
        self.add_checkpoint("analysis")
        self.add_checkpoint("plan")
        plan = self.root / "checkpoints/candidate-five/plan.json"
        data = json.loads(plan.read_text())
        data["previous_checkpoint_sha256"] = "sha256:" + "0" * 64
        plan.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "checkpoint chain invalid"):
            runner.verify_stage_checkpoints(self.args, "candidate-five", self.folder,
                                            ["analysis", "plan"])

    def test_execute_cannot_rewrite_prior_output_even_on_timeout(self):
        for status in ("completed", "timeout"):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as folder:
                    self.root = Path(folder)
                    self.folder = self.root / "candidate-five"
                    self.folder.mkdir()
                    self.args.output_root = self.root
                    for stage in ("analysis", "plan", "code"):
                        self.add_checkpoint(stage)
                    generated = self.folder / "generated"
                    generated.mkdir()
                    (generated / "invocation.json").write_text("{}")
                    def execute(*args, **kwargs):
                        (self.folder / "analysis.json").write_text('{"changed": true}')
                        return status, 0 if status == "completed" else None, 1.0
                    with patch.object(runner, "run_bounded", side_effect=execute), \
                         patch.object(runner, "tool_version", return_value="1.0"):
                        with self.assertRaisesRegex(ValueError, "stage evidence changed after analysis"):
                            runner.evaluate(self.args, "candidate-five", self.folder,
                                            generated / "test.java",
                                            "src/test/java/GeneratedTest.java", "secret")
                    self.assertFalse((self.root / "checkpoints/candidate-five/execute.json").exists())

    def test_timed_out_plan_tampering_records_blocked_and_does_not_stop_run(self):
        self.folder.rmdir()
        def generate(*args, **kwargs):
            self.plan_stage("timeout", lambda: (self.folder / "analysis.json").write_text("{}"))
        with patch.object(runner, "save_dependencies"), \
             patch.object(runner, "generate", side_effect=generate), \
             patch.object(runner, "evaluate") as evaluate, \
             patch.object(runner, "scan_evidence_for_key", return_value=1):
            outcome, stop_run = runner.run_one_candidate(self.args, "candidate-five", "secret")
        self.assertEqual(outcome, "blocked")
        self.assertFalse(stop_run)
        evaluate.assert_not_called()
        error = json.loads((self.folder / "runner-error.json").read_text())
        self.assertIn("stage evidence changed after analysis", error["reason"])
        plan = json.loads((self.root / "checkpoints/candidate-five/plan.json").read_text())
        self.assertEqual(plan["status"], "blocked")

    def test_code_tampering_blocks_candidate_before_execute(self):
        self.folder.rmdir()
        def staged(args, candidate, folder, workspace, stage, env, environment, key, profile):
            self.add_checkpoint(stage)
        def execute(*args, **kwargs):
            (self.folder / "plan.json").write_text('{"changed": true}')
            return "completed", 0, 1.0
        with patch.object(runner, "save_dependencies"), \
             patch.object(runner, "run_stage", side_effect=staged), \
             patch.object(runner, "validate_stage_artifacts",
                          return_value=Path("src/test/java/GeneratedTest.java")), \
             patch.object(runner, "adapter_command", return_value=["adapter"]), \
             patch.object(runner, "prepare_container_control"), \
             patch.object(runner, "remove_container"), \
             patch.object(runner, "run_bounded", side_effect=execute), \
             patch.object(runner, "evaluate") as evaluate, \
             patch.object(runner, "scan_evidence_for_key", return_value=1):
            outcome, blocked = runner.run_one_candidate(self.args, "candidate-five", "secret")
        self.assertEqual(outcome, "blocked")
        self.assertFalse(blocked)
        evaluate.assert_not_called()
        error = json.loads((self.folder / "runner-error.json").read_text())
        self.assertIn("stage evidence changed after plan", error["reason"])
        checkpoint = json.loads((self.root / "checkpoints/candidate-five/code.json").read_text())
        self.assertEqual(checkpoint["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
