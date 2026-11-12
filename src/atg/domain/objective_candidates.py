"""Deterministic, evidence-bounded objective candidate derivation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from atg.domain.repository_analysis import AnalysisFinding, RepositoryContextResult


CANDIDATE_DISPOSITIONS = frozenset({"candidate", "duplicate_candidate", "needs_evidence", "unsupported", "stated_gap"})
VALIDATION_ROLES = frozenset({"exposure", "fix_pinning", "regression", "characterization", "compatibility", "gap_analysis"})
EXPECTED_BEHAVIOR_SOURCES = frozenset({"attributable", "characterization", "unresolved"})


@dataclass(frozen=True)
class ObjectiveCandidate:
    """Review metadata for one canonical objective document."""

    candidate_id: str
    objective_ref: str
    statement: str
    disposition: str
    validation_role: str
    expected_behavior_source: str
    evidence_refs: tuple[str, ...]
    target_refs: tuple[str, ...]
    existing_test_refs: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    safety_flags: tuple[str, ...] = ()
    duplicate_of: str | None = None

    def __post_init__(self) -> None:
        """Validate candidate identity and controlled classifications."""
        if not self.candidate_id or not self.objective_ref or not self.statement:
            raise ValueError("objective candidate requires identities and a statement")
        if self.disposition not in CANDIDATE_DISPOSITIONS:
            raise ValueError(f"unsupported candidate disposition: {self.disposition}")
        if self.validation_role not in VALIDATION_ROLES:
            raise ValueError(f"unsupported validation role: {self.validation_role}")
        if self.expected_behavior_source not in EXPECTED_BEHAVIOR_SOURCES:
            raise ValueError(f"unsupported expected behavior source: {self.expected_behavior_source}")
        if self.disposition == "duplicate_candidate" and not self.duplicate_of:
            raise ValueError("duplicate candidate requires duplicate_of")

    def to_document(self) -> dict[str, object]:
        """Serialize candidate metadata for the analysis manifest."""
        document: dict[str, object] = {
            "candidate_id": self.candidate_id,
            "objective_ref": self.objective_ref,
            "statement": self.statement,
            "disposition": self.disposition,
            "validation_role": self.validation_role,
            "expected_behavior_source": self.expected_behavior_source,
            "evidence_refs": list(self.evidence_refs),
            "target_refs": list(self.target_refs),
            "existing_test_refs": list(self.existing_test_refs),
            "assumptions": list(self.assumptions),
            "limitations": list(self.limitations),
            "safety_flags": list(self.safety_flags),
        }
        if self.duplicate_of:
            document["duplicate_of"] = self.duplicate_of
        return document


@dataclass(frozen=True)
class ObjectiveDerivationResult:
    """Deterministic candidates and explicit derivation findings."""

    candidates: tuple[ObjectiveCandidate, ...]
    findings: tuple[AnalysisFinding, ...]


def derive_objective_candidates(
    objectives: Iterable[Mapping[str, object]],
    context: RepositoryContextResult,
    evidence: Iterable[Mapping[str, object]] = (),
) -> ObjectiveDerivationResult:
    """Classify supplied objectives without inventing expected behavior."""
    candidates: list[ObjectiveCandidate] = []
    findings: list[AnalysisFinding] = []
    seen: dict[tuple[str, tuple[str, ...]], str] = {}
    uncertain_evidence = {
        str(document.get("evidence_id"))
        for document in evidence
        if isinstance(document, Mapping)
        and any(
            isinstance(limitation, Mapping) and limitation.get("code") == "java-fact-uncertain"
            for limitation in document.get("limitations", [])
            if isinstance(document.get("limitations"), list)
        )
    }

    for document in objectives:
        objective_id = document.get("objective_id")
        statement = document.get("statement")
        if not isinstance(objective_id, str) or not objective_id or not isinstance(statement, str) or not statement.strip():
            findings.append(AnalysisFinding(
                f"finding-objective-{len(findings) + 1}",
                "incomplete",
                "Supplied objective is missing a stable identity or behavior statement.",
            ))
            continue

        evidence_refs = _string_tuple(document.get("evidence_refs"))
        target_refs = tuple(sorted(_string_tuple(document.get("target_refs"))))
        key = (_normalized_statement(statement), target_refs)
        duplicate_of = seen.get(key)
        role = _validation_role(document.get("kind"), document.get("driver_type"))
        behavior_source = _behavior_source(document.get("driver_type"), evidence_refs, uncertain_evidence)
        disposition = "candidate"
        if not evidence_refs or behavior_source == "unresolved":
            disposition = "needs_evidence"
            findings.append(AnalysisFinding(
                f"finding-objective-evidence-{objective_id}",
                "missing",
                f"Objective {objective_id} lacks attributable expected-behavior evidence.",
            ))
        elif role is None:
            role = "gap_analysis"
            disposition = "unsupported"
            findings.append(AnalysisFinding(
                f"finding-objective-kind-{objective_id}",
                "unsupported",
                f"Objective {objective_id} has an unsupported objective kind.",
            ))
        elif duplicate_of is not None:
            disposition = "duplicate_candidate"
        elif document.get("gaps") and document.get("driver_type") == "test_gap":
            disposition = "stated_gap"
        else:
            seen[key] = f"candidate-{objective_id}"

        safety_flags = (
            ("existing_tests_require_mapping",)
            if context.tests
            else ("existing_test_gap",)
        )
        candidates.append(ObjectiveCandidate(
            candidate_id=f"candidate-{objective_id}",
            objective_ref=objective_id,
            statement=statement.strip(),
            disposition=disposition,
            validation_role=role or "gap_analysis",
            expected_behavior_source=behavior_source,
            evidence_refs=evidence_refs,
            target_refs=target_refs,
            assumptions=_string_tuple(document.get("assumptions")),
            limitations=_string_tuple(document.get("gaps")),
            safety_flags=safety_flags,
            duplicate_of=duplicate_of,
        ))

    return ObjectiveDerivationResult(tuple(candidates), tuple(findings))


def _normalized_statement(statement: str) -> str:
    """Normalize only whitespace and case for conservative duplicate checks."""
    return " ".join(statement.casefold().split())


def _string_tuple(value: object) -> tuple[str, ...]:
    """Return string members from an untrusted sequence without coercion."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _validation_role(kind: object, driver_type: object) -> str | None:
    """Map canonical objective classifications to a neutral validation role."""
    if driver_type == "vulnerability":
        return "exposure"
    return {
        "regression": "regression",
        "behavior": "characterization",
        "security": "exposure",
        "compatibility": "compatibility",
        "coverage": "gap_analysis",
    }.get(kind)  # type: ignore[arg-type]


def _behavior_source(
    driver_type: object,
    evidence_refs: tuple[str, ...],
    uncertain_evidence: set[str] | None = None,
) -> str:
    """Classify whether expected behavior is attributable or characterization."""
    if not evidence_refs:
        return "unresolved"
    if uncertain_evidence and set(evidence_refs).issubset(uncertain_evidence):
        return "unresolved"
    if driver_type in {"vulnerability", "bug_report", "contract_change", "dependency_change"}:
        return "attributable"
    return "characterization"
