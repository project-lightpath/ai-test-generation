"""Analyze-stage orchestration over repository context and durable run state."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from typing import Mapping

from atg.domain.repository_analysis import AnalysisFinding, CapabilityObservation, RepositoryContextResult, RepositorySubject
from atg.domain.objective_candidates import derive_objective_candidates
from atg.domain.run_state import RunEvent, validate_identifier, validate_timestamp
from atg.domain.schema_validation import ContractValidationError, validate_contract_document
from atg.gates.analysis import evaluate_analysis
from atg.ports.analysis import AnalysisStore
from atg.ports.analyzer import RawAnalysisArtifact, RepositoryAnalyzerPort
from atg.ports.repository import RepositoryContextPort, RepositoryContextRequest


class AnalysisConflictError(ValueError):
    """Raised when an analysis identity is reused for different inputs."""


class AnalysisProcessingError(ValueError):
    """Raised when repository analysis cannot be completed safely."""


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
        for document in self.supplied_evidence:
            validate_contract_document(document, "evidence")
        for document in self.supplied_objectives:
            validate_contract_document(document, "objective")
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
        record = cls(
            request_digest=str(document["request_digest"]),
            manifest=dict(document["manifest"]),  # type: ignore[arg-type]
            checkpoint=dict(document["checkpoint"]) if document.get("checkpoint") is not None else None,  # type: ignore[arg-type]
            evidence=tuple(dict(item) for item in document.get("evidence", [])),  # type: ignore[arg-type]
            objectives=tuple(dict(item) for item in document.get("objectives", [])),  # type: ignore[arg-type]
            provenance=dict(document["provenance"]),  # type: ignore[arg-type]
            artifacts=tuple(dict(item) for item in document.get("artifacts", [])),  # type: ignore[arg-type]
            duplicate=duplicate,
        )
        validate_analysis_record(record)
        return record


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
        if admitted_repository is None:
            raise AnalysisConflictError("admission does not contain a repository identity")
        if (
            admitted_repository.get("id") != request.subject.repository_id
            or admitted_repository.get("version") != request.subject.revision
            or admitted_repository.get("digest") != request.subject.content_digest
        ):
            raise AnalysisConflictError("analysis repository does not match the admitted request")
        if not set(request.requested_capabilities).issubset(admission.admitted_capabilities):
            raise AnalysisConflictError("analysis capabilities exceed the admitted capability set")

        start_event = RunEvent(
            event_id=f"{request.analysis_id}-started",
            run_id=request.run_id,
            event_type="analysis_started",
            expected_revision=request.expected_revision,
            occurred_at=request.occurred_at,
        )
        try:
            context = self._repository_context.collect(RepositoryContextRequest(request.subject, request.repository_root))
        except Exception as exc:
            raise AnalysisProcessingError("repository discovery failed") from exc
        analyzer_evidence: list[Mapping[str, object]] = []
        analyzer_artifacts: list[RawAnalysisArtifact] = []
        producer_identities: list[Mapping[str, object]] = [self._repository_context.producer_identity]
        for index, analyzer in enumerate(self._analyzers, 1):
            if not set(analyzer.capabilities).issubset(admission.admitted_capabilities):
                continue
            producer_identities.append(analyzer.producer_identity)
            try:
                result = analyzer.collect(request.subject)
            except Exception:
                context = replace(
                    context,
                    findings=(*context.findings, AnalysisFinding(
                        f"finding-analyzer-{index}", "incomplete", "Analyzer replay failed safely."
                    )),
                    capabilities=(*context.capabilities, CapabilityObservation(f"analyzer-{index}", "unavailable")),
                )
                continue
            try:
                for document in result.evidence:
                    validate_contract_document(document, "evidence")
            except ContractValidationError:
                context = replace(
                    context,
                    findings=(*context.findings, AnalysisFinding(
                        f"finding-analyzer-contract-{index}", "conflicting", "Analyzer emitted invalid canonical evidence."
                    )),
                    capabilities=(*context.capabilities, CapabilityObservation(f"analyzer-{index}", "unavailable")),
                )
                continue
            context = replace(
                context,
                facts=(*context.facts, *(fact for fact in result.facts if fact.evidence_refs)),
                findings=(
                    *context.findings,
                    *result.findings,
                    *(
                        AnalysisFinding(
                            f"finding-analyzer-fact-{index}-{fact.fact_id}",
                            "incomplete",
                            f"Analyzer fact {fact.fact_id} did not include attributable evidence references.",
                        )
                        for fact in result.facts
                        if not fact.evidence_refs
                    ),
                ),
                capabilities=(*context.capabilities, *result.capabilities),
            )
            analyzer_evidence.extend(result.evidence)
            analyzer_artifacts.extend(result.artifacts)
        context = _require_capabilities(context, request.requested_capabilities)
        try:
            record = _build_record(
                request,
                digest,
                context,
                tuple(analyzer_evidence),
                tuple(analyzer_artifacts),
                admission.identities,
                tuple(producer_identities),
            )
        except AnalysisConflictError:
            raise
        except (ContractValidationError, TypeError, ValueError) as exc:
            raise AnalysisProcessingError("analysis normalization failed") from exc
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
                reason=(
                    "; ".join(record.manifest["gaps"])  # type: ignore[arg-type]
                    or f"analysis outcome is {outcome}"
                ),
            )
            checkpoint_event = None
        return self._store.record_analysis(record, start_event, outcome_event, checkpoint_event)


def _build_record(
    request: AnalysisRequest,
    request_digest: str,
    context: RepositoryContextResult,
    analyzer_evidence: tuple[Mapping[str, object], ...] = (),
    analyzer_artifacts: tuple[RawAnalysisArtifact, ...] = (),
    admission_identities: Mapping[str, Mapping[str, object]] | None = None,
    producer_identities: tuple[Mapping[str, object], ...] = (),
) -> AnalysisRecord:
    """Normalize repository context into an analysis bundle and checkpoint."""
    generated_fact_evidence = {
        fact.fact_id: _fact_evidence(request, fact)
        for fact in context.facts
        if not fact.evidence_refs
    }
    normalized_facts = tuple(
        replace(
            fact,
            evidence_refs=fact.evidence_refs or (str(generated_fact_evidence[fact.fact_id]["evidence_id"]),),
        )
        for fact in context.facts
    )
    context = replace(context, facts=normalized_facts)
    evidence = _unique_documents(
        (*request.supplied_evidence, *analyzer_evidence, *generated_fact_evidence.values()),
        "evidence_id",
        "evidence",
    )
    objectives = _unique_documents(request.supplied_objectives, "objective_id", "objective")
    if request.objective_refs and not request.supplied_objectives:
        context = replace(
            context,
            findings=(
                *context.findings,
                *(
                    AnalysisFinding(
                        f"finding-objective-reference-{objective_ref}",
                        "missing",
                        f"Referenced objective {objective_ref} was not supplied as a materialized objective document.",
                    )
                    for objective_ref in request.objective_refs
                ),
            ),
        )
    elif not request.supplied_objectives:
        context = replace(
            context,
            findings=(
                *context.findings,
                AnalysisFinding(
                    "finding-objective-source",
                    "incomplete",
                    "No behavior-bearing objective source was supplied; discovered repository metadata is not a test objective.",
                ),
            ),
        )
    derivation = derive_objective_candidates(objectives, context, evidence)
    context = replace(context, findings=(*context.findings, *derivation.findings))
    evidence_by_fact = {
        fact.fact_id: tuple(fact.evidence_refs)
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
            **({"digest": request.subject.content_digest} if request.subject.content_digest else {}),
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
                "certainty": fact.certainty,
                "status": fact.status,
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
        "assumptions": [
            "Materialized content matched the admitted subject digest."
            if request.subject.content_digest
            else "The materializer attested the repository revision; analyzed bytes are identified by profile_digest."
        ],
        "gaps": [finding.description for finding in context.findings],
        "limitations": [],
    }
    gate = evaluate_analysis(
        request.subject,
        manifest,
        evidence,
        context,
        objectives if objectives or request.objective_refs else None,
    )
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
    provenance_identities = _unique_identities((
        manifest["subject"],
        *(admission_identities or {}).values(),
        *(
            document["source"]["identity"]
            for document in evidence
            if isinstance(document.get("source"), Mapping)
            and isinstance(document["source"].get("identity"), Mapping)
        ),
    ))
    tool_identities = _unique_identities(producer_identities)
    provenance = {
        "contract": "atg.provenance",
        "version": "1.0",
        "provenance_id": provenance_id,
        "run_id": request.run_id,
        "recorded_at": request.occurred_at,
        "identities": provenance_identities,
        "tools": tool_identities,
        "artifacts": [
            profile_artifact["artifact"],
            tests_artifact["artifact"],
            coverage_artifact["artifact"],
            analysis_artifact["artifact"],
            *[artifact["artifact"] for artifact in replay_artifacts],
        ],
        "contract_versions": {"atg.analysis": "1.0", "atg.checkpoint": "1.0", "atg.evidence": "1.0", "atg.objective": "1.0", "atg.provenance": "1.0"},
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
    record = AnalysisRecord(
        request_digest,
        manifest,
        checkpoint if gate.reviewable else None,
        evidence,
        objectives,
        provenance,
        (profile_artifact, tests_artifact, coverage_artifact, *replay_artifacts, analysis_artifact),
    )
    validate_analysis_record(record)
    return record


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
            **({"digest": request.subject.content_digest} if request.subject.content_digest else {}),
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


def _unique_identities(identities) -> list[Mapping[str, object]]:
    """Return deterministic unique canonical identities for provenance."""
    unique: dict[str, Mapping[str, object]] = {}
    for identity in identities:
        if not isinstance(identity, Mapping):
            continue
        key = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        unique[key] = identity
    return [unique[key] for key in sorted(unique)]


def validate_analysis_record(record: AnalysisRecord) -> None:
    """Validate schemas, artifact closure, digests, and checkpoint binding."""
    for document, contract in (
        (record.manifest, "analysis"),
        (record.provenance, "provenance"),
    ):
        validate_contract_document(document, contract)
    for document in record.evidence:
        validate_contract_document(document, "evidence")
    for document in record.objectives:
        validate_contract_document(document, "objective")
    if record.checkpoint is not None:
        validate_contract_document(record.checkpoint, "checkpoint")
    elif record.manifest.get("outcome") == "needs_human_review":
        raise ContractValidationError("reviewable analysis is missing its checkpoint")

    artifacts: dict[str, Mapping[str, object]] = {}
    for stored in record.artifacts:
        metadata = stored.get("artifact")
        if not isinstance(metadata, Mapping) or "content" not in stored:
            raise ContractValidationError("invalid stored analysis artifact")
        artifact_id = metadata.get("artifact_id")
        if not isinstance(artifact_id, str) or artifact_id in artifacts:
            raise ContractValidationError("invalid or duplicate analysis artifact identity")
        encoded = json.dumps(stored["content"], sort_keys=True, separators=(",", ":")).encode()
        expected_digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        if metadata.get("digest") != expected_digest or metadata.get("size_bytes") != len(encoded):
            raise ContractValidationError("analysis artifact content metadata mismatch")
        artifacts[artifact_id] = metadata

    provenance_documents = [
        artifact for artifact in record.provenance.get("artifacts", [])
        if isinstance(artifact, Mapping)
    ]
    provenance_artifacts = {artifact.get("artifact_id"): artifact for artifact in provenance_documents}
    if len(provenance_artifacts) != len(provenance_documents):
        raise ContractValidationError("analysis provenance contains duplicate artifact identities")
    if record.manifest.get("provenance_ref") != record.provenance.get("provenance_id"):
        raise ContractValidationError("analysis provenance identity mismatch")
    if record.provenance.get("run_id") != record.manifest.get("run_id"):
        raise ContractValidationError("analysis provenance belongs to a different run")
    if provenance_artifacts != artifacts:
        raise ContractValidationError("analysis provenance artifact set differs from stored artifacts")
    if set(record.manifest.get("artifact_refs", [])) != set(artifacts):
        raise ContractValidationError("analysis manifest artifact set differs from stored artifacts")
    for artifact_id in record.manifest.get("artifact_refs", []):
        if artifact_id not in artifacts or provenance_artifacts.get(artifact_id) != artifacts[artifact_id]:
            raise ContractValidationError("analysis artifact reference is not closed by provenance")
    analysis_artifact_id = f"artifact-{record.manifest.get('analysis_id')}"
    analysis_stored = next(
        (stored for stored in record.artifacts if stored.get("artifact", {}).get("artifact_id") == analysis_artifact_id),
        None,
    )
    if analysis_stored is None or analysis_stored.get("content") != record.manifest:
        raise ContractValidationError("analysis manifest differs from its content-addressed artifact")
    if record.checkpoint is not None:
        reviewed = record.checkpoint.get("reviewed_artifact")
        if record.checkpoint.get("run_id") != record.manifest.get("run_id"):
            raise ContractValidationError("analysis checkpoint belongs to a different run")
        if not isinstance(reviewed, Mapping) or artifacts.get(reviewed.get("artifact_id")) != reviewed:
            raise ContractValidationError("analysis checkpoint artifact binding mismatch")
