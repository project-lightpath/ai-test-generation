"""Cross-document integrity checks for ATG evidence graphs.

JSON Schema validates individual documents. This module validates the links
between those documents before a run can be published as complete evidence.
"""

from __future__ import annotations

from typing import Iterable, Mapping


class ContractIntegrityError(ValueError):
    """Raised when a collection of ATG documents has broken references."""

    def __init__(self, errors: Iterable[str]):
        """Create an error containing all discovered integrity violations."""
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


def validate_documents(documents: Iterable[Mapping[str, object]]) -> None:
    """Validate IDs, run ownership, and evidence references across documents."""

    docs = list(documents)
    index: dict[tuple[str, str], Mapping[str, object]] = {}
    errors: list[str] = []
    for document in docs:
        contract = document.get("contract")
        identifier = _document_id(document)
        if not isinstance(contract, str) or not isinstance(identifier, str):
            errors.append("every document requires a contract and typed identifier")
            continue
        key = (contract, identifier)
        if key in index:
            errors.append(f"duplicate document: {contract}/{identifier}")
        index[key] = document

    def require(contract: str, identifier: object, relation: str) -> Mapping[str, object] | None:
        """Resolve a typed document reference and record missing links."""
        if not isinstance(identifier, str):
            errors.append(f"{relation} must be a string identifier")
            return None
        found = index.get((contract, identifier))
        if found is None:
            errors.append(f"{relation} references missing {contract}/{identifier}")
        return found

    def require_run(document: Mapping[str, object], relation: str) -> None:
        """Require a referenced run-state document when a run ID is present."""
        run_id = document.get("run_id")
        if run_id is not None:
            require("atg.run_state", run_id, relation)

    for document in docs:
        contract = document.get("contract")
        if contract == "atg.request":
            require_run(document, "request.run_id")
            for objective_id in document.get("objective_ids", []):
                require("atg.objective", objective_id, "request.objective_ids")
        elif contract == "atg.analysis":
            require_run(document, "analysis.run_id")
            _require_related(document, "atg.request", document.get("request_id"), require, errors, "analysis.request_id")
            for evidence_id in document.get("evidence_refs", []):
                evidence = require("atg.evidence", evidence_id, "analysis.evidence_refs")
                if evidence is not None and evidence.get("subject") != document.get("subject"):
                    errors.append("analysis.evidence_refs belongs to a different subject")
            for objective_id in document.get("objective_refs", []):
                require("atg.objective", objective_id, "analysis.objective_refs")
            provenance = _require_related(document, "atg.provenance", document.get("provenance_ref"), require, errors, "analysis.provenance_ref")
            if provenance is not None:
                provenance_artifacts = {
                    artifact.get("artifact_id"): artifact
                    for artifact in provenance.get("artifacts", [])
                    if isinstance(artifact, Mapping)
                }
                for artifact_id in document.get("artifact_refs", []):
                    if artifact_id not in provenance_artifacts:
                        errors.append(f"analysis.artifact_refs references missing provenance artifact {artifact_id}")
            candidates = document.get("objective_candidates", [])
            candidate_ids = {candidate.get("candidate_id") for candidate in candidates}
            if len(candidate_ids) != len(candidates):
                errors.append("analysis.objective_candidates requires unique candidate IDs")
            for candidate in candidates:
                objective_ref = candidate.get("objective_ref")
                require("atg.objective", objective_ref, "analysis.objective_candidates.objective_ref")
                if objective_ref not in document.get("objective_refs", []):
                    errors.append("analysis candidate objective_ref is absent from objective_refs")
                _require_evidence_refs(
                    candidate.get("evidence_refs", []), require, errors,
                    "analysis.objective_candidates.evidence_refs",
                )
                duplicate_of = candidate.get("duplicate_of")
                if duplicate_of is not None and duplicate_of not in candidate_ids:
                    errors.append("analysis duplicate candidate references a missing candidate")
            for fact in document.get("fact_summary", []):
                _require_evidence_refs(fact.get("evidence_refs", []), require, errors, "analysis.fact_summary.evidence_refs")
            for finding in document.get("findings", []):
                _require_evidence_refs(finding.get("evidence_refs", []), require, errors, "analysis.finding.evidence_refs")
        elif contract == "atg.run_admission":
            request = _require_related(document, "atg.request", document.get("request_id"), require, errors, "run_admission.request_id")
            if request is not None and request.get("run_id") != document.get("run_id"):
                errors.append("run_admission.request_id belongs to a different run")
        elif contract == "atg.plan":
            require_run(document, "plan.run_id")
            analysis = _require_related(document, "atg.analysis", document.get("analysis_id"), require, errors, "plan.analysis_id")
            if analysis is not None:
                if analysis.get("run_id") != document.get("run_id"):
                    errors.append("plan.analysis_id belongs to a different run")
                if document.get("analysis_revision") != analysis.get("revision"):
                    errors.append("plan analysis revision differs from analysis")
                analysis_artifact = document.get("analysis_artifact", {})
                if analysis_artifact.get("artifact_id") not in analysis.get("artifact_refs", []):
                    errors.append("plan.analysis_artifact is not referenced by analysis")
                provenance = index.get(("atg.provenance", analysis.get("provenance_ref")))
                if provenance is not None and analysis_artifact not in provenance.get("artifacts", []):
                    errors.append("plan.analysis_artifact metadata differs from analysis provenance")
                if document.get("subject") != analysis.get("subject"):
                    errors.append("plan subject differs from analysis subject")
                analysis_objectives = set(analysis.get("objective_refs", []))
                if set(document.get("objective_refs", [])) != analysis_objectives:
                    errors.append("plan.objective_refs must exhaustively match analysis objectives")
                candidates = analysis.get("objective_candidates", [])
                candidate_objectives = {candidate.get("objective_ref") for candidate in candidates}
                if candidate_objectives != analysis_objectives:
                    errors.append("analysis candidates must exhaustively cover analysis objectives")
            for objective_id in document.get("objective_refs", []):
                require("atg.objective", objective_id, "plan.objective_refs")
            coverage = document.get("coverage_matrix", [])
            coverage_ids = [row.get("objective_ref") for row in coverage]
            if len(coverage_ids) != len(set(coverage_ids)):
                errors.append("plan.coverage_matrix requires unique objective references")
            if set(coverage_ids) != set(document.get("objective_refs", [])):
                errors.append("plan.coverage_matrix must cover every plan objective exactly once")
            feasibility = document.get("feasibility", [])
            feasibility_ids = [row.get("objective_ref") for row in feasibility]
            if len(feasibility_ids) != len(set(feasibility_ids)):
                errors.append("plan.feasibility requires unique objective references")
            if set(feasibility_ids) != set(document.get("objective_refs", [])):
                errors.append("plan.feasibility must cover every plan objective exactly once")
            step_ids = [step.get("step_id") for step in document.get("steps", [])]
            if len(step_ids) != len(set(step_ids)):
                errors.append("plan.steps requires unique step IDs")
            _require_related(document, "atg.provenance", document.get("provenance_ref"), require, errors, "plan.provenance_ref")
        elif contract == "atg.objective":
            _require_evidence_refs(document.get("evidence_refs", []), require, errors, "objective.evidence_refs")
        elif contract in {
            "atg.analysis",
            "atg.checkpoint",
            "atg.feedback",
            "atg.generation",
            "atg.execution",
            "atg.quality",
            "atg.provenance",
            "atg.preflight",
            "atg.platform_readiness",
            "atg.run_admission",
            "atg.run_terminal",
            "atg.run_event",
        }:
            require_run(document, f"{contract}.run_id")

        if contract == "atg.feedback" and document.get("checkpoint_id") is not None:
            checkpoint = _require_related(
                document,
                "atg.checkpoint",
                document["checkpoint_id"],
                require,
                errors,
                "feedback.checkpoint_id",
            )
            if checkpoint is not None and document.get("plan_id") is not None:
                plan = _require_related(document, "atg.plan", document["plan_id"], require, errors, "feedback.plan_id")
                reviewed = checkpoint.get("reviewed_artifact") or {}
                if checkpoint.get("kind") != "plan_review" or reviewed.get("artifact_id") != f"artifact-{document['plan_id']}":
                    errors.append("feedback checkpoint does not review the referenced plan")
                if plan is not None:
                    if document.get("plan_revision") is not None and document["plan_revision"] != plan.get("plan_revision"):
                        errors.append("feedback plan revision differs from referenced plan")
                    if document.get("plan_artifact_digest") is not None and document["plan_artifact_digest"] != reviewed.get("digest"):
                        errors.append("feedback plan artifact digest differs from checkpoint")
        if contract == "atg.checkpoint" and document.get("kind") == "analysis_review":
            reviewed = document.get("reviewed_artifact") or {}
            analyses = [candidate for candidate in docs if candidate.get("contract") == "atg.analysis" and candidate.get("run_id") == document.get("run_id")]
            matching_analyses = [candidate for candidate in analyses if reviewed.get("artifact_id") in candidate.get("artifact_refs", [])]
            if not matching_analyses:
                errors.append("analysis_review checkpoint artifact is not referenced by an analysis manifest")
            for analysis in matching_analyses:
                expected_artifact_id = f"artifact-{analysis.get('analysis_id')}"
                if reviewed.get("artifact_id") != expected_artifact_id or reviewed.get("media_type") != "application/vnd.atg.analysis+json":
                    errors.append("analysis_review checkpoint must reference the analysis manifest artifact")
                provenance = index.get(("atg.provenance", analysis.get("provenance_ref")))
                artifacts = provenance.get("artifacts", []) if provenance is not None else []
                if reviewed not in artifacts:
                    errors.append("analysis_review checkpoint artifact metadata differs from provenance")
        if contract == "atg.checkpoint" and document.get("kind") == "plan_review":
            reviewed = document.get("reviewed_artifact") or {}
            plans = [candidate for candidate in docs if candidate.get("contract") == "atg.plan" and candidate.get("run_id") == document.get("run_id")]
            matching_plans = [plan for plan in plans if reviewed.get("artifact_id") in plan.get("artifact_refs", [])]
            if not matching_plans:
                errors.append("plan_review checkpoint artifact is not referenced by a plan manifest")
            for plan in matching_plans:
                expected_artifact_id = f"artifact-{plan.get('plan_id')}"
                if reviewed.get("artifact_id") != expected_artifact_id or reviewed.get("media_type") != "application/vnd.atg.plan+json":
                    errors.append("plan_review checkpoint must reference the plan manifest artifact")
                provenance = index.get(("atg.provenance", plan.get("provenance_ref")))
                artifacts = provenance.get("artifacts", []) if provenance is not None else []
                if reviewed not in artifacts:
                    errors.append("plan_review checkpoint artifact metadata differs from provenance")
        if contract == "atg.run_terminal":
            request = _require_related(
                document, "atg.request", document.get("request_id"), require, errors,
                "run_terminal.request_id",
            )
            admission = _require_related(
                document, "atg.run_admission", document.get("admission_id"), require, errors,
                "run_terminal.admission_id",
            )
            if request is not None and admission is not None:
                if admission.get("request_id") != request.get("request_id"):
                    errors.append("run_terminal admission and request IDs differ")
                if admission.get("request_digest") != document.get("request_digest"):
                    errors.append("run_terminal request digest differs from admission")
                if request.get("request_digest") is not None and admission.get("request_digest") != request.get("request_digest"):
                    errors.append("run_admission request digest differs from request")
                if document.get("status") != admission.get("status"):
                    errors.append("run_terminal status must match admission status")
        if contract == "atg.feedback":
            _require_evidence_refs(document.get("evidence_refs", []), require, errors, "feedback.evidence_refs")
        if contract == "atg.generation":
            plan = _require_related(document, "atg.plan", document.get("plan_id"), require, errors, "generation.plan_id")
            if plan is not None:
                if plan.get("run_id") != document.get("run_id"):
                    errors.append("generation.plan_id belongs to a different run")
                if plan.get("status") != "approved":
                    errors.append("generation requires an approved plan")
                if document.get("plan_revision") != plan.get("plan_revision"):
                    errors.append("generation plan revision differs from approved plan")
                checkpoint = _require_related(document, "atg.checkpoint", document.get("plan_checkpoint_id"), require, errors, "generation.plan_checkpoint_id")
                if checkpoint is not None:
                    reviewed = checkpoint.get("reviewed_artifact") or {}
                    if checkpoint.get("kind") != "plan_review" or checkpoint.get("state") != "approved":
                        errors.append("generation requires an approved plan review checkpoint")
                    if reviewed.get("artifact_id") != f"artifact-{plan.get('plan_id')}" or reviewed.get("digest") != document.get("plan_artifact_digest"):
                        errors.append("generation requires the approved plan artifact digest")
                    decision = checkpoint.get("decision") or {}
                    if decision.get("decision_id") != document.get("plan_decision_id"):
                        errors.append("generation plan_decision_id does not match checkpoint decision")
                    if decision.get("value") != "approved":
                        errors.append("generation requires an approved plan decision")
        if contract == "atg.execution":
            _require_related(
                document,
                "atg.generation",
                document.get("generation_id"),
                require,
                errors,
                "execution.generation_id",
            )
        if contract == "atg.quality":
            for execution_id in document.get("execution_refs", []):
                _require_related(document, "atg.execution", execution_id, require, errors, "quality.execution_refs")
            for check in document.get("checks", []):
                _require_evidence_refs(
                    check.get("evidence_refs", []), require, errors, "quality.check.evidence_refs"
                )
        if contract == "atg.signed_differential":
            _require_related(document, "atg.request", document.get("request_id"), require, errors, "differential.request_id")
            if document.get("plan_id") is not None:
                _require_related(document, "atg.plan", document["plan_id"], require, errors, "differential.plan_id")
            if document.get("generation_id") is not None:
                _require_related(
                    document, "atg.generation", document["generation_id"], require, errors, "differential.generation_id"
                )
            base_execution = _require_related(
                document, "atg.execution", document.get("base_execution_id"), require, errors,
                "differential.base_execution_id",
            )
            candidate_execution = _require_related(
                document, "atg.execution", document.get("candidate_execution_id"), require, errors,
                "differential.candidate_execution_id",
            )
            if document.get("base_execution_id") == document.get("candidate_execution_id"):
                errors.append("differential base and candidate executions must differ")
            if base_execution is not None and base_execution.get("subject") != "base":
                errors.append("differential.base_execution_id must reference a base execution")
            if candidate_execution is not None and candidate_execution.get("subject") != "candidate":
                errors.append("differential.candidate_execution_id must reference a candidate execution")
            _require_related(document, "atg.quality", document.get("quality_id"), require, errors, "differential.quality_id")
            _require_related(
                document, "atg.provenance", document.get("provenance_id"), require, errors,
                "differential.provenance_id",
            )
            for comparison in document.get("comparisons", []):
                _require_evidence_refs(
                    comparison.get("evidence_refs", []), require, errors, "differential.comparison.evidence_refs"
                )
        if contract == "atg.run_event" and document.get("checkpoint_id") is not None:
            _require_related(
                document,
                "atg.checkpoint",
                document["checkpoint_id"],
                require,
                errors,
                "run_event.checkpoint_id",
            )

    if errors:
        raise ContractIntegrityError(errors)


def _document_id(document: Mapping[str, object]) -> str | None:
    """Return the contract-specific identifier from a document."""
    contract_fields = {
        "atg.request": "request_id",
        "atg.analysis": "analysis_id",
        "atg.evidence": "evidence_id",
        "atg.objective": "objective_id",
        "atg.plan": "plan_id",
        "atg.checkpoint": "checkpoint_id",
        "atg.feedback": "feedback_id",
        "atg.generation": "generation_id",
        "atg.execution": "execution_id",
        "atg.quality": "quality_id",
        "atg.provenance": "provenance_id",
        "atg.preflight": "preflight_id",
        "atg.platform_readiness": "readiness_id",
        "atg.run_admission": "admission_id",
        "atg.run_terminal": "terminal_id",
        "atg.compatibility": "compatibility_id",
        "atg.signed_differential": "differential_id",
        "atg.run_state": "run_id",
        "atg.run_event": "event_id",
    }
    preferred = contract_fields.get(document.get("contract"))
    if preferred:
        return document[preferred] if isinstance(document.get(preferred), str) else None
    for field in (
        "request_id",
        "analysis_id",
        "evidence_id",
        "objective_id",
        "plan_id",
        "checkpoint_id",
        "feedback_id",
        "generation_id",
        "execution_id",
        "quality_id",
        "provenance_id",
        "preflight_id",
        "readiness_id",
        "admission_id",
        "terminal_id",
        "compatibility_id",
        "differential_id",
        "run_id",
        "event_id",
    ):
        value = document.get(field)
        if isinstance(value, str):
            return value
    return None


def _require_evidence_refs(refs, require, errors: list[str], relation: str) -> None:
    """Require every evidence reference in a document relationship."""
    for evidence_id in refs:
        require("atg.evidence", evidence_id, relation)


def _require_related(document, contract, identifier, require, errors, relation):
    """Resolve a related document and verify that it belongs to the same run."""
    related = require(contract, identifier, relation)
    if related is not None and document.get("run_id") != related.get("run_id"):
        errors.append(f"{relation} belongs to a different run")
    return related
