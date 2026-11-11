"""Replay translator for VPE repository preparation context."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping

from atg.domain.repository_analysis import AnalysisFinding, CapabilityObservation, ContextFact, RepositorySubject
from atg.ports.analyzer import AnalyzerEvidenceResult, RawAnalysisArtifact


class VPEAnalysisContextReplayAdapter:
    """Translate a captured VPE AnalysisContext without defining VEX semantics."""

    capabilities = ("repository-preparation", "build-context", "dependency-context", "framework-metadata")
    producer_identity = {"kind": "analysis-adapter", "id": "vpe-analysis-context-replay", "version": "1"}

    def __init__(self, document: Mapping[str, object]) -> None:
        """Capture one immutable replay document."""
        self._document = dict(document)

    def collect(self, subject: RepositorySubject) -> AnalyzerEvidenceResult:
        """Normalize preparation identity, fingerprints, limitations, and gaps."""
        document = self._document
        digest = _digest(document)
        suffix = digest.removeprefix("sha256:")[:16]
        context_id = str(document.get("id", f"context-{suffix}"))
        provenance = document.get("provenance") if isinstance(document.get("provenance"), Mapping) else {}
        repository = provenance.get("sourceRepository")
        revision = provenance.get("sourceRevision")
        evidence_id = f"evidence-vpe-context-{suffix}"
        findings: list[AnalysisFinding] = []
        if document.get("schemaVersion") != "0.1.0":
            artifact = RawAnalysisArtifact(
                f"artifact-vpe-context-{suffix}",
                "application/vnd.project-lightpath.vpe-analysis-context+json",
                document,
            )
            return AnalyzerEvidenceResult(
                (),
                (),
                (
                    CapabilityObservation("repository-preparation", "unavailable"),
                    CapabilityObservation("build-context", "unavailable"),
                    CapabilityObservation("dependency-context", "unavailable"),
                    CapabilityObservation("framework-metadata", "unavailable"),
                ),
                (AnalysisFinding("finding-vpe-version", "unsupported", "VPE analysis context schema version is unsupported."),),
                (artifact,),
            )
        if repository != subject.repository_id or revision != subject.revision:
            findings.append(AnalysisFinding("finding-vpe-subject", "conflicting", "VPE analysis context belongs to a different repository subject or revision."))

        claims = ["VPE prepared immutable repository analysis context."]
        facts = [ContextFact("fact-vpe-context", "supplied", claims[0], evidence_refs=(evidence_id,))]
        framework = document.get("framework")
        if isinstance(framework, str) and framework:
            claim = f"Prepared context identifies framework {framework}."
            claims.append(claim)
            facts.append(ContextFact("fact-vpe-framework", "supplied", claim, evidence_refs=(evidence_id,)))
        scope = document.get("analysisScope")
        if isinstance(scope, str) and scope:
            claim = f"Prepared context declares analysis scope {scope}."
            claims.append(claim)
            facts.append(ContextFact("fact-vpe-scope", "supplied", claim, evidence_refs=(evidence_id,)))

        limitations = document.get("limitations") if isinstance(document.get("limitations"), list) else []
        for index, limitation in enumerate(limitations, 1):
            if isinstance(limitation, str) and limitation:
                findings.append(AnalysisFinding(f"finding-vpe-limitation-{index}", "incomplete", limitation))

        metadata = document.get("frameworkMetadata") if isinstance(document.get("frameworkMetadata"), list) else []
        metadata_statuses = {item.get("status") for item in metadata if isinstance(item, Mapping)}
        invalid_metadata = any(status not in {"observed", "not_captured"} for status in metadata_statuses)
        if invalid_metadata:
            findings.append(AnalysisFinding("finding-vpe-framework-metadata", "unsupported", "VPE framework metadata has an unsupported status."))
        metadata_status = "unavailable" if invalid_metadata or not metadata else "partial" if "not_captured" in metadata_statuses else "available"
        build_evidence = document.get("buildEvidence")
        valid_build = isinstance(build_evidence, Mapping) and isinstance(build_evidence.get("tool"), str) and isinstance(build_evidence.get("fingerprint"), str)
        dependency_fingerprint = provenance.get("dependencyFingerprint")
        valid_dependency = isinstance(dependency_fingerprint, str) and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", dependency_fingerprint))
        capabilities = (
            CapabilityObservation("repository-preparation", "available"),
            CapabilityObservation("build-context", "available" if valid_build else "unavailable"),
            CapabilityObservation("dependency-context", "available" if valid_dependency else "unavailable"),
            CapabilityObservation("framework-metadata", metadata_status),
        )
        evidence_subject = {
            "kind": "git-repository",
            "id": repository if isinstance(repository, str) and repository else subject.repository_id,
            "version": revision if isinstance(revision, str) and revision else subject.revision,
            **({"digest": provenance.get("sourceFingerprint")} if provenance.get("sourceFingerprint") else {}),
        }
        prepared_at = provenance.get("preparedAt")
        artifact = RawAnalysisArtifact(
            f"artifact-vpe-context-{suffix}",
            "application/vnd.project-lightpath.vpe-analysis-context+json",
            document,
        )
        if not isinstance(prepared_at, str) or not prepared_at:
            findings.append(AnalysisFinding(
                "finding-vpe-prepared-at", "incomplete", "VPE context has no valid preparation timestamp."
            ))
            return AnalyzerEvidenceResult(tuple(facts), (), capabilities, tuple(findings), (artifact,))
        evidence = {
            "contract": "atg.evidence",
            "version": "1.0",
            "evidence_id": evidence_id,
            "category": "repository",
            "subject": evidence_subject,
            "source": {
                "identity": {"kind": "vpe-analysis-context", "id": context_id, "version": str(document.get("schemaVersion", "unknown"))},
                "locator": f"urn:atg-replay:vpe-analysis-context:{context_id}",
                "content_digest": digest,
            },
            "observed_at": prepared_at,
            "content_digest": digest,
            "claims": claims,
            "artifacts": [],
            "limitations": [{"code": "vpe-context-limitation", "description": item} for item in limitations if isinstance(item, str) and item],
        }
        return AnalyzerEvidenceResult(tuple(facts), (evidence,), capabilities, tuple(findings), (artifact,))


def _digest(document: Mapping[str, object]) -> str:
    """Return a deterministic digest for a replay document."""
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
