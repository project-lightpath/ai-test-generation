"""Offline build readiness must be established before candidate execution."""

import argparse
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[2] / "experiments" / "harness"))

import run_comparison as comparison  # noqa: E402
from fixture_profiles import get_profile  # noqa: E402


class OfflinePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = self.root / "fixture"
        self.fixture.mkdir()
        (self.fixture / "pom.xml").write_text("<project/>")
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.args = argparse.Namespace(
            profile="quarkus-staged", fixture_root=self.fixture,
            maven_cache=self.cache, output_root=self.root / "output",
        )
        self.profile = get_profile("quarkus-staged")

    def fake_maven(self, status: str, exit_code: int):
        def run(workspace, label, phase, cache, image, class_name, profile):
            self.assertEqual((label, phase, cache, image, class_name, profile),
                             ("preflight", "verify", self.cache, self.profile.maven_image,
                              "", self.profile))
            self.assertTrue((workspace / "pom.xml").is_file())
            self.assertNotEqual(workspace, self.fixture)
            log = workspace.parent / "preflight-verify.log"
            log.write_text("Quarkus plugin unavailable offline\n")
            return {"status": status, "exit_code": exit_code, "log": str(log)}
        return run

    def test_readiness_verifies_disposable_fixture_with_offline_cache(self) -> None:
        with patch.object(comparison, "_run_maven_phase",
                          side_effect=self.fake_maven("passed", 0)) as maven:
            comparison.offline_maven_readiness(self.args, self.profile)
        maven.assert_called_once()
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         ["cache", "fixture"])

    def test_failed_verify_blocks_and_explains_missing_cache(self) -> None:
        with patch.object(comparison, "_run_maven_phase",
                          side_effect=self.fake_maven("failed", 1)):
            with self.assertRaisesRegex(ValueError,
                                        "offline Maven verify failed.*Quarkus plugin unavailable"):
                comparison.offline_maven_readiness(self.args, self.profile)

    def test_preflight_checks_maven_before_candidate_runtime(self) -> None:
        with patch.object(comparison, "tree_digest", return_value=self.profile.tree_sha256), \
                patch.object(comparison, "image_id",
                             side_effect=[comparison.PYTHON_IMAGE_ID,
                                          self.profile.maven_image_id]), \
                patch.object(comparison, "offline_maven_readiness",
                             side_effect=ValueError("offline Maven verify failed")) as verify, \
                patch.object(comparison, "container_version") as candidate_runtime:
            with self.assertRaisesRegex(ValueError, "offline Maven verify failed"):
                comparison.preflight(self.args, ["pydantic-ai"])
        verify.assert_called_once_with(self.args, self.profile)
        candidate_runtime.assert_not_called()
