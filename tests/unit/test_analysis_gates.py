import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.domain.repository_analysis import (  # noqa: E402
    AnalysisFinding,
    CapabilityObservation,
    ContextFact,
    CoverageObservation,
    RepositoryContextResult,
    RepositoryProfile,
    RepositorySubject,
)
from atg.gates.analysis import evaluate_analysis  # noqa: E402


SUBJECT = RepositorySubject("https://git.example/app.git", "abc123")


def context(*, facts=(), coverage=(), findings=()):
    """Build a normalized repository context for deterministic gate tests."""
    return RepositoryContextResult(
        RepositoryProfile(SUBJECT, ("java",), ("maven",), ("pom.xml",), (), ("src/main",), ("src/test",), 1, "sha256:" + "a" * 64),
        tuple(facts),
        (),
        tuple(coverage),
        tuple(findings),
    )


def evidence(evidence_id="evidence-1", *, revision="abc123", category="repository"):
    """Build one attributable evidence document."""
    return {
        "evidence_id": evidence_id,
        "category": category,
        "subject": {"kind": "git-repository", "id": SUBJECT.repository_id, "version": revision},
    }


def manifest(*, evidence_refs=("evidence-1",), fact_refs=("evidence-1",), objective_refs=()):
    """Build the analysis fields consumed by deterministic gates."""
    return {
        "evidence_refs": list(evidence_refs),
        "objective_refs": list(objective_refs),
        "fact_summary": [{"fact_id": "fact-1", "evidence_refs": list(fact_refs)}] if fact_refs is not None else [],
    }


class AnalysisGateTests(unittest.TestCase):
    def test_attributable_repository_fact_is_reviewable(self):
        result = evaluate_analysis(
            SUBJECT,
            manifest(),
            (evidence(),),
            context(facts=(ContextFact("fact-1", "discovered", "A manifest exists", "pom.xml"),)),
        )
        self.assertTrue(result.reviewable)
        self.assertEqual(result.outcome, "needs_human_review")
        self.assertEqual(result.findings, ())

    def test_subject_revision_mismatch_blocks_analysis(self):
        result = evaluate_analysis(
            SUBJECT,
            manifest(),
            (evidence(revision="different"),),
            context(facts=(ContextFact("fact-1", "discovered", "A manifest exists", "pom.xml"),)),
        )
        self.assertFalse(result.reviewable)
        self.assertEqual(result.outcome, "blocked")
        self.assertEqual([(finding.finding_id, finding.kind) for finding in result.findings], [("gate-evidence-subject", "conflicting")])

    def test_uncited_fact_and_objective_block_analysis(self):
        objective = {"objective_id": "objective-1", "evidence_refs": []}
        result = evaluate_analysis(
            SUBJECT,
            manifest(fact_refs=(), objective_refs=("objective-1",)),
            (evidence(),),
            context(facts=(ContextFact("fact-1", "discovered", "A manifest exists", "pom.xml"),)),
            (objective,),
        )
        self.assertEqual(result.outcome, "blocked")
        finding_pairs = {(finding.finding_id, finding.kind) for finding in result.findings}
        self.assertIn(("gate-fact-citation-fact-1", "missing"), finding_pairs)
        self.assertIn(("gate-objective-citation-objective-1", "missing"), finding_pairs)

    def test_runtime_coverage_without_execution_evidence_is_not_evaluable(self):
        result = evaluate_analysis(
            SUBJECT,
            manifest(),
            (evidence(),),
            context(
                facts=(ContextFact("fact-1", "discovered", "A manifest exists", "pom.xml"),),
                coverage=(CoverageObservation("coverage.xml", "runtime", "evidence-1"),),
            ),
        )
        self.assertEqual(result.outcome, "not_evaluable")
        self.assertFalse(result.reviewable)
        self.assertIn(("gate-runtime-coverage-1", "missing"), {(finding.finding_id, finding.kind) for finding in result.findings})

    def test_incomplete_discovery_remains_explicit_but_reviewable(self):
        result = evaluate_analysis(
            SUBJECT,
            manifest(),
            (evidence(),),
            context(
                facts=(ContextFact("fact-1", "discovered", "A manifest exists", "pom.xml"),),
                findings=(AnalysisFinding("finding-limit", "incomplete", "File limit reached"),),
            ),
        )
        self.assertEqual(result.outcome, "needs_human_review")
        self.assertTrue(result.reviewable)
        self.assertEqual(result.findings[0].finding_id, "finding-limit")

    def test_evidence_free_analysis_is_not_evaluable(self):
        result = evaluate_analysis(SUBJECT, manifest(evidence_refs=(), fact_refs=None), (), context())
        self.assertEqual(result.outcome, "not_evaluable")
        self.assertFalse(result.reviewable)

    def test_required_unavailable_capability_and_stale_context_are_not_evaluable(self):
        result = evaluate_analysis(
            SUBJECT,
            manifest(),
            (evidence(),),
            replace_context(
                context(facts=(ContextFact("fact-1", "discovered", "A manifest exists", "pom.xml"),)),
                capabilities=(CapabilityObservation("java-semantic-calls", "unavailable", True),),
                findings=(AnalysisFinding("finding-stale", "stale", "The context is stale"),),
            ),
        )
        self.assertEqual(result.outcome, "not_evaluable")
        self.assertFalse(result.reviewable)


def replace_context(value, **changes):
    """Return a repository context with selected gate inputs replaced."""
    from dataclasses import replace
    return replace(value, **changes)


if __name__ == "__main__":
    unittest.main()
