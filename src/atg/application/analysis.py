"""Analyze-stage orchestration over repository context and durable run state."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from typing import Mapping

from atg.domain.repository_analysis import AnalysisFinding, CapabilityObservation, RepositoryContextResult, RepositorySubject
from atg.domain.objective_candidates import derive_objective_candidates
from atg.domain.run_state import RunEvent, validate_identifier, validate_timestamp
from atg.gates.analysis import evaluate_analysis
from atg.ports.analysis import AnalysisStore
from atg.ports.analyzer import RawAnalysisArtifact, RepositoryAnalyzerPort
from atg.ports.repository import RepositoryContextPort, RepositoryContextRequest


class AnalysisConflictError(ValueError):
    """Raised when an analysis identity is reused for different inputs."""


@dataclass(frozen=True)
class AnalysisRequest:
    """One idempotent request to analyze an immutable repository subject."""

    analysis_id: str
    request_id: str
    run_id: str
    repository_root: str
    subject: RepositorySubject
    expected_revision: int
    occurred_at: str
    objective_refs: tuple[str, ...] = ()
    supplied_evidence: tuple[Mapping[str, object], ...] = ()
    supplied_objectives: tuple[Mapping[str, object], ...] = ()
    requested_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate stable identities, revision, timestamp, and local root."""
        for field in ("analysis_id", "request_id", "run_id"):
            validate_identifier(getattr(self, field), field)
        for objective_ref in self.objective_refs:
            validate_identifier(objective_ref, "objective_ref")
        for capability in self.requested_capabilities:
            validate_identifier(capability, "requested_capability")
        if len(set(self.objective_refs)) != len(self.objective_refs):
            raise ValueError("objective_refs must be unique")
        if len(set(self.requested_capabilities)) != len(self.requested_capabilities):
            raise ValueError("requested_capabilities must be unique")
        for document, field, identifier in (
            *((document, "supplied_evidence", "evidence_id") for document in self.supplied_evidence),
            *((document, "supplied_objectives", "objective_id") for document in self.supplied_objectives),
        ):
            if not isinstance(document, Mapping) or not isinstance(document.get(identifier), str):
                raise ValueError(f"{field} documents require {identifier}")
        for documents, identifier, field in (
            (self.supplied_evidence, "evidence_id", "supplied_evidence"),
            (self.supplied_objectives, "objective_id", "supplied_objectives"),
        ):
            identifiers = [document[identifier] for document in documents]
            if len(set(identifiers)) != len(identifiers):
                raise ValueError(f"{field} identities must be unique")
        if not isinstance(self.expected_revision, int) or isinstance(self.expected_revision, bool) or self.expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        validate_timestamp(self.occurred_at, "occurred_at")
        if not self.repository_root:
            raise ValueError("repository_root is required")

    def digest(self) -> str:
        """Return an idempotency digest excluding the execution-local path."""
        payload = {
            "analysis_id": self.analysis_id,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "subject": asdict(self.subject),
            "expected_revision": self.expected_revision,
            "occurred_at": self.occurred_at,
            "objective_refs": sorted(self.objective_refs),
            "supplied_evidence": _sorted_documents(self.supplied_evidence, "evidence_id"),
            "supplied_objectives": _sorted_documents(self.supplied_objectives, "objective_id"),
            "requested_capabilities": sorted(self.requested_capabilities),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class AnalysisRecord:
    """Durable normalized analysis bundle and bound review checkpoint."""

    request_digest: str
    manifest: Mapping[str, object]
    checkpoint: Mapping[str, object] | None
    evidence: tuple[Mapping[str, object], ...]
    objectives: tuple[Mapping[str, object], ...]
    provenance: Mapping[str, object]
    artifacts: tuple[Mapping[str, object], ...]
    duplicate: bool = False

    def to_storage_document(self) -> dict[str, object]:
        """Serialize the complete bundle for reference persistence."""
        return {
            "request_digest": self.request_digest,
            "manifest": dict(self.manifest),
            "checkpoint": dict(self.checkpoint) if self.checkpoint is not None else None,
            "evidence": [dict(document) for document in self.evidence],
            "objectives": [dict(document) for document in self.objectives],
            "provenance": dict(self.provenance),
            "artifacts": [dict(document) for document in self.artifacts],
        }

    @classmethod
    def from_storage_document(cls, document: Mapping[str, object], *, duplicate: bool = False) -> "AnalysisRecord":
        """Reconstruct a bundle loaded from durable reference persistence."""
        return cls(
            request_digest=str(document["request_digest"]),
            manifest=dict(document["manifest"]),  # type: ignore[arg-type]
            checkpoint=dict(document["checkpoint"]) if document.get("checkpoint") is not None else None,  # type: ignore[arg-type]
            evidence=tuple(dict(item) for item in document.get("evidence", [])),  # type: ignore[arg-type]
            objectives=tuple(dict(item) for item in document.get("objectives", [])),  # type: ignore[arg-type]
            provenance=dict(document["provenance"]),  # type: ignore[arg-type]
            artifacts=tuple(dict(item) for item in document.get("artifacts", [])),  # type: ignore[arg-type]
            duplicate=duplicate,
        )


class AnalysisService:
    """Collect repository context and durably create its review checkpoint."""

    def __init__(
        self,
        store: AnalysisStore,
        repository_context: RepositoryContextPort,
        analyzers: tuple[RepositoryAnalyzerPort, ...] = (),
    ) -> None:
        """Create the service from stable persistence and discovery ports."""
        self._store = store
        self._repository_context = repository_context
        self._analyzers = analyzers

    def analyze(self, request: AnalysisRequest) -> AnalysisRecord:
        """Run or replay one idempotent Analyze-stage request."""
        digest = request.digest()
        existing = self._store.get_analysis(request.analysis_id)
        if existing is not None:
            if existing.request_digest != digest:
                raise AnalysisConflictError("analysis identity is bound to different inputs")
            return replace(existing, duplicate=True)

        admission = self._store.get_admission_for_run(request.run_id)
        if admission is None or admission.request_id != request.request_id:
            raise AnalysisConflictError("analysis request does not match the admitted run")
        admitted_repository = admission.identities.get("repository")
        if admitted_repository is not None and (
            admitted_repository.get("id") != request.subject.repository_id
            or admitted_repository.get("version") != request.subject.revision
        ):
            raise AnalysisConflictError("analysis repository does not match the admitted request")

        start_event = RunEvent(
            event_id=f"{request.analysis_id}-started",
            run_id=request.run_id,
            event_type="analysis_started",
            expected_revision=request.expected_revision,
            occurred_at=request.occurred_at,
        )
        self._store.apply(start_event)
        context = self._repository_context.collect(RepositoryContextRequest(request.subject, request.repository_root))
        analyzer_evidence: list[Mapping[str, object]] = []
        analyzer_artifacts: list[RawAnalysisArtifact] = []
        for index, analyzer in enumerate(self._analyzers, 1):
            try:
                result = analyzer.collect(request.subject)
            except Exception as exc:
                context = replace(
                    context,
                    findings=(*context.findings, AnalysisFinding(
                        f"finding-analyzer-{index}", "incomplete", f"Analyzer replay failed: {exc}"
                    )),
                    capabilities=(*context.capabilities, CapabilityObservation(f"analyzer-{index}", "unavailable")),
                )
                continue
            context = replace(
                context,
                facts=(*context.facts, *result.facts),
                findings=(*context.findings, *result.findings),
                capabilities=(*context.capabilities, *result.capabilities),
            )
            analyzer_evidence.extend(result.evidence)
            analyzer_artifacts.extend(result.artifacts)
        context = _require_capabilities(context, request.requested_capabilities)
        record = _build_record(request, digest, context, tuple(analyzer_evidence), tuple(analyzer_artifacts))
        if record.checkpoint is not None:
            outcome_event = RunEvent(
                event_id=f"{request.analysis_id}-completed",
                run_id=request.run_id,
                event_type="analysis_completed",
                expected_revision=request.expected_revision + 1,
                occurred_at=request.occurred_at,
            )
            checkpoint_event = RunEvent(
                event_id=f"{request.analysis_id}-review-created",
                run_id=request.run_id,
                event_type="checkpoint_created",
                expected_revision=request.expected_revision + 2,
                occurred_at=request.occurred_at,
                checkpoint_id=f"checkpoint-{request.analysis_id}",
                checkpoint_kind="analysis_review",
            )
        else:
            outcome = str(record.manifest["outcome"])
            outcome_event = RunEvent(
                event_id=f"{request.analysis_id}-{outcome}",
                run_id=request.run_id,
                event_type="analysis_blocked" if outcome == "blocked" else "analysis_inconclusive",
                expected_revision=request.expected_revision + 1,
                occurred_at=request.occurred_at,
                reason="; ".join(record.manifest["gaps"]),  # type: ignore[arg-type]
            )
            checkpoint_event = None
        return self._store.record_analysis(record, outcome_event, checkpoint_event)


def _build_record(
    request: AnalysisRequest,
    request_digest: str,
    context: RepositoryContextResult,
    analyzer_evidence: tuple[Mapping[str, object], ...] = (),
    analyzer_artifacts: tuple[RawAnalysisArtifact, ...] = (),
) -> AnalysisRecord:
    """Normalize repository context into an analysis bundle and checkpoint."""
    generated_fact_evidence = {
        fact.fact_id: _fact_evidence(request, fact)
        for fact in context.facts
        if not fact.evidence_refs
    }
    evidence = _unique_documents(
        (*request.supplied_evidence, *analyzer_evidence, *generated_fact_evidence.values()),
        "evidence_id",
        "evidence",
    )
    objectives = _unique_documents(request.supplied_objectives, "objective_id", "objective")
    derivation = derive_objective_candidates(objectives, context)
    context = replace(context, findings=(*context.findings, *derivation.findings))
    evidence_by_fact = {
        fact.fact_id: tuple(fact.evidence_refs) or (str(generated_fact_evidence[fact.fact_id]["evidence_id"]),)
        for fact in context.facts
    }
    profile_artifact = _content_artifact(
        f"artifact-{request.analysis_id}-profile",
        "application/vnd.atg.repository-profile+json",
        asdict(context.profile),
    )
    tests_artifact = _content_artifact(
        f"artifact-{request.analysis_id}-tests",
        "application/vnd.atg.test-inventory+json",
        [asdict(test) for test in context.tests],
    )
    coverage_artifact = _content_artifact(
        f"artifact-{request.analysis_id}-coverage",
        "application/vnd.atg.coverage-observations+json",
        [asdict(observation) for observation in context.coverage],
    )
    replay_artifacts = tuple(
        _content_artifact(artifact.artifact_id, artifact.media_type, artifact.content)
        for artifact in analyzer_artifacts
    )
    analysis_artifact_id = f"artifact-{request.analysis_id}"
    provenance_id = f"provenance-{request.analysis_id}"
    objective_refs = tuple(sorted(set(request.objective_refs) | {str(document["objective_id"]) for document in objectives}))
    manifest: dict[str, object] = {
        "contract": "atg.analysis",
        "version": "1.0",
        "analysis_id": request.analysis_id,
        "run_id": request.run_id,
        "request_id": request.request_id,
        "revision": 1,
        "analyzed_at": request.occurred_at,
        "subject": {
            "kind": "git-repository",
            "id": request.subject.repository_id,
            "version": request.subject.revision,
        },
        "profile_digest": context.profile.scanned_content_digest,
        "outcome": "needs_human_review",
        "evidence_refs": [document["evidence_id"] for document in evidence],
        "objective_refs": list(objective_refs),
        "objective_candidates": [candidate.to_document() for candidate in derivation.candidates],
        "provenance_ref": provenance_id,
        "artifact_refs": [
            analysis_artifact_id,
            profile_artifact["artifact"]["artifact_id"],
            tests_artifact["artifact"]["artifact_id"],
            coverage_artifact["artifact"]["artifact_id"],
            *[artifact["artifact"]["artifact_id"] for artifact in replay_artifacts],
        ],
        "capabilities": [
            {
                "capability": capability.capability,
                "status": capability.status,
                "required": capability.required,
                **({"detail": capability.detail} if capability.detail else {}),
            }
            for capability in sorted(context.capabilities, key=lambda item: item.capability)
        ],
        "fact_summary": [
            {
                "fact_id": fact.fact_id,
                "origin": fact.origin,
                "claim": fact.claim,
                "evidence_refs": list(evidence_by_fact[fact.fact_id]),
            }
            for fact in context.facts
        ],
        "findings": [
            {
                "finding_id": finding.finding_id,
                "kind": finding.kind,
                "description": finding.description,
                **({"resolution": "Review or supply the missing repository context."} if finding.kind in {"missing", "incomplete"} else {}),
            }
            for finding in context.findings
        ],
        "assumptions": ["The supplied immutable revision identifies the intended analysis subject."],
        "gaps": [finding.description for finding in context.findings],
        "limitations": [],
    }
    gate = evaluate_analysis(request.subject, manifest, evidence, context, objectives if objectives else None)
    manifest["outcome"] = gate.outcome
    manifest["findings"] = [
        {
            "finding_id": finding.finding_id,
            "kind": finding.kind,
            "description": finding.description,
            **({"resolution": "Review or supply the missing repository context."} if finding.kind in {"missing", "incomplete"} else {}),
        }
        for finding in gate.findings
    ]
    manifest["gaps"] = [finding.description for finding in gate.findings]
    analysis_artifact = _content_artifact(
        analysis_artifact_id,
        "application/vnd.atg.analysis+json",
        manifest,
    )
    provenance = {
        "contract": "atg.provenance",
        "version": "1.0",
        "provenance_id": provenance_id,
        "run_id": request.run_id,
        "recorded_at": request.occurred_at,
        "identities": [manifest["subject"]],
        "artifacts": [
            profile_artifact["artifact"],
            tests_artifact["artifact"],
            coverage_artifact["artifact"],
            analysis_artifact["artifact"],
            *[artifact["artifact"] for artifact in replay_artifacts],
        ],
        "contract_versions": {"atg.analysis": "1.0", "atg.evidence": "1.0", "atg.objective": "1.0", "atg.provenance": "1.0"},
        "limitations": [],
    }
    checkpoint = {
        "contract": "atg.checkpoint",
        "version": "1.0",
        "checkpoint_id": f"checkpoint-{request.analysis_id}",
        "run_id": request.run_id,
        "kind": "analysis_review",
        "revision": 1,
        "state": "pending",
        "created_at": request.occurred_at,
        "reviewed_artifact": analysis_artifact["artifact"],
    }
    return AnalysisRecord(
        request_digest,
        manifest,
        checkpoint if gate.reviewable else None,
        evidence,
        objectives,
        provenance,
        (profile_artifact, tests_artifact, coverage_artifact, *replay_artifacts, analysis_artifact),
    )


def _fact_evidence(request: AnalysisRequest, fact) -> dict[str, object]:
    """Translate one attributable context fact into canonical evidence."""
    locator = f"{request.subject.repository_id}@{request.subject.revision}"
    if fact.source_path:
        locator += f":{fact.source_path}"
    return {
        "contract": "atg.evidence",
        "version": "1.0",
        "evidence_id": f"evidence-{request.analysis_id}-{fact.fact_id}",
        "category": "repository",
        "subject": {
            "kind": "git-repository",
            "id": request.subject.repository_id,
            "version": request.subject.revision,
        },
        "source": {
            "identity": {"kind": "git", "id": request.subject.repository_id},
            "locator": locator,
            "retrieved_at": request.occurred_at,
        },
        "observed_at": request.occurred_at,
        "claims": [fact.claim],
        "artifacts": [],
        "limitations": [],
    }


def _content_artifact(artifact_id: str, media_type: str, content) -> dict[str, object]:
    """Create internal content plus its canonical artifact metadata."""
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    artifact = {
        "artifact_id": artifact_id,
        "media_type": media_type,
        "digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "size_bytes": len(encoded),
    }
    return {"artifact": artifact, "content": content}


def _sorted_documents(documents: tuple[Mapping[str, object], ...], identifier: str) -> list[dict[str, object]]:
    """Return deterministic shallow copies of supplied canonical documents."""
    return [dict(document) for document in sorted(documents, key=lambda item: str(item.get(identifier, "")))]


def _unique_documents(
    documents: tuple[Mapping[str, object], ...],
    identifier: str,
    label: str,
) -> tuple[Mapping[str, object], ...]:
    """Sort supplied documents and reject conflicting duplicate identities."""
    unique: dict[str, Mapping[str, object]] = {}
    for document in documents:
        document_id = str(document[identifier])
        existing = unique.get(document_id)
        if existing is not None and dict(existing) != dict(document):
            raise AnalysisConflictError(f"{label} identity {document_id!r} has conflicting documents")
        unique[document_id] = document
    return tuple(unique[document_id] for document_id in sorted(unique))


def _require_capabilities(
    context: RepositoryContextResult,
    requested: tuple[str, ...],
) -> RepositoryContextResult:
    """Mark requested capabilities as required and preserve missing ones explicitly."""
    observations = {capability.capability: capability for capability in context.capabilities}
    findings = list(context.findings)
    for capability in requested:
        existing = observations.get(capability)
        if existing is None:
            observations[capability] = CapabilityObservation(
                capability, "unavailable", True, "No configured analyzer reported this capability."
            )
            findings.append(AnalysisFinding(
                f"finding-capability-{capability}",
                "missing",
                f"Required analysis capability {capability} is unavailable.",
            ))
        else:
            observations[capability] = replace(existing, required=True)
    return replace(
        context,
        capabilities=tuple(observations[name] for name in sorted(observations)),
        findings=tuple(findings),
    )
