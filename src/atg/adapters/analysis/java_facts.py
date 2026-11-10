"""Replay translator for Java SemanticCallFacts analyzer output."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Mapping

from atg.domain.repository_analysis import AnalysisFinding, CapabilityObservation, ContextFact, RepositorySubject
from atg.ports.analyzer import AnalyzerEvidenceResult, RawAnalysisArtifact


class JavaSemanticFactsReplayAdapter:
    """Translate captured Java call/framework facts into canonical evidence."""

    capabilities = ("java-semantic-calls", "java-framework-facts")
    producer_identity = {"kind": "analysis-adapter", "id": "java-semantic-facts-replay", "version": "1"}

    def __init__(self, document: Mapping[str, object]) -> None:
        """Capture one immutable SemanticCallFacts replay document."""
        self._document = dict(document)

    def collect(self, subject: RepositorySubject) -> AnalyzerEvidenceResult:
        """Normalize facts while preserving certainty, abstention, and limitations."""
        document = self._document
        raw_digest = _digest(document)
        suffix = raw_digest.removeprefix("sha256:")[:16]
        metadata = document.get("runMetadata") if isinstance(document.get("runMetadata"), Mapping) else {}
        evidence_subject = RepositorySubject(
            str(metadata.get("sourceRepository") or subject.repository_id),
            str(metadata.get("sourceRevision") or subject.revision),
            str(metadata.get("sourceFingerprint")) if metadata.get("sourceFingerprint") else None,
        )
        findings: list[AnalysisFinding] = []
        if document.get("schemaVersion") != "0.1.0" or document.get("artifact") != "SemanticCallFacts":
            artifact = RawAnalysisArtifact(
                f"artifact-java-semantic-facts-{suffix}",
                "application/vnd.project-lightpath.java-semantic-call-facts+json",
                document,
            )
            return AnalyzerEvidenceResult(
                (),
                (),
                (
                    CapabilityObservation("java-semantic-calls", "unavailable"),
                    CapabilityObservation("java-framework-facts", "unavailable"),
                    CapabilityObservation("runtime-coverage", "unavailable"),
                ),
                (AnalysisFinding("finding-java-version", "unsupported", "Java semantic facts contract is unsupported."),),
                (artifact,),
            )
        if metadata.get("sourceRepository") != subject.repository_id or metadata.get("sourceRevision") != subject.revision:
            findings.append(AnalysisFinding("finding-java-subject", "conflicting", "Java semantic facts belong to a different repository subject or revision."))

        evidence_documents: list[Mapping[str, object]] = []
        facts: list[ContextFact] = []
        seen_fact_ids: set[str] = set()
        provider_facts = document.get("providerFacts") if isinstance(document.get("providerFacts"), list) else []
        framework_facts = document.get("frameworkFacts") if isinstance(document.get("frameworkFacts"), list) else []
        for fact_set, values in (("provider", provider_facts), ("framework", framework_facts)):
            for index, value in enumerate(values, 1):
                if not isinstance(value, Mapping):
                    findings.append(AnalysisFinding(f"finding-java-{fact_set}-{index}", "incomplete", "Java analyzer emitted a non-object fact."))
                    continue
                fact_digest = _digest(value).removeprefix("sha256:")[:16]
                fact_id = f"fact-java-{fact_set}-{fact_digest}"
                if fact_id in seen_fact_ids:
                    findings.append(AnalysisFinding(
                        f"finding-java-duplicate-{fact_digest}",
                        "conflicting",
                        f"Java analyzer emitted duplicate {fact_set} fact {fact_id}.",
                    ))
                    continue
                seen_fact_ids.add(fact_id)
                evidence_id = f"evidence-java-{fact_set}-{fact_digest}"
                certainty = str(value.get("certainty", "unresolved"))
                status = str(value.get("status", "emitted"))
                claim = str(value.get("claim") or _relationship_claim(value, certainty, status))
                source_path, line = _source_location(value)
                prepared_at = metadata.get("preparedAt")
                if not isinstance(prepared_at, str) or not prepared_at:
                    findings.append(AnalysisFinding(
                        f"finding-java-prepared-at-{fact_digest}",
                        "incomplete",
                        "Java semantic fact has no valid preparation timestamp.",
                        source_path,
                    ))
                    continue
                evidence_documents.append(_evidence_document(
                    evidence_id, evidence_subject, document, value, source_path, line, claim, raw_digest
                ))
                facts.append(ContextFact(
                    fact_id,
                    "supplied",
                    claim,
                    source_path,
                    (evidence_id,),
                    certainty,
                    status,
                ))
                if certainty in {"possible", "unresolved"} or status == "abstained":
                    findings.append(AnalysisFinding(
                        f"finding-java-certainty-{fact_digest}",
                        "ambiguous",
                        f"Java analyzer fact remains {certainty} with status {status}.",
                        source_path,
                    ))

        capabilities = (
            CapabilityObservation("java-semantic-calls", "available"),
            CapabilityObservation("java-framework-facts", "available" if framework_facts else "partial"),
            CapabilityObservation("runtime-coverage", "unavailable", detail="Java semantic facts do not prove execution coverage."),
        )
        for index, limitation in enumerate(document.get("limitations", []) if isinstance(document.get("limitations"), list) else [], 1):
            if isinstance(limitation, str) and limitation:
                findings.append(AnalysisFinding(f"finding-java-limitation-{index}", "incomplete", limitation))
        artifact = RawAnalysisArtifact(
            f"artifact-java-semantic-facts-{suffix}",
            "application/vnd.project-lightpath.java-semantic-call-facts+json",
            document,
        )
        return AnalyzerEvidenceResult(tuple(facts), tuple(evidence_documents), capabilities, tuple(findings), (artifact,))


def _evidence_document(evidence_id, subject, document, fact, source_path, line, claim, raw_digest):
    """Create canonical evidence for one Java semantic fact."""
    context_id = str(document.get("contextId", "unknown"))
    metadata = document.get("runMetadata") if isinstance(document.get("runMetadata"), Mapping) else {}
    locator = f"{subject.repository_id}@{subject.revision}"
    if source_path:
        locator += f":{source_path}"
    if line:
        locator += f"#L{line}"
    return {
        "contract": "atg.evidence",
        "version": "1.0",
        "evidence_id": evidence_id,
        "category": "repository",
        "subject": {
            "kind": "git-repository",
            "id": subject.repository_id,
            "version": subject.revision,
            **({"digest": subject.content_digest} if subject.content_digest else {}),
        },
        "source": {
            "identity": {"kind": "java-analysis-adapter", "id": context_id, "version": str(document.get("schemaVersion", "unknown"))},
            "locator": locator,
            "content_digest": raw_digest,
        },
        "observed_at": metadata["preparedAt"],
        "claims": [claim],
        "artifacts": [],
        "limitations": [
            *([{"code": "java-fact-limitation", "description": str(fact["limitation"])}] if fact.get("limitation") else []),
            *([{"code": "java-fact-uncertain", "description": f"certainty={fact.get('certainty', 'unresolved')}; status={fact.get('status', 'emitted')}"}] if fact.get("certainty") in {"possible", "unresolved"} or fact.get("status") == "abstained" else []),
        ],
    }


def _source_location(fact: Mapping[str, object]) -> tuple[str | None, int | None]:
    """Extract a safe repository-relative source path and line when present."""
    evidence = fact.get("evidence") if isinstance(fact.get("evidence"), list) else []
    first = next((item for item in evidence if isinstance(item, Mapping)), {})
    path = first.get("path")
    source_path = None
    if isinstance(path, str) and path:
        candidate = PurePosixPath(path)
        if not candidate.is_absolute() and ".." not in candidate.parts:
            source_path = path
    line = first.get("line")
    return source_path, line if isinstance(line, int) and line > 0 else None


def _relationship_claim(fact: Mapping[str, object], certainty: str, status: str) -> str:
    """Build a bounded relationship statement without strengthening certainty."""
    caller = str(fact.get("caller", "unknown caller"))
    callee = str(fact.get("callee", "unknown callee"))
    relationship = str(fact.get("relationshipType", "relationship"))
    return f"Java analyzer {status} {certainty} {relationship} from {caller} to {callee}."


def _digest(document: Mapping[str, object]) -> str:
    """Return a deterministic digest for a replay document or fact."""
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
