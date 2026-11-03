"""Cross-document integrity checks for ATG evidence graphs.

JSON Schema validates individual documents. This module validates the links
between those documents before a run can be published as complete evidence.
"""

from __future__ import annotations

from typing import Iterable, Mapping


class ContractIntegrityError(ValueError):
    """Raised when a collection of ATG documents has broken references."""

    def __init__(self, errors: Iterable[str]):
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
        if not isinstance(identifier, str):
            errors.append(f"{relation} must be a string identifier")
            return None
        found = index.get((contract, identifier))
        if found is None:
            errors.append(f"{relation} references missing {contract}/{identifier}")
        return found

    def require_run(document: Mapping[str, object], relation: str) -> None:
        run_id = document.get("run_id")
        if run_id is not None:
            require("atg.run_state", run_id, relation)

    for document in docs:
        contract = document.get("contract")
        if contract == "atg.request":
            require_run(document, "request.run_id")
            for objective_id in document.get("objective_ids", []):
                require("atg.objective", objective_id, "request.objective_ids")
        elif contract == "atg.plan":
            require_run(document, "plan.run_id")
            for objective_id in document.get("objective_refs", []):
                require("atg.objective", objective_id, "plan.objective_refs")
        elif contract == "atg.objective":
            _require_evidence_refs(document.get("evidence_refs", []), require, errors, "objective.evidence_refs")
        elif contract in {
            "atg.checkpoint",
            "atg.feedback",
            "atg.generation",
            "atg.execution",
            "atg.quality",
            "atg.provenance",
            "atg.preflight",
            "atg.run_event",
        }:
            require_run(document, f"{contract}.run_id")

        if contract == "atg.feedback" and document.get("checkpoint_id") is not None:
            _require_related(
                document,
                "atg.checkpoint",
                document["checkpoint_id"],
                require,
                errors,
                "feedback.checkpoint_id",
            )
        if contract == "atg.feedback":
            _require_evidence_refs(document.get("evidence_refs", []), require, errors, "feedback.evidence_refs")
        if contract == "atg.generation":
            _require_related(document, "atg.plan", document.get("plan_id"), require, errors, "generation.plan_id")
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
            _require_related(
                document, "atg.execution", document.get("base_execution_id"), require, errors,
                "differential.base_execution_id",
            )
            _require_related(
                document, "atg.execution", document.get("candidate_execution_id"), require, errors,
                "differential.candidate_execution_id",
            )
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
    contract_fields = {
        "atg.request": "request_id",
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
        "atg.compatibility": "compatibility_id",
        "atg.signed_differential": "differential_id",
        "atg.run_state": "run_id",
        "atg.run_event": "event_id",
    }
    preferred = contract_fields.get(document.get("contract"))
    if preferred and isinstance(document.get(preferred), str):
        return document[preferred]
    for field in (
        "request_id",
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
    for evidence_id in refs:
        require("atg.evidence", evidence_id, relation)


def _require_related(document, contract, identifier, require, errors, relation):
    related = require(contract, identifier, relation)
    if related is not None and document.get("run_id") != related.get("run_id"):
        errors.append(f"{relation} belongs to a different run")
    return related
