"""Replay checks for the Spring XML deposit fixture and method coverage."""

import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "experiments" / "harness"))

import candidate_evaluation as evaluation  # noqa: E402
from fixture_profiles import SPRING_TRANSFER  # noqa: E402


class SpringXmlFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_objective_names_uncovered_lowercase_behavior(self) -> None:
        objective = (pathlib.Path(__file__).parents[2] / "experiments" / "harness"
                     / "fixtures/spring-xml-deposit-objective.md").read_text()
        self.assertIn("lower case", objective)
        self.assertIn("1100.00", objective)
        self.assertIn("uppercase-only import tests do not cover it", objective)

    def test_xml_mutation_uses_exact_source_anchor(self) -> None:
        profile = SPRING_TRANSFER._replace(
            name="spring-xml-deposit",
            mutation_path=pathlib.Path(
                "src/main/java/com/redhat/lightwell/service/StatementImportService.java"),
            original_line='"DEPOSIT".equalsIgnoreCase(type)',
            mutated_line='"DEPOSIT".equals(type)')
        source = self.root / profile.mutation_path
        source.parent.mkdir(parents=True)
        source.write_text("A = " + profile.original_line + ";\n")
        evaluation.mutate_fixture(self.root, profile)
        self.assertIn(profile.mutated_line, source.read_text())
        self.assertNotIn(profile.original_line, source.read_text())

    def test_xml_method_coverage_is_measured_separately_from_transfer(self) -> None:
        report = self.root / "target/site/jacoco/jacoco.xml"
        report.parent.mkdir(parents=True)
        report.write_text("""<report><package name="com/redhat/lightwell/service">
            <class name="com/redhat/lightwell/service/StatementImportService">
            <method name="importStatement" desc="(Ljava/lang/Long;Ljava/lang/String;)V">
            <counter type="LINE" missed="0" covered="8"/>
            </method>
            <method name="parseAndCreateTransactions" desc="(LAccount;Ljava/lang/String;)V">
            <counter type="LINE" missed="3" covered="10"/>
            <counter type="BRANCH" missed="1" covered="2"/>
            </method></class></package></report>""")
        target = evaluation.SPRING_COVERAGE_TARGETS["spring-xml-deposit"]
        self.assertEqual(target[1], "parseAndCreateTransactions")
        measured = evaluation.spring_method_coverage(self.root, *target)
        self.assertEqual(measured["status"], "measured")
        self.assertEqual(measured["line"], {"missed": 3, "covered": 10})
        self.assertEqual(measured["branch"], {"missed": 1, "covered": 2})
        import_result = evaluation.spring_method_coverage(
            self.root, target[0], "importStatement")
        self.assertEqual(import_result["reason"], "target_counters_missing")
        self.assertEqual(evaluation.spring_transfer_coverage(self.root)["status"],
                         "coverage_unknown")

    def test_xml_profile_runs_coverage_comparison(self) -> None:
        fixture = self.root / "fixture"
        fixture.mkdir()
        (fixture / "pom.xml").write_text("pinned fixture")
        candidate = self.root / "SpringXmlLowercaseDepositReferenceTest.java"
        candidate.write_text("class SpringXmlLowercaseDepositReferenceTest {}")
        target = pathlib.Path("src/test/java/SpringXmlLowercaseDepositReferenceTest.java")
        profile = SPRING_TRANSFER._replace(name="spring-xml-deposit")
        with patch.object(evaluation, "_run_maven_phase",
                          return_value={"status": "failed"}) as runner:
            result = evaluation.spring_coverage_comparison(
                fixture, candidate, target, self.root / "output",
                self.root / "cache", "pinned-image", profile)
        self.assertEqual(result["reason"], "coverage_build_failed")
        self.assertEqual(runner.call_count, 2)
        self.assertEqual((self.root / "output/coverage-candidate" / target).read_text(),
                         candidate.read_text())


if __name__ == "__main__":
    unittest.main()
