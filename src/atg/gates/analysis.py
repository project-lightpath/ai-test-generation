"""Deterministic gates for repository analysis evidence and progression."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from atg.domain.repository_analysis import AnalysisFinding, RepositoryContextResult, RepositorySubject


@dataclass(frozen=True)
class AnalysisGateResult:
    """Outcome and explicit findings produced by deterministic analysis gates."""

    outcome: str
    reviewable: bool
    findings: tuple[AnalysisFinding, ...]


def evaluate_analysis(
    subject: RepositorySubject,
    manifest: Mapping[str, object],
    evidence: Iterable[Mapping[str, object]],
    context: RepositoryContextResult,
    objectives: Iterable[Mapping[str, object]] | None = None,
) -> AnalysisGateResult:
    """Validate attribution, citations, coverage claims, and progression safety."""
    findings = list(context.findings)
    blocked = any(finding.kind in {"conflicting", "unauthorized"} for finding in findings)
    not_evaluable = any(finding.kind == "stale" for finding in findings)
    not_evaluable = not_evaluable or any(
        capability.required and capability.status == "unavailable"
        for capability in context.capabilities
    )
    evidence_by_id = {
        document.get("evidence_id"): document
        for document in evidence
        if isinstance(document.get("evidence_id"), str)
    }
    manifest_evidence = set(_string_refs(manifest.get("evidence_refs")))

    for evidence_id in sorted(manifest_evidence):
        document = evidence_by_id.get(evidence_id)
        if document is None:
            findings.append(_finding("missing-evidence", "missing", f"Analysis references missing evidence {evidence_id}."))
            blocked = True
            continue
        if not _subject_matches(document.get("subject"), subject):
            findings.append(_finding("evidence-subject", "conflicting", f"Evidence {evidence_id} belongs to a different repository subject or revision."))
            blocked = True

    for fact in manifest.get("fact_summary", []):
        if not isinstance(fact, Mapping):
            findings.append(_finding("invalid-fact", "conflicting", "Analysis fact summary contains an invalid entry."))
            blocked = True
            continue
        fact_id = str(fact.get("fact_id", "unknown"))
        references = set(_string_refs(fact.get("evidence_refs")))
        if not references or not references.issubset(manifest_evidence) or not references.issubset(evidence_by_id):
            findings.append(_finding(f"fact-citation-{fact_id}", "missing", f"Fact {fact_id} lacks attributable evidence in this analysis."))
            blocked = True

    for observation in context.coverage:
        if observation.kind != "runtime":
            continue
        evidence_id = observation.evidence_ref
        document = evidence_by_id.get(evidence_id) if evidence_id else None
        if evidence_id not in manifest_evidence or document is None or document.get("category") != "execution":
            findings.append(_finding(
                f"runtime-coverage-{len(findings) + 1}",
                "missing",
                f"Runtime coverage observation {observation.path} lacks execution evidence.",
            ))
            not_evaluable = True

    if objectives is not None:
        objectives_by_id = {
            document.get("objective_id"): document
            for document in objectives
            if isinstance(document.get("objective_id"), str)
        }
        for objective_id in _string_refs(manifest.get("objective_refs")):
            objective = objectives_by_id.get(objective_id)
            if objective is None:
                findings.append(_finding(f"objective-{objective_id}", "missing", f"Analysis references missing objective {objective_id}."))
                blocked = True
                continue
            references = set(_string_refs(objective.get("evidence_refs")))
            if not references or not references.issubset(manifest_evidence) or not references.issubset(evidence_by_id):
                findings.append(_finding(f"objective-citation-{objective_id}", "missing", f"Objective {objective_id} lacks attributable expected-behavior evidence."))
                blocked = True

    if not manifest_evidence or not manifest.get("fact_summary"):
        findings.append(_finding("analysis-evidence-empty", "missing", "Analysis produced no attributable repository facts."))
        not_evaluable = True

    if blocked:
        return AnalysisGateResult("blocked", False, tuple(_deduplicate(findings)))
    if not_evaluable:
        return AnalysisGateResult("not_evaluable", False, tuple(_deduplicate(findings)))
    return AnalysisGateResult("needs_human_review", True, tuple(_deduplicate(findings)))


def _subject_matches(value: object, subject: RepositorySubject) -> bool:
    """Return whether a canonical identity matches the analyzed repository."""
    return (
        isinstance(value, Mapping)
        and value.get("id") == subject.repository_id
        and value.get("version") == subject.revision
    )


def _string_refs(value: object) -> tuple[str, ...]:
    """Return only string references from an untrusted collection value."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _finding(finding_id: str, kind: str, description: str) -> AnalysisFinding:
    """Create a deterministic gate finding."""
    return AnalysisFinding(f"gate-{finding_id}", kind, description)


def _deduplicate(findings: Iterable[AnalysisFinding]) -> list[AnalysisFinding]:
    """Preserve the first finding for each stable identity."""
    unique: dict[str, AnalysisFinding] = {}
    for finding in findings:
        unique.setdefault(finding.finding_id, finding)
    return list(unique.values())
