"""Post-hoc method coverage checks without Maven or model calls."""

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "experiments" / "harness"))
import reanalyze_coverage as coverage  # noqa: E402


XML = (b'<?xml version="1.0"?><!DOCTYPE report PUBLIC '
       b'"-//JACOCO//DTD Report 1.1//EN" "report.dtd">'
       b'<report><package name="com/redhat/lightwell/service">'
       b'<class name="com/redhat/lightwell/service/StatementImportService">'
       b'<method name="parseAndCreateTransactions" desc="()V">'
       b'<counter type="LINE" missed="2" covered="3"/>'
       b'<counter type="BRANCH" missed="1" covered="4"/>'
       b'</method></class></package></report>')


class ReanalyzeCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name) / "archive"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def archive(self, candidate_xml: bytes = XML) -> pathlib.Path:
        summary = {"evidence_scan": "passed", "run_id": "trial-1",
                   "candidate_results": {"candidate-a": "discriminates"}}
        summary_bytes = json.dumps(summary).encode()
        (self.root / "summary.json").write_bytes(summary_bytes)
        files = {"summary.json": {"sha256": coverage.sha256(summary_bytes),
                                  "bytes": len(summary_bytes)}}
        for case in coverage.CASES.values():
            path = pathlib.Path("candidate-a", "evaluation", case, "target",
                                "site", "jacoco", "jacoco.xml")
            target = self.root / path
            target.parent.mkdir(parents=True)
            content = XML if case == "coverage-control" else candidate_xml
            target.write_bytes(content)
            files[path.as_posix()] = {"sha256": coverage.sha256(content),
                                      "bytes": len(content)}
        manifest = {"schema": "flpath-5050.evidence-archive.v1", "files": files}
        (self.root / "archive-manifest.json").write_text(json.dumps(manifest))
        return self.root

    def test_reads_hash_verified_method_counters(self) -> None:
        report = coverage.derive(self.archive())
        result = report["candidates"]["candidate-a"]
        self.assertEqual("measured", result["status"])
        self.assertEqual({"missed": 2, "covered": 3},
                         result["cases"]["existing_suite"]["line"])
        self.assertEqual({"missed": 1, "covered": 4},
                         result["cases"]["existing_plus_candidate"]["branch"])
        self.assertEqual("()V", result["cases"]["existing_suite"]["descriptor"])

    def test_hash_mismatch_is_unknown(self) -> None:
        root = self.archive()
        path = root / "candidate-a/evaluation/coverage-candidate/target/site/jacoco/jacoco.xml"
        path.write_bytes(XML + b" ")
        result = coverage.derive(root)["candidates"]["candidate-a"]
        self.assertEqual("unknown", result["status"])
        self.assertEqual("archive_hash_mismatch",
                         result["cases"]["existing_plus_candidate"]["reason"])

    def test_missing_and_linked_xml_are_unknown(self) -> None:
        root = self.archive()
        path = root / "candidate-a/evaluation/coverage-candidate/target/site/jacoco/jacoco.xml"
        path.unlink()
        path.symlink_to(root / "summary.json")
        result = coverage.derive(root)["candidates"]["candidate-a"]
        self.assertEqual("xml_missing_linked_or_oversized",
                         result["cases"]["existing_plus_candidate"]["reason"])

    def test_missing_method_is_unknown_not_zero(self) -> None:
        root = self.archive(candidate_xml=XML.replace(
            b'parseAndCreateTransactions', b'otherMethod'))
        result = coverage.derive(root)["candidates"]["candidate-a"]
        case = result["cases"]["existing_plus_candidate"]
        self.assertEqual("unknown", case["status"])
        self.assertNotIn("line", case)
        self.assertEqual("method_missing_or_ambiguous", case["reason"])

    def test_rejects_unapproved_doctype(self) -> None:
        xml = XML.replace(b'"report.dtd"', b'"remote.dtd"')
        root = self.archive(candidate_xml=xml)
        case = coverage.derive(root)["candidates"]["candidate-a"]["cases"]["existing_plus_candidate"]
        self.assertEqual("unsupported_xml_declaration", case["reason"])

    def test_requires_scanned_summary(self) -> None:
        root = self.archive()
        summary = json.loads((root / "summary.json").read_text())
        summary["evidence_scan"] = "blocked"
        (root / "summary.json").write_text(json.dumps(summary))
        with self.assertRaisesRegex(ValueError, "passed evidence scan"):
            coverage.derive(root)


if __name__ == "__main__":
    unittest.main()
