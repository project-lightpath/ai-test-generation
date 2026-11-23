"""Archive selection and safety checks; no network, model, or Maven calls."""

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "experiments" / "harness"))
import archive_evidence  # noqa: E402
import build_findings  # noqa: E402


class ArchiveEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def record(self, path: pathlib.Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def run_root(self, scanned: bool = True) -> pathlib.Path:
        root = self.base / "run"
        self.record(root / "summary.json", {
            "evidence_scan": "passed" if scanned else "blocked",
            "run_id": "trial-1",
            "candidate_results": {"candidate-a": "discriminates"},
        })
        self.record(root / "prepare.json", {"status": "passed"})
        self.record(root / "run-spec.json", {"fixture": "pinned"})
        folder = root / "candidate-a"
        test = folder / "generated/src/test/java/sample/CandidateTest.java"
        test.parent.mkdir(parents=True)
        test.write_text("class CandidateTest {}\n")
        self.record(folder / "evaluation/evaluation.json", {
            "result": "discriminates",
            "test": {"path": "src/test/java/sample/CandidateTest.java",
                     "sha256": "sha256:" + hashlib.sha256(test.read_bytes()).hexdigest()},
            "cases": {"baseline": {"status": "passed"},
                      "mutant": {"status": "failed_assertion"}},
        })
        self.record(folder / "generation.json", {"status": "completed"})
        self.record(folder / "generated/invocation.json", {"tool_version": "1"})
        (folder / "generation.log").write_text("bounded log\n")
        # This legacy non-staged packet has Code and Execute checkpoints only.
        # The records use the real schema so the reporter can validate retained
        # links while correctly marking the missing Analyze/Plan proof unverified.
        self.record(folder / "evaluation-run.json", {"status": "completed"})
        spec_hash = "sha256:" + hashlib.sha256((root / "run-spec.json").read_bytes()).hexdigest()
        common = {"schema": "flpath-5050.stage-checkpoint.v1", "candidate": "candidate-a",
                  "status": "completed", "fixture_tree_sha256": None,
                  "objective_sha256": None, "model_id": None, "endpoint_id": None,
                  "run_spec_sha256": spec_hash}
        code = root / "checkpoints/candidate-a/code.json"
        self.record(code, {**common, "stage": "code", "previous_checkpoint_sha256": None,
                           "outputs": {
                               "generation.json": build_findings.digest(folder / "generation.json"),
                               "generated/src/test/java/sample/CandidateTest.java":
                                   build_findings.digest(test)}})
        self.record(root / "checkpoints/candidate-a/execute.json",
                    {**common, "stage": "execute", "result": "discriminates",
                     "previous_checkpoint_sha256": build_findings.digest(code),
                     "outputs": {
                         "evaluation-run.json": build_findings.digest(folder / "evaluation-run.json"),
                         "evaluation/evaluation.json":
                             build_findings.digest(folder / "evaluation/evaluation.json")}})
        xml = folder / "evaluation/baseline/target/site/jacoco/jacoco.xml"
        xml.parent.mkdir(parents=True)
        xml.write_text("<report/>")
        bulk = folder / "evaluation/baseline/target/classes/Example.class"
        bulk.parent.mkdir(parents=True)
        bulk.write_bytes(b"bulk")
        return root

    def test_exports_reportable_selected_evidence(self) -> None:
        root = self.run_root()
        output = self.base / "export"
        archive_evidence.archive(root, output)
        self.assertTrue((output / "candidate-a/generated/src/test/java/sample/CandidateTest.java").is_file())
        self.assertTrue((output / "candidate-a/evaluation/baseline/target/site/jacoco/jacoco.xml").is_file())
        self.assertFalse((output / "candidate-a/evaluation/baseline/target/classes/Example.class").exists())
        report = build_findings.load_run(output)
        self.assertEqual("discriminates", report["candidates"][0]["outcome"])
        self.assertEqual("unverified", report["candidates"][0]["integrity_status"])
        self.assertEqual([], report["candidates"][0]["integrity_issues"])
        self.assertTrue((output / "checkpoints/candidate-a/code.json").is_file())
        self.assertTrue((output / "checkpoints/candidate-a/execute.json").is_file())
        manifest = json.loads((output / "archive-manifest.json").read_text())
        entry = manifest["files"]["summary.json"]
        self.assertEqual("sha256:" + hashlib.sha256((output / "summary.json").read_bytes()).hexdigest(),
                         entry["sha256"])

    def test_requires_passed_source_scan(self) -> None:
        root = self.run_root(scanned=False)
        with self.assertRaisesRegex(ValueError, "passed credential evidence scan"):
            archive_evidence.archive(root, self.base / "export")
        self.assertFalse((self.base / "export").exists())

    def test_rejects_linked_generated_test(self) -> None:
        root = self.run_root()
        test = root / "candidate-a/generated/src/test/java/sample/CandidateTest.java"
        test.unlink()
        test.symlink_to(root / "summary.json")
        with self.assertRaisesRegex(ValueError, "linked evidence"):
            archive_evidence.archive(root, self.base / "export")
        self.assertFalse((self.base / "export").exists())

    def test_rejects_oversized_selected_file(self) -> None:
        root = self.run_root()
        log = root / "candidate-a/generation.log"
        log.write_bytes(b"x" * (archive_evidence.MAX_FILE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "oversized evidence"):
            archive_evidence.archive(root, self.base / "export")
        self.assertFalse((self.base / "export").exists())

    def test_does_not_replace_existing_export(self) -> None:
        root = self.run_root()
        output = self.base / "export"
        output.mkdir()
        with self.assertRaisesRegex(ValueError, "must be new"):
            archive_evidence.archive(root, output)


if __name__ == "__main__":
    unittest.main()
