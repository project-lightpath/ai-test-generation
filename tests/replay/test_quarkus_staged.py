"""Replay checks for the staged Quarkus blank-user comparison task."""

import argparse
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

HARNESS = pathlib.Path(__file__).parents[2] / "experiments" / "harness"
sys.path.insert(0, str(HARNESS))

from fixture_profiles import get_profile  # noqa: E402
import run_comparison as comparison  # noqa: E402


class QuarkusStagedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.folder = pathlib.Path(self.temporary_directory.name)
        self.profile = get_profile("quarkus-staged")

    def write_stages(self) -> None:
        analysis = {
            "relevant_files": ["src/main/java/com/projectlightpath/fixture/api/TransferResource.java"],
            "existing_coverage": "The existing test sends a nonempty user.",
            "gap": "No test sends an empty user string.",
            "feasibility": "feasible",
        }
        plan = {
            "test_path": "src/test/java/com/projectlightpath/fixture/TransferBlankUserTest.java",
            "setup": "POST an empty user to the Quarkus endpoint with RestAssured.",
            "assertions": ["HTTP 200", "Reference preserved", "accepted is false"],
            "baseline_expectation": "pass",
            "mutant_expectation": "assertion failure",
        }
        (self.folder / "analysis.json").write_text(json.dumps(analysis))
        (self.folder / "plan.json").write_text(json.dumps(plan))

    def make_workspace(self) -> tuple[pathlib.Path, argparse.Namespace]:
        fixture = self.folder / "fixture"
        for relative in self.profile.source_paths:
            source = fixture / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("supplied source: " + relative)
        candidate = self.folder / "candidate"
        candidate.mkdir()
        args = argparse.Namespace(fixture_root=fixture, profile=self.profile.name)
        with patch.object(comparison.os, "chown"):
            workspace = comparison.workspace_for(args, "claude-code", candidate)
        return workspace, args

    def test_profile_pins_staged_task_without_changing_original(self) -> None:
        original = get_profile("quarkus")
        self.assertTrue(self.profile.staged)
        self.assertFalse(original.staged)
        self.assertEqual(self.profile.revision, original.revision)
        self.assertEqual(self.profile.tree_sha256, original.tree_sha256)
        self.assertEqual(self.profile.maven_image_id, original.maven_image_id)
        self.assertEqual(self.profile.maven_options, original.maven_options)
        self.assertIn("pom.xml", self.profile.source_paths)
        self.assertEqual(comparison.sha256_file(self.profile.objective),
                         self.profile.objective_sha256)

    def test_analysis_and_plan_are_required_before_code(self) -> None:
        self.write_stages()
        planned = comparison.validate_stage_artifacts(self.folder, self.profile)
        self.assertEqual(str(planned),
                         "src/test/java/com/projectlightpath/fixture/TransferBlankUserTest.java")
        for stage in ("analysis.json", "plan.json"):
            with self.subTest(missing=stage):
                path = self.folder / stage
                original = path.read_bytes()
                path.unlink()
                with self.assertRaises(ValueError):
                    comparison.validate_stage_artifacts(self.folder, self.profile)
                path.write_bytes(original)

    def test_blocked_analysis_cannot_advance_to_code(self) -> None:
        self.write_stages()
        path = self.folder / "analysis.json"
        analysis = json.loads(path.read_text())
        analysis["feasibility"] = "blocked"
        path.write_text(json.dumps(analysis))
        with self.assertRaisesRegex(ValueError, "feasible execution"):
            comparison.validate_stage_artifacts(self.folder, self.profile)

    def test_analysis_rejects_early_test_or_production_change(self) -> None:
        for change in ("early-test", "production-edit"):
            with self.subTest(change=change):
                workspace, args = self.make_workspace()
                (workspace / "analysis.json").write_text('{"feasibility":"feasible"}')
                if change == "early-test":
                    new_test = workspace / "src/test/java/com/projectlightpath/fixture/EarlyTest.java"
                    new_test.parent.mkdir(parents=True, exist_ok=True)
                    new_test.write_text("class EarlyTest {}")
                else:
                    source = workspace / self.profile.mutation_path
                    source.write_text("modified production code")
                with self.assertRaises(ValueError):
                    comparison.validate_stage_workspace(workspace, args, "claude-code", "analysis")
                (self.folder / "candidate").rename(self.folder / ("checked-" + change))

    def test_reference_test_has_prior_differential_evidence(self) -> None:
        evidence = json.loads((HARNESS / "results/reference-blank-user-evaluation.json").read_text())
        self.assertEqual(evidence["fixture"]["verified_tree_sha256"],
                         self.profile.tree_sha256)
        self.assertEqual(evidence["mutation"]["path"], str(self.profile.mutation_path))
        self.assertEqual(evidence["mutation"]["old"], self.profile.original_line)
        self.assertEqual(evidence["mutation"]["new"], self.profile.mutated_line)
        self.assertEqual(evidence["cases"]["baseline"]["status"], "passed")
        self.assertEqual(evidence["cases"]["mutant"]["status"], "failed_assertion")
        self.assertEqual(evidence["result"], "discriminates")
