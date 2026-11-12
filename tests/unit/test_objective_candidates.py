import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.domain.objective_candidates import derive_objective_candidates  # noqa: E402
from atg.domain.repository_analysis import RepositoryContextResult, RepositoryProfile, RepositorySubject, TestInventoryEntry  # noqa: E402


SUBJECT = RepositorySubject("https://git.example/app.git", "abc123")


def context(*, tests=()):
    """Build repository context with an optional existing-test inventory."""
    return RepositoryContextResult(
        RepositoryProfile(SUBJECT, ("java",), ("maven",), ("pom.xml",), (), ("src/main",), ("src/test",), 2, "sha256:" + "a" * 64),
        (),
        tuple(tests),
        (),
        (),
    )


def objective(objective_id, *, statement="Validate the affected behavior", evidence_refs=("evidence-1",), target_refs=("symbol-1",), kind="regression", driver_type="bug_report"):
    """Build a canonical objective input for deterministic derivation."""
    return {
        "objective_id": objective_id,
        "statement": statement,
        "kind": kind,
        "driver_type": driver_type,
        "evidence_refs": list(evidence_refs),
        "target_refs": list(target_refs),
        "assumptions": [],
        "gaps": [],
    }


class ObjectiveCandidateTests(unittest.TestCase):
    def test_attributable_regression_candidate_preserves_evidence(self):
        result = derive_objective_candidates((objective("objective-1"),), context())
        candidate = result.candidates[0]
        self.assertEqual(candidate.disposition, "candidate")
        self.assertEqual(candidate.validation_role, "regression")
        self.assertEqual(candidate.expected_behavior_source, "attributable")
        self.assertEqual(candidate.evidence_refs, ("evidence-1",))
        self.assertEqual(candidate.safety_flags, ("existing_test_gap",))

    def test_exact_statement_and_target_duplicate_is_flagged_conservatively(self):
        result = derive_objective_candidates(
            (
                objective("objective-1", statement="Validate   behavior"),
                objective("objective-2", statement="validate behavior"),
                objective("objective-3", statement="validate behavior", target_refs=("symbol-2",)),
            ),
            context(),
        )
        self.assertEqual(result.candidates[1].disposition, "duplicate_candidate")
        self.assertEqual(result.candidates[1].duplicate_of, "candidate-objective-1")
        self.assertEqual(result.candidates[2].disposition, "candidate")

    def test_missing_evidence_is_not_promoted_to_expected_behavior(self):
        result = derive_objective_candidates((objective("objective-1", evidence_refs=()),), context())
        candidate = result.candidates[0]
        self.assertEqual(candidate.disposition, "needs_evidence")
        self.assertEqual(candidate.expected_behavior_source, "unresolved")
        self.assertEqual([(finding.finding_id, finding.kind) for finding in result.findings], [("finding-objective-evidence-objective-1", "missing")])

    def test_non_normative_driver_is_explicit_characterization(self):
        result = derive_objective_candidates(
            (objective("objective-1", kind="behavior", driver_type="user_objective"),),
            context(tests=(TestInventoryEntry("src/test/AppTest.java", "java", "junit-compatible"),)),
        )
        candidate = result.candidates[0]
        self.assertEqual(candidate.validation_role, "characterization")
        self.assertEqual(candidate.expected_behavior_source, "characterization")
        self.assertEqual(candidate.safety_flags, ("existing_tests_require_mapping",))
        self.assertEqual(candidate.existing_test_refs, ())

    def test_malformed_objective_is_an_incomplete_finding(self):
        result = derive_objective_candidates(({"objective_id": "objective-bad"},), context())
        self.assertEqual(result.candidates, ())
        self.assertEqual([(finding.finding_id, finding.kind) for finding in result.findings], [("finding-objective-1", "incomplete")])

    def test_unsupported_kind_is_not_promoted(self):
        result = derive_objective_candidates((objective("objective-unsupported", kind="unknown"),), context())
        candidate = result.candidates[0]
        self.assertEqual((candidate.disposition, candidate.validation_role), ("unsupported", "gap_analysis"))
        self.assertEqual([(finding.finding_id, finding.kind) for finding in result.findings], [("finding-objective-kind-objective-unsupported", "unsupported")])

    def test_vulnerability_driver_maps_to_exposure(self):
        result = derive_objective_candidates((objective("objective-vulnerability", driver_type="vulnerability"),), context())
        self.assertEqual(result.candidates[0].validation_role, "exposure")

    def test_uncertain_evidence_cannot_make_vulnerability_objective_attributable(self):
        evidence = ({
            "evidence_id": "evidence-1",
            "limitations": [{"code": "java-fact-uncertain", "description": "possible dispatch"}],
        },)
        result = derive_objective_candidates(
            (objective("objective-vulnerability", driver_type="vulnerability"),), context(), evidence
        )
        self.assertEqual(result.candidates[0].expected_behavior_source, "unresolved")
        self.assertEqual(result.candidates[0].disposition, "needs_evidence")

    def test_gapped_candidates_preserve_limitations_and_are_not_duplicates(self):
        first = objective("objective-gap-1", kind="behavior", driver_type="test_gap")
        first["gaps"] = ["Expected behavior needs review."]
        second = objective("objective-gap-2", kind="behavior", driver_type="test_gap")
        second["gaps"] = ["Expected behavior needs review."]
        result = derive_objective_candidates((first, second), context())
        self.assertEqual([candidate.disposition for candidate in result.candidates], ["stated_gap", "stated_gap"])
        self.assertEqual(result.candidates[0].limitations, ("Expected behavior needs review.",))

    def test_needs_evidence_duplicates_remain_needs_evidence(self):
        result = derive_objective_candidates(
            (objective("objective-missing-1", evidence_refs=()), objective("objective-missing-2", evidence_refs=())),
            context(),
        )
        self.assertEqual([candidate.disposition for candidate in result.candidates], ["needs_evidence", "needs_evidence"])
        self.assertEqual([candidate.duplicate_of for candidate in result.candidates], [None, None])


if __name__ == "__main__":
    unittest.main()
