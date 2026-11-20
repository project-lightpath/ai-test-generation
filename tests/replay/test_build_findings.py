"""Saved-evidence report checks; no model, container, or network calls."""

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "experiments" / "harness"))
import build_findings as findings  # noqa: E402


class FindingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def run_root(self, name, runner="sha256:runner", outcome="discriminates"):
        root = self.root / name
        self.write_json(root / "summary.json", {
            "candidate_results": {"candidate-a": outcome},
            "run_id": name,
            "candidate_order": ["candidate-a"],
            "fixture_profile": "spring-transfer",
            "fixture_tree_sha256": "sha256:fixture",
            "objective_sha256": "sha256:objective",
            "model_id": "model", "endpoint_id": "endpoint",
            "runner_sha256": runner, "evaluator_sha256": "sha256:evaluator",
            "evidence_scan": "passed",
        })
        folder = root / "candidate-a"
        self.write_json(folder / "generation.json", {
            "status": "completed", "elapsed_seconds": 7,
        })
        self.write_json(folder / "invocation.json", {
            "config_sha256": "sha256:config",
            "prompt_sha256": "sha256:prompt",
            "tool_source_revision": "sha256:tool", "tool_version": "1",
        })
        self.write_json(folder / "evaluation/evaluation.json", {
            "test": {"path": "src/test/java/sample/CandidateTest.java"},
            "result": outcome,
            "cases": {"baseline": {"status": "passed"},
                      "mutant": {"status": "failed_assertion"}},
        })
        test = folder / "workspace/src/test/java/sample/CandidateTest.java"
        test.parent.mkdir(parents=True)
        test.write_text("class CandidateTest {\n\n  void test() {}\n}\n")
        evaluation_path = folder / "evaluation/evaluation.json"
        evaluation = json.loads(evaluation_path.read_text())
        evaluation["test"]["sha256"] = "sha256:" + hashlib.sha256(test.read_bytes()).hexdigest()
        self.write_json(evaluation_path, evaluation)
        return root

    def write_manifest(self, root):
        files = {}
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.name != "archive-manifest.json":
                content = path.read_bytes()
                files[path.relative_to(root).as_posix()] = {
                    "bytes": len(content), "sha256": "sha256:" + hashlib.sha256(content).hexdigest()}
        summary = json.loads((root / "summary.json").read_text())
        self.write_json(root / "archive-manifest.json", {
            "schema": "flpath-5050.evidence-archive.v1", "run_id": summary["run_id"],
            "evidence_scan": "passed", "file_count": len(files),
            "total_bytes": sum(item["bytes"] for item in files.values()), "files": files})

    def verified_run_root(self, name, analysis_value="analysis", profile="spring-transfer",
                          config_override=None):
        root = self.run_root(name)
        summary_path = root / "summary.json"
        summary = json.loads(summary_path.read_text())
        summary["fixture_profile"] = profile
        summary["fixture_tree_sha256"] = "fixture:" + profile
        summary["objective_sha256"] = "objective:" + profile
        spec = root / "run-spec.json"
        self.write_json(spec, {"profile": profile, "candidates": ["candidate-a"],
                               "model_id": "model", "timeout": 600})
        summary["run_spec_sha256"] = findings.digest(spec)
        self.write_json(summary_path, summary)
        folder = root / "candidate-a"
        self.write_json(folder / "analysis.json", {"value": analysis_value})
        self.write_json(folder / "plan.json", {"value": "plan-" + analysis_value})
        for stage in ("analysis", "plan", "evaluation"):
            self.write_json(folder / (stage + "-run.json"), {"status": "completed", "elapsed_ms": 1000})
        self.write_json(folder / "generation.json", {"status": "completed", "elapsed_ms": 7000})
        config = {"candidate": "candidate-a", "model": "model", "version": "1",
                  "stage_sha256": {"analysis.json": findings.digest(folder / "analysis.json"),
                                   "plan.json": findings.digest(folder / "plan.json")}}
        if config_override:
            config.update(config_override)
        self.write_json(folder / "sanitized-config.json", config)
        invocation_path = folder / "invocation.json"
        invocation = json.loads(invocation_path.read_text())
        invocation.update({"candidate": "candidate-a", "model_id": summary["model_id"],
                           "endpoint_id": summary["endpoint_id"],
                           "prompt_sha256": summary["objective_sha256"]})
        invocation["config_sha256"] = findings.digest(folder / "sanitized-config.json")
        self.write_json(invocation_path, invocation)
        previous = None
        output_paths = {
            "analysis": ["analysis-run.json", "analysis.json"],
            "plan": ["plan-run.json", "plan.json"],
            "code": ["generation.json", "workspace/src/test/java/sample/CandidateTest.java"],
            "execute": ["evaluation-run.json", "evaluation/evaluation.json"],
        }
        for stage in findings.STAGES:
            path = root / "checkpoints/candidate-a" / (stage + ".json")
            self.write_json(path, {
                "schema": "flpath-5050.stage-checkpoint.v1", "candidate": "candidate-a",
                "stage": stage, "status": "completed",
                "result": "discriminates" if stage == "execute" else None,
                "previous_checkpoint_sha256": previous,
                "fixture_tree_sha256": summary["fixture_tree_sha256"],
                "objective_sha256": summary["objective_sha256"],
                "model_id": summary["model_id"], "endpoint_id": summary["endpoint_id"],
                "run_spec_sha256": summary.get("run_spec_sha256"),
                "outputs": {value: findings.digest(folder / value) for value in output_paths[stage]},
            })
            previous = findings.digest(path)
        self.write_manifest(root)
        return root

    def test_reports_generated_size_and_unknown_coverage(self):
        run = findings.load_run(self.run_root("one"))
        attempt = run["candidates"][0]
        self.assertEqual(4, attempt["generated_test"]["physical_lines"])
        self.assertEqual(3, attempt["generated_test"]["nonblank_lines"])
        self.assertEqual("unknown", attempt["coverage"]["status"])
        self.assertEqual("failed_assertion", attempt["case_statuses"]["mutant"])
        self.assertEqual(7000, attempt["stages_ms"]["code"])
        self.assertTrue(any(ref["path"].endswith("evaluation.json")
                            for ref in attempt["evidence"]))

    def test_tampered_generated_file_invalidates_success(self):
        root = self.run_root("one")
        test = root / "candidate-a/workspace/src/test/java/sample/CandidateTest.java"
        test.write_text("class ChangedTest {}\n")
        attempt = findings.load_run(root)["candidates"][0]
        self.assertEqual("invalid_evidence", attempt["outcome"])
        self.assertIn("generated_test_hash_mismatch", attempt["integrity_issues"])

    def test_summary_success_requires_differential_evaluator(self):
        root = self.run_root("one")
        path = root / "candidate-a/evaluation/evaluation.json"
        evaluation = json.loads(path.read_text())
        evaluation["cases"]["mutant"]["status"] = "passed"
        self.write_json(path, evaluation)
        attempt = findings.load_run(root)["candidates"][0]
        self.assertEqual("invalid_evidence", attempt["outcome"])
        self.assertIn("differential_cases_do_not_support_success", attempt["integrity_issues"])

    def test_saved_run_spec_separates_external_image_revisions(self):
        one = self.run_root("one")
        two = self.run_root("two")
        self.write_json(one / "run-spec.json", {"image_id": "sha256:first"})
        self.write_json(two / "run-spec.json", {"image_id": "sha256:second"})
        report = findings.findings([findings.load_run(one), findings.load_run(two)])
        self.assertEqual(2, report["cohort_count"])
        self.assertEqual(2, report["decisions"]["candidate-a"]["comparable_identity_count"])

    def test_changed_runner_is_not_aggregated_as_repeat(self):
        a = findings.load_run(self.run_root("one"))
        b = findings.load_run(self.run_root("two", runner="sha256:changed"))
        report = findings.findings([a, b])
        self.assertEqual(2, report["cohort_count"])
        decision = report["decisions"]["candidate-a"]
        self.assertEqual(2, decision["comparable_identity_count"])
        self.assertIsNone(decision["complete_wall_ms_median"])
        self.assertEqual("insufficient_evidence", decision["status"])

    def test_same_identity_can_aggregate_complete_timings(self):
        a = findings.load_run(self.verified_run_root("one", analysis_value="first"))
        b = findings.load_run(self.verified_run_root("two", analysis_value="different generated analysis"))
        report = findings.findings([a, b])
        self.assertEqual(10000, report["decisions"]["candidate-a"]["complete_wall_ms_median"])

    def test_changed_candidate_order_is_not_pooled(self):
        a = findings.load_run(self.run_root("one"))
        b = findings.load_run(self.run_root("two"))
        b["candidate_order"] = ["other", "candidate-a"]
        for run in (a, b):
            run["candidates"][0]["stages_ms"].update(
                {"analysis": 1000, "plan": 1000, "execute": 1000})
        decision = findings.findings([a, b])["decisions"]["candidate-a"]
        self.assertFalse(decision["same_candidate_order"])
        self.assertIsNone(decision["complete_wall_ms_median"])

    def test_duplicate_run_id_is_not_independent_repeat(self):
        a = findings.load_run(self.run_root("one"))
        b = findings.load_run(self.run_root("two"))
        b["run_id"] = a["run_id"]
        for run in (a, b):
            run["candidates"][0]["stages_ms"].update(
                {"analysis": 1000, "plan": 1000, "execute": 1000})
        decision = findings.findings([a, b])["decisions"]["candidate-a"]
        self.assertFalse(decision["independent_run_ids"])
        self.assertIsNone(decision["complete_wall_ms_median"])

    def test_archive_tamper_and_unlisted_files_invalidate_evidence(self):
        root = self.verified_run_root("one")
        self.assertEqual("verified", findings.load_run(root)["candidates"][0]["integrity_status"])
        (root / "candidate-a/analysis-run.json").write_text('{"elapsed_ms": 2000}')
        attempt = findings.load_run(root)["candidates"][0]
        self.assertEqual("invalid", attempt["integrity_status"])
        self.assertEqual("invalid_evidence", attempt["outcome"])
        root2 = self.verified_run_root("two")
        (root2 / "unlisted.json").write_text("{}")
        self.assertEqual("invalid", findings.load_run(root2)["archive_integrity"]["status"])

    def test_checkpoint_chain_and_outputs_verified_even_if_manifest_rewritten(self):
        root = self.verified_run_root("one")
        path = root / "candidate-a/analysis.json"
        path.write_text('{"value":"tampered"}')
        self.write_manifest(root)
        attempt = findings.load_run(root)["candidates"][0]
        self.assertTrue(any(item.startswith("checkpoint_output_hash_mismatch:analysis")
                            for item in attempt["integrity_issues"]))
        root2 = self.verified_run_root("two")
        checkpoint = root2 / "checkpoints/candidate-a/plan.json"
        record = json.loads(checkpoint.read_text())
        record["previous_checkpoint_sha256"] = "sha256:tampered"
        self.write_json(checkpoint, record)
        self.write_manifest(root2)
        attempt = findings.load_run(root2)["candidates"][0]
        self.assertTrue(any(item.startswith("checkpoint_chain_mismatch:plan")
                            for item in attempt["integrity_issues"]))

    def test_checkpoint_candidate_and_spec_bindings(self):
        for field, value in (("candidate", "different"), ("stage", "execute"),
                             ("run_spec_sha256", "sha256:other"),
                             ("model_id", "another-model")):
            with self.subTest(field=field):
                root = self.verified_run_root("case-" + field)
                path = root / "checkpoints/candidate-a/analysis.json"
                record = json.loads(path.read_text())
                record[field] = value
                self.write_json(path, record)
                self.write_manifest(root)
                attempt = findings.load_run(root)["candidates"][0]
                self.assertEqual("invalid", attempt["integrity_status"])
                self.assertTrue(any(item.startswith(("checkpoint_identity_invalid",
                                                     "checkpoint_binding_mismatch"))
                                    for item in attempt["integrity_issues"]))

    def test_legacy_evidence_and_missing_checkpoints_are_unverified(self):
        root = self.run_root("legacy")
        run = findings.load_run(root)
        self.assertEqual("unverified", run["archive_integrity"]["status"])
        self.assertEqual("unverified", run["candidates"][0]["integrity_status"])
        root2 = self.verified_run_root("partial")
        (root2 / "checkpoints/candidate-a/execute.json").unlink()
        self.write_manifest(root2)
        self.assertEqual("unverified", findings.load_run(root2)["candidates"][0]["integrity_status"])

    def test_manifest_rejects_traversal_missing_file_and_symlink(self):
        root = self.verified_run_root("one")
        manifest_path = root / "archive-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["../escape"] = {"bytes": 0, "sha256": "sha256:bad"}
        manifest["file_count"] += 1
        self.write_json(manifest_path, manifest)
        self.assertEqual("invalid", findings.load_run(root)["archive_integrity"]["status"])
        root2 = self.verified_run_root("two")
        (root2 / "candidate-a/analysis.json").unlink()
        self.assertEqual("invalid", findings.load_run(root2)["archive_integrity"]["status"])
        root3 = self.verified_run_root("three")
        path = root3 / "candidate-a/analysis.json"
        path.unlink()
        path.symlink_to(root3 / "candidate-a/plan.json")
        self.assertEqual("invalid", findings.load_run(root3)["archive_integrity"]["status"])

    def test_config_hash_mismatch_and_missing_config_cannot_be_compared(self):
        root = self.verified_run_root("one")
        config = root / "candidate-a/sanitized-config.json"
        config.write_text('{"candidate":"changed"}')
        self.write_manifest(root)
        attempt = findings.load_run(root)["candidates"][0]
        self.assertEqual("invalid", attempt["integrity_status"])
        self.assertIn("invocation_config_hash_mismatch", attempt["integrity_issues"])
        root2 = self.verified_run_root("two")
        (root2 / "candidate-a/sanitized-config.json").unlink()
        self.write_manifest(root2)
        attempt2 = findings.load_run(root2)["candidates"][0]
        self.assertEqual("unverified", attempt2["integrity_status"])
        self.assertIsNone(attempt2["invocation_identity"]["stable_config_sha256"])

    def test_repeats_aggregate_per_task_but_not_across_tasks(self):
        roots = [self.verified_run_root("spring1", "one", "spring"),
                 self.verified_run_root("spring2", "two", "spring"),
                 self.verified_run_root("quarkus1", "three", "quarkus"),
                 self.verified_run_root("quarkus2", "four", "quarkus")]
        report = findings.findings([findings.load_run(root) for root in roots])
        decision = report["decisions"]["candidate-a"]
        self.assertEqual(2, len(decision["cohorts"]))
        for cohort in decision["cohorts"]:
            self.assertEqual(2, cohort["attempts"])
            self.assertEqual(10000, cohort["configuration_groups"][0]["complete_wall_ms_median"])
        self.assertIsNone(decision["complete_wall_ms_median"])

    def test_timeout_kept_in_task_denominator(self):
        runs = [findings.load_run(self.verified_run_root("one", "first")),
                findings.load_run(self.verified_run_root("two", "second")),
                findings.load_run(self.verified_run_root("three", "third"))]
        blocked = runs[2]["candidates"][0]
        blocked["outcome"] = "blocked"
        blocked["reported_outcome"] = "blocked"
        blocked["integrity_status"] = "unverified"
        blocked["invocation_identity"]["config_status"] = "unverified"
        blocked["invocation_identity"]["stable_config_sha256"] = None
        report = findings.findings(runs)
        cohort = report["decisions"]["candidate-a"]["cohorts"][0]
        self.assertEqual(3, cohort["attempts"])
        self.assertEqual(2, cohort["discriminating_attempts"])
        self.assertEqual(1, cohort["unknown_configuration_attempts"])

    def test_missing_run_spec_prevents_timing_aggregation(self):
        roots = [self.verified_run_root("one"), self.verified_run_root("two")]
        for root in roots:
            summary_path = root / "summary.json"
            summary = json.loads(summary_path.read_text())
            summary.pop("run_spec_sha256")
            self.write_json(summary_path, summary)
            (root / "run-spec.json").unlink()
            previous = None
            for stage in findings.STAGES:
                checkpoint = root / "checkpoints/candidate-a" / (stage + ".json")
                record = json.loads(checkpoint.read_text())
                record["run_spec_sha256"] = None
                record["previous_checkpoint_sha256"] = previous
                self.write_json(checkpoint, record)
                previous = findings.digest(checkpoint)
            self.write_manifest(root)
        decision = findings.findings([findings.load_run(root) for root in roots])["decisions"]["candidate-a"]
        self.assertIsNone(decision["complete_wall_ms_median"])
        self.assertFalse(decision["cohorts"][0]["configuration_groups"][0]["configuration_complete"])

    def test_saved_stage_hashes_must_match_generation_configuration(self):
        root = self.verified_run_root("one")
        folder = root / "candidate-a"
        config_path = folder / "sanitized-config.json"
        config = json.loads(config_path.read_text())
        config["stage_sha256"]["analysis.json"] = "sha256:wrong"
        self.write_json(config_path, config)
        invocation_path = folder / "invocation.json"
        invocation = json.loads(invocation_path.read_text())
        invocation["config_sha256"] = findings.digest(config_path)
        self.write_json(invocation_path, invocation)
        self.write_manifest(root)
        attempt = findings.load_run(root)["candidates"][0]
        self.assertEqual("invalid", attempt["integrity_status"])
        self.assertIn("invocation_config_hash_mismatch", attempt["integrity_issues"])

    def test_candidate_dependency_changes_prevent_same_config_grouping(self):
        folder = self.root / "pydantic-ai"
        self.write_json(folder / "analysis.json", {"a": 1})
        self.write_json(folder / "plan.json", {"p": 1})
        config = {"candidate": "pydantic-ai",
                  "stage_sha256": {name: findings.digest(folder / name)
                                   for name in ("analysis.json", "plan.json")}}
        self.write_json(folder / "sanitized-config.json", config)
        invocation = {"candidate": "pydantic-ai",
                      "config_sha256": findings.digest(folder / "sanitized-config.json")}
        first = findings.stable_invocation_identity(folder, invocation)
        self.assertEqual("unverified", first["config_status"])
        dependencies = folder / "pip-freeze.log"
        dependencies.write_text("pydantic-ai==1\n")
        first = findings.stable_invocation_identity(folder, invocation)
        dependencies.write_text("pydantic-ai==2\n")
        second = findings.stable_invocation_identity(folder, invocation)
        self.assertEqual("verified", first["config_status"])
        self.assertNotEqual(first["dependencies_sha256"], second["dependencies_sha256"])

    def test_blocked_prefix_with_missing_output_is_not_verified(self):
        root = self.verified_run_root("one")
        summary_path = root / "summary.json"
        summary = json.loads(summary_path.read_text())
        summary["candidate_results"]["candidate-a"] = "blocked"
        self.write_json(summary_path, summary)
        plan = root / "checkpoints/candidate-a/plan.json"
        record = json.loads(plan.read_text())
        record["status"] = "blocked"
        record["outputs"] = {}
        self.write_json(plan, record)
        (root / "checkpoints/candidate-a/code.json").unlink()
        (root / "checkpoints/candidate-a/execute.json").unlink()
        (root / "candidate-a/analysis.json").unlink()
        self.write_manifest(root)
        attempt = findings.load_run(root)["candidates"][0]
        self.assertEqual("unverified", attempt["integrity_status"])
        self.assertEqual("blocked", attempt["outcome"])

    def test_blocked_prefix_without_terminal_marker_is_not_verified(self):
        root = self.run_root("timeout", outcome="blocked")
        summary = json.loads((root / "summary.json").read_text())
        path = root / "checkpoints/candidate-a/analysis.json"
        self.write_json(path, {"schema": "flpath-5050.stage-checkpoint.v1",
                               "candidate": "candidate-a", "stage": "analysis", "status": "blocked",
                               "previous_checkpoint_sha256": None,
                               "fixture_tree_sha256": summary["fixture_tree_sha256"],
                               "objective_sha256": summary["objective_sha256"],
                               "model_id": summary["model_id"], "endpoint_id": summary["endpoint_id"],
                               "run_spec_sha256": None, "outputs": {}})
        self.write_manifest(root)
        self.assertEqual("unverified", findings.load_run(root)["candidates"][0]["integrity_status"])

    def test_review_can_include_verified_partial_timeout(self):
        good = findings.load_run(self.verified_run_root("good"))
        root = self.run_root("timeout", outcome="blocked")
        summary = json.loads((root / "summary.json").read_text())
        folder = root / "candidate-a"
        self.write_json(folder / "analysis-run.json", {"status": "timeout", "elapsed_ms": 600000})
        path = root / "checkpoints/candidate-a/analysis.json"
        self.write_json(path, {"schema": "flpath-5050.stage-checkpoint.v1",
                               "candidate": "candidate-a", "stage": "analysis", "status": "blocked",
                               "result": None, "previous_checkpoint_sha256": None,
                               "fixture_tree_sha256": summary["fixture_tree_sha256"],
                               "objective_sha256": summary["objective_sha256"],
                               "model_id": summary["model_id"], "endpoint_id": summary["endpoint_id"],
                               "run_spec_sha256": None, "outputs": {}})
        self.write_manifest(root)
        timeout = findings.load_run(root)
        self.assertEqual("verified_partial", timeout["candidates"][0]["integrity_status"])
        entry = {"status": "use", "reason": "Reviewer considered timeout", "decision_id": "x",
                 "authorization_ref": "FLPATH-5050",
                 "by_stage": {stage: "use" for stage in findings.STAGES}}
        review = {"reviewer": "QE", "reviewer_role": "QE", "source_sha256": "x",
                  "candidates": {"candidate-a": entry}}
        result = findings.findings([good, timeout], review)["decisions"]["candidate-a"]
        self.assertEqual("use", result["status"])
        self.assertEqual(2, result["attempts"])
        self.assertEqual(1, result["discriminating_attempts"])

    def test_duplicate_run_ids_across_tasks_are_not_independent(self):
        runs = [findings.load_run(self.verified_run_root("one", "first", "spring")),
                findings.load_run(self.verified_run_root("two", "second", "spring")),
                findings.load_run(self.verified_run_root("three", "third", "quarkus"))]
        runs[2]["run_id"] = runs[0]["run_id"]
        decision = findings.findings(runs)["decisions"]["candidate-a"]
        spring = next(c for c in decision["cohorts"] if c["identity"]["fixture_profile"] == "spring")
        self.assertFalse(spring["independent_run_ids"])
        self.assertIsNone(spring["configuration_groups"][0]["complete_wall_ms_median"])

    def test_legacy_nonstaged_checkpoints_and_summary_spec_fallback(self):
        root = self.verified_run_root("legacy")
        summary_path = root / "summary.json"
        summary = json.loads(summary_path.read_text())
        summary.pop("run_spec_sha256")
        self.write_json(summary_path, summary)
        # Older non-staged runs legitimately start at Code with no Plan checkpoint.
        (root / "checkpoints/candidate-a/analysis.json").unlink()
        (root / "checkpoints/candidate-a/plan.json").unlink()
        code = root / "checkpoints/candidate-a/code.json"
        record = json.loads(code.read_text())
        record["previous_checkpoint_sha256"] = None
        self.write_json(code, record)
        execute = root / "checkpoints/candidate-a/execute.json"
        record = json.loads(execute.read_text())
        record["previous_checkpoint_sha256"] = findings.digest(code)
        self.write_json(execute, record)
        self.write_manifest(root)
        run = findings.load_run(root)
        attempt = run["candidates"][0]
        self.assertEqual("discriminates", attempt["outcome"])
        self.assertEqual("unverified", attempt["integrity_status"])
        self.assertEqual([], attempt["integrity_issues"])

    def test_changed_stable_configuration_not_pooled(self):
        a = findings.load_run(self.verified_run_root("one"))
        b = findings.load_run(self.verified_run_root("two", config_override={"model": "other"}))
        decision = findings.findings([a, b])["decisions"]["candidate-a"]
        self.assertEqual(2, len(decision["cohorts"][0]["configuration_groups"]))
        self.assertIsNone(decision["complete_wall_ms_median"])

    def test_positive_decision_rejects_legacy_unverified_evidence(self):
        run = findings.load_run(self.run_root("legacy"))
        entry = {"status": "use", "reason": "claim", "decision_id": "x",
                 "authorization_ref": "FLPATH-5050",
                 "by_stage": {stage: "use" for stage in findings.STAGES}}
        review = {"reviewer": "QE", "reviewer_role": "QE", "source_sha256": "x",
                  "candidates": {"candidate-a": entry}}
        with self.assertRaisesRegex(ValueError, "verified evidence"):
            findings.findings([run], review)

    def test_replay_packet_needs_source_manifest(self):
        root = self.run_root("one")
        summary = json.loads((root / "summary.json").read_text())
        summary["run_kind"] = "evaluation_replay"
        summary["source_run_summary_sha256"] = "sha256:source"
        self.write_json(root / "summary.json", summary)
        with self.assertRaisesRegex(ValueError, "source linkage"):
            findings.load_run(root)

    def test_rejects_candidate_path_traversal(self):
        root = self.run_root("one")
        record = json.loads((root / "summary.json").read_text())
        record["candidate_results"] = {"../outside": "discriminates"}
        self.write_json(root / "summary.json", record)
        with self.assertRaisesRegex(ValueError, "invalid candidate results"):
            findings.load_run(root)

    def test_review_needs_authorization_reference(self):
        self.run_root("one")
        path = self.root / "review.json"
        self.write_json(path, {"schema": "flpath-5050.decision-review.v1",
                               "reviewer": "QE", "reviewer_role": "quality_engineer",
                               "candidates": {"candidate-a": {
                                   "status": "reject", "reason": "Not suitable",
                                   "decision_id": "decision-one",
                                   "by_stage": {stage: "reject" for stage in findings.STAGES}}}})
        with self.assertRaisesRegex(ValueError, "authorization_ref"):
            findings.reviewed_decisions(path, ["candidate-a"])

    def test_positive_review_requires_discriminating_attempt(self):
        root = self.run_root("one", outcome="blocked")
        path = self.root / "review.json"
        self.write_json(path, {"schema": "flpath-5050.decision-review.v1",
                               "reviewer": "QE", "reviewer_role": "quality_engineer",
                               "candidates": {
                                   "candidate-a": {"status": "use", "reason": "Claimed ready",
                                                   "decision_id": "decision-one",
                                                   "authorization_ref": "FLPATH-5050",
                                                   "by_stage": {stage: "use"
                                                                for stage in findings.STAGES}}}})
        review = findings.reviewed_decisions(path, ["candidate-a"])
        with self.assertRaisesRegex(ValueError, "discriminating"):
            findings.findings([findings.load_run(root)], review)

    def test_reviewed_use_cannot_override_unresolved_stage(self):
        self.run_root("one")
        path = self.root / "review.json"
        self.write_json(path, {"schema": "flpath-5050.decision-review.v1",
                               "reviewer": "QE", "reviewer_role": "quality_engineer",
                               "candidates": {
                                   "candidate-a": {"status": "use", "reason": "Checked",
                                                   "decision_id": "decision-one",
                                                   "authorization_ref": "FLPATH-5050",
                                                   "by_stage": {stage: "insufficient_evidence"
                                                                for stage in findings.STAGES}}}})
        with self.assertRaisesRegex(ValueError, "conflicts"):
            findings.reviewed_decisions(path, ["candidate-a"])

    def test_reviewed_fork_decision_requires_change_list(self):
        self.verified_run_root("one")
        path = self.root / "review.json"
        stages = {stage: "use_with_fork" for stage in findings.STAGES}
        self.write_json(path, {"schema": "flpath-5050.decision-review.v1",
                               "reviewer": "QE", "reviewer_role": "quality_engineer",
                               "candidates": {
                                   "candidate-a": {"status": "use_with_fork",
                                                   "reason": "Bounded change needed",
                                                   "decision_id": "decision-one",
                                                   "authorization_ref": "FLPATH-5050",
                                                   "by_stage": stages}}})
        with self.assertRaisesRegex(ValueError, "required changes"):
            findings.reviewed_decisions(path, ["candidate-a"])
        value = json.loads(path.read_text())
        value["candidates"]["candidate-a"]["required_changes"] = ["Add bounded retries"]
        self.write_json(path, value)
        review = findings.reviewed_decisions(path, ["candidate-a"])
        report = findings.findings([findings.load_run(self.root / "one")], review)
        self.assertEqual("use_with_fork", report["decisions"]["candidate-a"]["status"])
        self.assertEqual("QE", report["decisions"]["candidate-a"]["reviewer"])



if __name__ == "__main__":
    unittest.main()
