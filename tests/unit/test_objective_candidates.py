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
        self.assertTrue(any(finding.kind == "missing" for finding in result.findings))

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


if __name__ == "__main__":
    unittest.main()
