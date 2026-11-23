"""The Spring fixture keeps Analyze and Plan visible before test generation."""

import argparse
import importlib.util
import io
import os
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "experiments" / "harness"))

from fixture_profiles import get_profile  # noqa: E402
import candidate_evaluation as evaluation  # noqa: E402
import run_comparison as comparison  # noqa: E402


def load_langgraph_adapter_without_dependencies():
    """Import the adapter with inert dependency stubs; no model call is possible."""
    graph = ModuleType("langgraph.graph")
    graph.END = object()
    graph.START = object()
    graph.StateGraph = object
    openai = ModuleType("openai")
    openai.OpenAI = object
    chat = ModuleType("openai.types.chat")
    chat.ChatCompletion = object
    modules = {"langgraph": ModuleType("langgraph"), "langgraph.graph": graph,
               "openai": openai, "openai.types": ModuleType("openai.types"),
               "openai.types.chat": chat}
    adapter_path = (pathlib.Path(__file__).parents[2] / "experiments" / "harness"
                    / "candidates" / "langgraph" / "adapter.py")
    spec = importlib.util.spec_from_file_location("langgraph_adapter_under_test", adapter_path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class SpringFixtureStageTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.temporary_directory.name)
        self.profile = get_profile("spring-transfer")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_valid_stages(self):
        analysis = {
            "relevant_files": ["src/main/java/com/redhat/lightwell/service/TransactionService.java"],
            "existing_coverage": "Smaller transfers and insufficient funds are tested.",
            "gap": "No test transfers exactly the full available balance.",
            "feasibility": "feasible",
        }
        plan = {
            "test_path": "src/test/java/com/redhat/lightwell/service/FullBalanceTransferTest.java",
            "setup": "Construct source and target accounts with a real TransactionService.",
            "assertions": ["Transfer succeeds", "Source is zero", "Target receives the amount"],
            "baseline_expectation": "pass",
            "mutant_expectation": "assertion failure",
        }
        (self.folder / "analysis.json").write_text(json.dumps(analysis))
        (self.folder / "plan.json").write_text(json.dumps(plan))
        return analysis, plan

    def prepared_workspace(self, name, candidate="claude-code"):
        fixture = self.folder / "fixture"
        fixture.mkdir(exist_ok=True)
        for relative in self.profile.source_paths:
            target = fixture / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("source for " + relative)
        candidate_folder = self.folder / name
        candidate_folder.mkdir()
        args = argparse.Namespace(fixture_root=fixture, profile="spring-transfer")
        with patch.object(comparison.os, "chown"):
            workspace = comparison.workspace_for(args, candidate, candidate_folder)
        return workspace, args

    def test_spring_profile_pins_separate_application_and_objective(self):
        self.assertTrue(self.profile.staged)
        self.assertEqual(self.profile.repository, "lzspangler/spring-boot-example-application")
        self.assertEqual(self.profile.revision,
                         "01a18a1b40e151ffcc1d7bef86bf335f87999750")
        self.assertTrue(self.profile.objective.is_file())
        self.assertEqual(comparison.sha256_file(self.profile.objective),
                         self.profile.objective_sha256)
        self.assertIn("TransactionService.java", str(self.profile.mutation_path))
        self.assertNotEqual(self.profile.original_line, self.profile.mutated_line)
        self.assertFalse(get_profile("quarkus").staged)

    def test_valid_analysis_and_plan_pass_before_code(self):
        self.write_valid_stages()
        comparison.validate_stage_artifacts(self.folder, self.profile)

    def test_missing_analysis_or_plan_blocks_code(self):
        analysis, plan = self.write_valid_stages()
        for missing in ("analysis.json", "plan.json"):
            with self.subTest(missing=missing):
                path = self.folder / missing
                path.unlink()
                with self.assertRaises(ValueError):
                    comparison.validate_stage_artifacts(self.folder, self.profile)
                path.write_text(json.dumps(analysis if missing == "analysis.json" else plan))

    def test_empty_analysis_field_blocks_code(self):
        analysis, _ = self.write_valid_stages()
        for field in ("relevant_files", "existing_coverage", "gap"):
            with self.subTest(field=field):
                changed = dict(analysis)
                changed[field] = [] if field == "relevant_files" else ""
                (self.folder / "analysis.json").write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    comparison.validate_stage_artifacts(self.folder, self.profile)

    def test_blocked_feasibility_cannot_be_reported_as_plan_ready(self):
        analysis, _ = self.write_valid_stages()
        for feasibility in ("blocked", "unknown", "exactly feasible"):
            with self.subTest(feasibility=feasibility):
                analysis["feasibility"] = feasibility
                (self.folder / "analysis.json").write_text(json.dumps(analysis))
                with self.assertRaises(ValueError):
                    comparison.validate_stage_artifacts(self.folder, self.profile)

    def test_unsafe_or_non_test_plan_path_blocks_code(self):
        _, plan = self.write_valid_stages()
        for path in ("../escape/Test.java", "/tmp/OutsideTest.java",
                     "src/main/java/com/redhat/lightwell/ProductionTest.java",
                     "src/test/java/com/redhat/lightwell/Notes.java"):
            with self.subTest(path=path):
                changed = dict(plan)
                changed["test_path"] = path
                (self.folder / "plan.json").write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    comparison.validate_stage_artifacts(self.folder, self.profile)

    def test_spring_mutation_changes_transfer_guard_only(self):
        source = self.folder / self.profile.mutation_path
        source.parent.mkdir(parents=True)
        withdraw = "if (account.getBalance().compareTo(request.getAmount()) < 0) {"
        source.write_text(withdraw + "\n" + self.profile.original_line + "\n")

        evaluation.mutate_fixture(self.folder, self.profile)

        changed = source.read_text()
        self.assertIn(withdraw, changed)
        self.assertNotIn(self.profile.original_line, changed)
        self.assertIn(self.profile.mutated_line, changed)

    def test_junit_assertion_failed_error_counts_as_assertion(self):
        reports = self.folder / "target/surefire-reports"
        reports.mkdir(parents=True)
        class_name = "SpringTransferExactBalanceReferenceTest"
        (reports / ("TEST-com.redhat.lightwell.service." + class_name + ".xml")).write_text(
            '<testsuite name="com.redhat.lightwell.service.' + class_name + '" '
            'tests="1" failures="1" errors="0" skipped="0">'
            '<testcase name="fullBalance" classname="com.redhat.lightwell.service.'
            + class_name + '">'
            '<failure type="org.opentest4j.AssertionFailedError">rejected</failure>'
            '</testcase></testsuite>')

        result = evaluation.surefire_result(self.folder, class_name)

        self.assertEqual(result["assertion_failures"], 1)
        self.assertEqual(result["errors"], 0)

    def test_analyze_stage_blocks_early_test_and_source_edit(self):
        for change in ("early-test", "source-edit"):
            with self.subTest(change=change):
                workspace, args = self.prepared_workspace("candidate-" + change)
                (workspace / "analysis.json").write_text('{"feasibility":"feasible"}')
                if change == "early-test":
                    target = workspace / "src/test/java/com/redhat/lightwell/service/EarlyTest.java"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("class EarlyTest {}")
                else:
                    (workspace / self.profile.mutation_path).write_text("changed")
                with self.assertRaises(ValueError):
                    comparison.validate_stage_workspace(workspace, args, "claude-code", "analysis")

    def test_plan_stage_blocks_early_test_and_analysis_rewrite(self):
        original_analysis = b'{"gap":"exact-balance boundary"}'
        for change in ("early-test", "analysis-rewrite"):
            with self.subTest(change=change):
                workspace, args = self.prepared_workspace("candidate-" + change)
                (workspace / "analysis.json").write_bytes(original_analysis)
                (workspace / "plan.json").write_text('{"test_path":"src/test/java/PlannedTest.java"}')
                if change == "early-test":
                    target = workspace / "src/test/java/com/redhat/lightwell/service/EarlyTest.java"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("class EarlyTest {}")
                else:
                    (workspace / "analysis.json").write_text('{"gap":"changed"}')
                with self.assertRaises(ValueError):
                    comparison.validate_stage_workspace(
                        workspace, args, "claude-code", "plan", original_analysis)

    def test_stage_workspace_accepts_only_expected_artifact(self):
        workspace, args = self.prepared_workspace("candidate-valid")
        analysis = b'{"gap":"exact-balance boundary"}'
        (workspace / "analysis.json").write_bytes(analysis)
        comparison.validate_stage_workspace(workspace, args, "claude-code", "analysis")
        (workspace / "plan.json").write_text('{"test_path":"src/test/java/PlannedTest.java"}')
        comparison.validate_stage_workspace(workspace, args, "claude-code", "plan", analysis)

    def test_staged_generation_records_analysis_plan_then_code(self):
        fixture = self.folder / "fixture"
        fixture.mkdir()
        for relative in self.profile.source_paths:
            file = fixture / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("source for " + relative)
        folder = self.folder / "candidate"
        folder.mkdir()
        workspace = folder / "workspace"
        args = argparse.Namespace(fixture_root=fixture, profile="spring-transfer",
                                  output_root=self.folder,
                                  claude_runtime_dir=pathlib.Path("/unused"))
        planned = "src/test/java/com/redhat/lightwell/service/FullBalanceTransferTest.java"
        analysis = {"relevant_files": list(self.profile.source_paths),
                    "existing_coverage": "Smaller transfers", "gap": "Exact balance",
                    "feasibility": "feasible"}
        plan = {"test_path": planned, "setup": "Two accounts",
                "assertions": ["Source zero", "Target credited"],
                "baseline_expectation": "pass", "mutant_expectation": "assertion failure"}
        calls = []

        def fake_run(command, env, cwd, log, key, timeout):
            calls.append(log.name)
            log.write_text("mock stage log")
            if log.name == "analysis.log":
                (workspace / "analysis.json").write_text(json.dumps(analysis))
            elif log.name == "plan.log":
                (workspace / "plan.json").write_text(json.dumps(plan))
            elif log.name == "generation.log":
                target = workspace / planned
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("class FullBalanceTransferTest {}\n")
            return "completed", 0, 1

        with patch.object(comparison.os, "chown"), \
                patch.object(comparison.os, "fchown"), \
                patch.object(comparison, "run_bounded", side_effect=fake_run):
            generated, relative = comparison.generate(args, "claude-code", folder,
                                                      "unit-test-secret")

        self.assertEqual(calls, ["analysis.log", "plan.log", "generation.log"])
        self.assertEqual(relative, planned)
        self.assertEqual(generated, workspace / planned)
        stage_status = json.loads((folder / "stage-validation.json").read_text())
        self.assertEqual(stage_status["semantic_review"], "pending")
        config = json.loads((folder / "sanitized-config.json").read_text())
        self.assertEqual(config["stage_sha256"]["analysis.json"],
                         comparison.sha256_bytes((folder / "analysis.json").read_bytes()))
        self.assertEqual(config["stage_sha256"]["plan.json"],
                         comparison.sha256_bytes((folder / "plan.json").read_bytes()))

    def test_staged_generation_stops_when_analysis_creates_test(self):
        fixture = self.folder / "fixture"
        fixture.mkdir()
        for relative in self.profile.source_paths:
            file = fixture / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("source for " + relative)
        folder = self.folder / "candidate"
        folder.mkdir()
        workspace = folder / "workspace"
        args = argparse.Namespace(fixture_root=fixture, profile="spring-transfer",
                                  output_root=self.folder,
                                  claude_runtime_dir=pathlib.Path("/unused"))
        calls = []

        def fake_run(command, env, cwd, log, key, timeout):
            calls.append(log.name)
            log.write_text("mock stage log")
            (workspace / "analysis.json").write_text(json.dumps({
                "relevant_files": ["TransactionService.java"],
                "existing_coverage": "smaller transfers", "gap": "exact balance",
                "feasibility": "feasible"}))
            target = workspace / "src/test/java/com/redhat/lightwell/service/EarlyTest.java"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("class EarlyTest {}\n")
            return "completed", 0, 1

        with patch.object(comparison.os, "chown"), \
                patch.object(comparison.os, "fchown"), \
                patch.object(comparison, "run_bounded", side_effect=fake_run):
            with self.assertRaises(ValueError):
                comparison.generate(args, "claude-code", folder, "unit-test-secret")
        self.assertEqual(calls, ["analysis.log"])
        self.assertFalse((folder / "plan-command.json").exists())
        self.assertFalse((folder / "generation.json").exists())

    def test_adapter_stage_rejects_symlink_artifact(self):
        folder = self.folder / "adapter"
        folder.mkdir()
        external = self.folder / "outside-analysis.json"
        external.write_text(json.dumps({"feasibility": "feasible"}))
        target = folder / "analysis.json"
        args = argparse.Namespace(profile="spring-transfer", fixture_root=self.folder,
                                  pydantic_venv=pathlib.Path("/unused"))

        def fake_run(command, env, cwd, log, key, timeout):
            target.symlink_to(external)
            return "completed", 0, 1

        with patch.object(comparison, "run_bounded", side_effect=fake_run):
            with self.assertRaises(ValueError):
                comparison.run_stage(args, "pydantic-ai", folder, folder / "generated",
                                     "analysis", {"ENMAAS_API_KEY": "unit-test-secret"},
                                     ["ENMAAS_API_KEY"], "unit-test-secret", self.profile)
        self.assertTrue(external.is_file())

    def test_adapter_rejects_symlink_result_before_read(self):
        workspace = self.folder / "adapter"
        workspace.mkdir()
        external = self.folder / "outside-result.json"
        external.write_text(json.dumps({"path": "src/test/java/CandidateTest.java",
                                        "content_sha256": "sha256:" + "0" * 64}))
        (workspace / "result.json").symlink_to(external)
        with self.assertRaises(ValueError):
            comparison.validate_adapter_output(workspace, argparse.Namespace())
        self.assertTrue(external.is_file())

    def test_adapter_rejects_symlink_test_and_parent(self):
        relative = "src/test/java/com/redhat/lightwell/service/CandidateTest.java"
        for kind in ("file", "parent"):
            with self.subTest(kind=kind):
                workspace = self.folder / ("adapter-" + kind)
                workspace.mkdir()
                external = self.folder / ("outside-" + kind)
                if kind == "file":
                    external.write_text("class CandidateTest {}\n")
                    target = workspace / relative
                    target.parent.mkdir(parents=True)
                    target.symlink_to(external)
                    real_file = external
                else:
                    external.mkdir()
                    real_file = external / "CandidateTest.java"
                    real_file.write_text("class CandidateTest {}\n")
                    parent = workspace / "src/test/java/com/redhat/lightwell"
                    parent.mkdir(parents=True)
                    (parent / "service").symlink_to(external, target_is_directory=True)
                (workspace / "result.json").write_text(json.dumps({
                    "path": relative, "content_sha256": comparison.sha256_file(real_file)}))
                with self.assertRaises(ValueError):
                    comparison.validate_adapter_output(workspace, argparse.Namespace())
                self.assertTrue(real_file.is_file())

    def test_regular_output_check_rejects_fifo(self):
        workspace = self.folder / "adapter"
        workspace.mkdir()
        fifo = workspace / "result.json"
        os.mkfifo(str(fifo))
        with self.assertRaises(ValueError):
            comparison.require_regular_output(workspace, fifo)

    def test_supplied_source_symlink_is_rejected_before_read(self):
        workspace, args = self.prepared_workspace("candidate-source-link")
        source = workspace / self.profile.mutation_path
        external = self.folder / "outside-source.java"
        external.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(external)
        original_read = pathlib.Path.read_bytes

        def guarded_read(path):
            if path == source:
                raise AssertionError("host read a supplied symlink")
            return original_read(path)

        with patch.object(pathlib.Path, "read_bytes", guarded_read):
            with self.assertRaises(ValueError):
                comparison.validate_stage_workspace(workspace, args, "claude-code", "analysis")
            with self.assertRaises(ValueError):
                comparison.generated_test(workspace, args, "claude-code")
        self.assertTrue(external.is_file())

    def test_aider_input_history_is_allowed_without_weakening_stage_checks(self):
        workspace, args = self.prepared_workspace("candidate-aider-valid", "aider")
        metadata = workspace / ".aider.input.history"
        metadata.write_text("Aider local input history\n")
        analysis = b'{"gap":"exact-balance boundary"}'
        plan = b'{"test_path":"src/test/java/com/redhat/lightwell/service/FullBalanceTest.java"}'
        (workspace / "analysis.json").write_bytes(analysis)
        comparison.validate_stage_workspace(workspace, args, "aider", "analysis")
        (workspace / "plan.json").write_bytes(plan)
        comparison.validate_stage_workspace(workspace, args, "aider", "plan", analysis)
        target = workspace / "src/test/java/com/redhat/lightwell/service/FullBalanceTest.java"
        target.write_text("class FullBalanceTest {}\n")
        generated, relative = comparison.generated_test(
            workspace, args, "aider", {"analysis.json": analysis, "plan.json": plan})
        self.assertEqual(generated, target)
        self.assertEqual(relative, "src/test/java/com/redhat/lightwell/service/FullBalanceTest.java")

        for change in ("early-test", "source-edit"):
            with self.subTest(change=change):
                workspace, args = self.prepared_workspace("candidate-aider-" + change, "aider")
                (workspace / ".aider.input.history").write_text("Aider local input history\n")
                (workspace / "analysis.json").write_bytes(analysis)
                if change == "early-test":
                    (workspace / "src/test/java/com/redhat/lightwell/service/EarlyTest.java").write_text(
                        "class EarlyTest {}\n")
                else:
                    (workspace / self.profile.mutation_path).write_text("changed source")
                with self.assertRaises(ValueError):
                    comparison.validate_stage_workspace(workspace, args, "aider", "analysis")

    def test_analysis_accepts_supplied_and_extra_inferred_paths(self):
        analysis, _ = self.write_valid_stages()
        supplied = analysis["relevant_files"][0]
        inferred = "src/main/java/com/redhat/lightwell/service/RelatedService.java"
        analysis["relevant_files"] = [supplied, inferred]
        (self.folder / "analysis.json").write_text(json.dumps(analysis))
        comparison.validate_stage_artifacts(self.folder, self.profile)

        analysis["relevant_files"] = [inferred]
        (self.folder / "analysis.json").write_text(json.dumps(analysis))
        with self.assertRaises(ValueError):
            comparison.validate_stage_artifacts(self.folder, self.profile)

        analysis["relevant_files"] = [supplied, 7]
        (self.folder / "analysis.json").write_text(json.dumps(analysis))
        with self.assertRaises(ValueError):
            comparison.validate_stage_artifacts(self.folder, self.profile)

    def test_analysis_prompt_uses_exact_feasibility_enum(self):
        prompt = comparison.stage_prompt(self.profile, "analysis")
        self.assertIn('JSON strings "feasible", "blocked", or "unknown"', prompt)
        self.assertNotIn("exactly feasible", prompt)

    def test_spring_adapter_code_timeout_is_recorded_without_model_call(self):
        fixture = self.folder / "fixture"
        fixture.mkdir()
        for relative in self.profile.source_paths:
            file = fixture / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("source for " + relative)
        folder = self.folder / "candidate-pydantic"
        folder.mkdir()
        args = argparse.Namespace(fixture_root=fixture, profile="spring-transfer",
                                  output_root=self.folder,
                                  pydantic_venv=pathlib.Path("/unused"),
                                  langgraph_venv=pathlib.Path("/unused"))
        planned = "src/test/java/com/redhat/lightwell/service/FullBalanceTest.java"
        analysis = {"relevant_files": [self.profile.source_paths[1]],
                    "existing_coverage": "smaller transfers", "gap": "exact balance",
                    "feasibility": "feasible"}
        plan = {"test_path": planned, "setup": "two accounts",
                "assertions": ["source zero", "target credited"],
                "baseline_expectation": "pass", "mutant_expectation": "assertion failure"}
        seen = []

        def fake_stage(args, candidate, folder, workspace, stage, env,
                       environment, key, profile):
            seen.append(stage)
            artifact = folder / (stage + ".json")
            artifact.write_text(json.dumps(analysis if stage == "analysis" else plan))
            status = folder / (stage + "-run.json")
            status.write_text('{"status":"completed"}')
            log = folder / (stage + ".log")
            log.write_text("mock stage log")
            comparison.write_stage_checkpoint(args, candidate, folder, stage,
                                              "completed", [artifact, status, log])

        def fake_run(command, env, cwd, log, key, timeout):
            seen.append("code")
            self.assertEqual(log.name, "generation.log")
            self.assertEqual(timeout, 600)
            log.write_text("mock code log")
            workspace = folder / "generated"
            target = workspace / planned
            target.parent.mkdir(parents=True, exist_ok=True)
            content = b"class FullBalanceTest {}\n"
            target.write_bytes(content)
            (workspace / "result.json").write_text(json.dumps({
                "path": planned, "content_sha256": comparison.sha256_bytes(content)}))
            return "completed", 0, 42

        with patch.object(comparison, "run_stage", side_effect=fake_stage), \
                patch.object(comparison, "run_bounded", side_effect=fake_run):
            generated, relative = comparison.generate(args, "pydantic-ai", folder,
                                                      "unit-test-secret")
        self.assertEqual(seen, ["analysis", "plan", "code"])
        self.assertEqual(relative, planned)
        self.assertEqual(generated, folder / "generated" / planned)
        record = json.loads((folder / "generation.json").read_text())
        self.assertEqual(record["timeout_seconds"], 600)
        self.assertEqual(record["status"], "completed")

    def test_timeout_limits_are_profile_and_candidate_specific(self):
        self.assertEqual(comparison.generation_timeout("pydantic-ai", self.profile), 600)
        self.assertEqual(comparison.generation_timeout("langgraph", self.profile), 600)
        self.assertEqual(comparison.generation_timeout("aider", self.profile), 300)
        self.assertEqual(comparison.generation_timeout("claude-code", self.profile), 360)
        quarkus = get_profile("quarkus")
        self.assertEqual(comparison.generation_timeout("pydantic-ai", quarkus), 360)
        self.assertEqual(comparison.generation_timeout("langgraph", quarkus), 360)

    def test_langgraph_reason_codes_are_fixed_and_do_not_echo_errors(self):
        adapter = load_langgraph_adapter_without_dependencies()
        codes = ("missing_tool_call", "invalid_tool_arguments",
                 "missing_stage_fields", "invalid_test_path")
        for code in codes:
            with self.subTest(code=code):
                error = adapter.CandidateStageError(code)
                self.assertEqual(adapter.error_reason_code(error), code)
        self.assertEqual(adapter.error_reason_code(
            adapter.CandidateStageError("unit-test-secret")), "other")
        self.assertEqual(adapter.error_reason_code(ValueError("unit-test-secret")), "other")

        missing = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(tool_calls=[]))])
        with self.assertRaises(adapter.CandidateStageError) as failure:
            adapter.parse_candidate_response(missing, self.folder, "submit_analysis")
        self.assertEqual(failure.exception.reason_code, "missing_tool_call")
        invalid = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            tool_calls=[SimpleNamespace(type="function", function=SimpleNamespace(
                name="submit_analysis", arguments="not-json"))]))])
        with self.assertRaises(adapter.CandidateStageError) as failure:
            adapter.parse_candidate_response(invalid, self.folder, "submit_analysis")
        self.assertEqual(failure.exception.reason_code, "invalid_tool_arguments")

    def test_langgraph_error_record_and_response_diagnostics_exclude_raw_key(self):
        adapter = load_langgraph_adapter_without_dependencies()
        key = "unit-test-secret"
        fixture = self.folder / "adapter-fixture"
        fixture.mkdir()
        (fixture / "source.java").write_text("class Source {}")
        objective = self.folder / "objective.md"
        objective.write_text("test behavior")
        output = self.folder / "adapter-output"
        argv = ["adapter.py", "--fixture-root", str(fixture),
                "--objective", str(objective), "--output-dir", str(output),
                "--source-path", "source.java", "--stage", "analysis"]
        with patch.object(sys, "argv", argv), \
                patch.dict(os.environ, {"ENMAAS_API_KEY": key}), \
                patch.object(adapter, "write_evidence"), \
                patch.object(adapter, "run_candidate", side_effect=ValueError(key)):
            with redirect_stdout(io.StringIO()):
                status = adapter.main()
        self.assertEqual(status, 2)
        error = json.loads((output / "error.json").read_text())
        self.assertEqual(error, {"error_type": "ValueError", "reason_code": "other"})
        self.assertNotIn(key, (output / "error.json").read_text())

        known_output = self.folder / "known-error-output"
        known_argv = list(argv)
        known_argv[known_argv.index(str(output))] = str(known_output)
        with patch.object(sys, "argv", known_argv), \
                patch.dict(os.environ, {"ENMAAS_API_KEY": key}), \
                patch.object(adapter, "write_evidence"), \
                patch.object(adapter, "run_candidate", side_effect=adapter.CandidateStageError(
                    "missing_stage_fields")):
            with redirect_stdout(io.StringIO()):
                status = adapter.main()
        self.assertEqual(status, 2)
        known = json.loads((known_output / "error.json").read_text())
        self.assertEqual(known, {"error_type": "CandidateStageError",
                                 "reason_code": "missing_stage_fields"})
        self.assertNotIn(key, (known_output / "error.json").read_text())

        message = SimpleNamespace(content=key, tool_calls=[], reasoning_content=key,
                                  model_extra={})
        choice = SimpleNamespace(message=message, finish_reason=key)
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30,
                                completion_tokens_details=SimpleNamespace(reasoning_tokens=5))
        response = SimpleNamespace(choices=[choice], usage=usage)
        adapter.write_response_diagnostics(response, output)
        diagnostics = json.loads((output / "response-diagnostics.json").read_text())
        self.assertEqual(diagnostics["finish_reason"], "other")
        self.assertEqual(diagnostics["content_length"], len(key))
        self.assertEqual(diagnostics["tool_call_count"], 0)
        self.assertNotIn(key, (output / "response-diagnostics.json").read_text())

    def test_quarkus_profile_keeps_existing_unstaged_behavior(self):
        comparison.validate_stage_artifacts(self.folder, get_profile("quarkus"))


if __name__ == "__main__":
    unittest.main()
