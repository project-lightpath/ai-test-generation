import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "experiments" / "harness"))

import fake_model_replay as replay  # noqa: E402


class ReplayStateTests(unittest.TestCase):
    def test_replay_state_rejects_skipped_stage_and_keeps_explicit_order(self):
        state = replay.ReplayState()
        with self.assertRaisesRegex(replay.ReplayInputError, "Analyze -> Plan"):
            state.advance("code", "generated")

        state.advance("analyze", "replayed")
        state.advance("plan", "synthetic_replay_input")
        state.advance("code", "generated")
        state.advance("execute", "not_run")
        self.assertEqual([event["stage"] for event in state.events], ["analyze", "plan", "code", "execute"])


class FakeModelReplayTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary_directory.name)
        self.fixture = self.root / "fixture"
        self.fixture.mkdir()
        (self.fixture / "pom.xml").write_text(
            "<java.release>21</java.release>\n<quarkus.platform.version>3.40.1</quarkus.platform.version>"
        )
        endpoint = self.fixture / "src/main/java/com/projectlightpath/fixture/api/TransferResource.java"
        endpoint.parent.mkdir(parents=True)
        endpoint.write_text("class TransferResource {}\n")
        target = self.fixture / "src/main/java/com/projectlightpath/fixture/target/ServerUserAuthService.java"
        target.parent.mkdir(parents=True)
        target.write_text("class ServerUserAuthService {}\n")
        self.case = json.loads(replay.CASE_PATH.read_text())
        self.case_path = self.root / "case.json"
        self.write_case(self.case)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_case(self, case):
        self.case_path.write_text(json.dumps(case))

    def test_replay_generates_identical_test_and_record_in_fresh_workspaces(self):
        first = replay.replay(self.case_path, self.fixture, self.root / "first")
        second = replay.replay(self.case_path, self.fixture, self.root / "second")

        self.assertEqual(first, second)
        first_test = self.root / "first" / first["generated_test"]["path"]
        second_test = self.root / "second" / second["generated_test"]["path"]
        self.assertEqual(first_test.read_bytes(), second_test.read_bytes())
        self.assertTrue((self.root / "first/src/main/java/com/projectlightpath/fixture/target/ServerUserAuthService.java").is_file())
        self.assertEqual(first["stages"][-1]["status"], "not_run")

    def test_replay_rejects_path_traversal(self):
        self.case["generated_test"]["path"] = "src/test/java/../../pom.xml"
        self.write_case(self.case)

        with self.assertRaisesRegex(replay.ReplayInputError, "stay under src/test/java"):
            replay.replay(self.case_path, self.fixture, self.root / "unsafe")

    def test_replay_rejects_test_that_does_not_assert_objective(self):
        with patch.object(replay, "MODEL_SOURCE_ROOT", self.root):
            (self.root / "TransferGeneratedReplayTest.java").write_text("class EmptyTest {}\n")
            with self.assertRaisesRegex(replay.ReplayInputError, "does not assert"):
                replay.replay(self.case_path, self.fixture, self.root / "weak")

    def test_replay_rejects_unpinned_fixture_revision(self):
        self.case["fixture"]["revision"] = "main"
        self.write_case(self.case)

        with self.assertRaisesRegex(replay.ReplayInputError, "revision"):
            replay.replay(self.case_path, self.fixture, self.root / "stale")

    def test_replay_rejects_output_directory_that_already_exists(self):
        output = self.root / "existing"
        output.mkdir()

        with self.assertRaisesRegex(replay.ReplayInputError, "must be new"):
            replay.replay(self.case_path, self.fixture, output)


if __name__ == "__main__":
    unittest.main()
