import pathlib
import json
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.domain.contract_integrity import ContractIntegrityError, validate_documents  # noqa: E402


def graph_documents():
    run = {
        "contract": "atg.run_state", "version": "1.0", "run_id": "run-1",
        "status": "completed", "revision": 1, "updated_at": "2026-11-03T17:00:00Z",
        "processed_event_ids": [], "event_fingerprints": {},
    }
    objective = {
        "contract": "atg.objective", "version": "1.0", "objective_id": "objective-1",
        "statement": "Validate the regression", "kind": "regression", "priority": "high",
        "acceptance": ["The regression is detected"], "evidence_refs": [],
    }
    plan = {
        "contract": "atg.plan", "version": "1.0", "plan_id": "plan-1", "run_id": "run-1",
        "objective_refs": ["objective-1"], "steps": [{"step_id": "step-1", "stage": "execute", "action": "run tests"}],
        "status": "approved", "created_at": "2026-11-03T17:00:00Z",
    }
    generation = {
        "contract": "atg.generation", "version": "1.0", "generation_id": "generation-1",
        "run_id": "run-1", "plan_id": "plan-1",
        "base": {"kind": "git", "id": "base"}, "candidate": {"kind": "git", "id": "candidate"},
        "patch_digest": "sha256:" + "a" * 64, "changed_files": ["src/test/Test.java"],
        "status": "verified", "created_at": "2026-11-03T17:00:00Z",
    }
    executions = [
        {
            "contract": "atg.execution", "version": "1.0", "execution_id": execution_id,
            "run_id": "run-1", "generation_id": "generation-1", "subject": subject,
        }
        for execution_id, subject in (("execution-base", "base"), ("execution-candidate", "candidate"))
    ]
    quality = {
        "contract": "atg.quality", "version": "1.0", "quality_id": "quality-1", "run_id": "run-1",
        "execution_refs": ["execution-base", "execution-candidate"], "checks": [],
    }
    provenance = {
        "contract": "atg.provenance", "version": "1.0", "provenance_id": "provenance-1", "run_id": "run-1",
    }
    differential = {
        "contract": "atg.signed_differential", "version": "1.0", "differential_id": "differential-1",
        "run_id": "run-1", "request_id": "request-1", "base_execution_id": "execution-base", "candidate_execution_id": "execution-candidate",
        "quality_id": "quality-1", "provenance_id": "provenance-1", "comparisons": [],
    }
    request = {
        "contract": "atg.request", "version": "1.0", "request_id": "request-1", "run_id": "run-1",
        "objective_ids": ["objective-1"],
    }
    return [run, request, objective, plan, generation, *executions, quality, provenance, differential]


class ContractIntegrityTests(unittest.TestCase):
    def test_terminal_record_requires_request_and_admission(self):
        documents = graph_documents()
        documents.append({
            "contract": "atg.run_terminal",
            "terminal_id": "terminal-1",
            "run_id": "run-1",
            "request_id": "request-1",
            "admission_id": "admission-1",
        })
        with self.assertRaises(ContractIntegrityError) as raised:
            validate_documents(documents)
        self.assertIn(
            "run_terminal.admission_id references missing atg.run_admission/admission-1",
            raised.exception.errors,
        )

    def test_schema_valid_example_graph_is_consistent(self):
        root = pathlib.Path(__file__).parents[2] / "contracts" / "examples"
        filenames = [
            "run-state.json", "request.json", "evidence.json", "objective.json", "plan.json",
            "checkpoint.json", "feedback.json", "preflight.json", "generation.json",
            "execution-base.json", "execution.json", "quality.json", "provenance.json",
            "signed-differential.json", "analysis.json",
        ]
        documents = [json.loads((root / filename).read_text()) for filename in filenames]
        validate_documents(documents)

    def test_analysis_requires_same_run_request_and_provenance(self):
        root = pathlib.Path(__file__).parents[2] / "contracts" / "examples"
        filenames = ["request.json", "evidence.json", "objective.json", "provenance.json", "analysis.json"]
        documents = [json.loads((root / filename).read_text()) for filename in filenames]
        analysis = next(document for document in documents if document.get("contract") == "atg.analysis")
        analysis["run_id"] = "run-other"
        with self.assertRaises(ContractIntegrityError) as raised:
            validate_documents(documents)
        self.assertIn("analysis.request_id belongs to a different run", raised.exception.errors)

    def test_complete_evidence_graph_is_consistent(self):
        validate_documents(graph_documents())

    def test_missing_execution_is_rejected(self):
        documents = graph_documents()
        documents = [document for document in documents if document.get("execution_id") != "execution-base"]
        with self.assertRaises(ContractIntegrityError) as raised:
            validate_documents(documents)
        self.assertIn("differential.base_execution_id references missing atg.execution/execution-base", raised.exception.errors)

    def test_cross_run_reference_is_rejected(self):
        documents = graph_documents()
        generation = next(document for document in documents if document.get("generation_id") == "generation-1")
        generation["run_id"] = "run-2"
        with self.assertRaises(ContractIntegrityError) as raised:
            validate_documents(documents)
        self.assertIn("execution.generation_id belongs to a different run", raised.exception.errors)

    def test_duplicate_typed_document_id_is_rejected(self):
        documents = graph_documents()
        documents.append(dict(next(document for document in documents if document.get("plan_id") == "plan-1")))
        with self.assertRaises(ContractIntegrityError) as raised:
            validate_documents(documents)
        self.assertIn("duplicate document: atg.plan/plan-1", raised.exception.errors)

    def test_known_contract_without_typed_id_is_rejected(self):
        documents = graph_documents()
        execution = next(document for document in documents if document.get("execution_id") == "execution-base")
        del execution["execution_id"]
        with self.assertRaises(ContractIntegrityError) as raised:
            validate_documents(documents)
        self.assertIn("every document requires a contract and typed identifier", raised.exception.errors)

    def test_differential_requires_distinct_base_and_candidate_subjects(self):
        documents = graph_documents()
        differential = next(document for document in documents if document.get("differential_id") == "differential-1")
        candidate = next(document for document in documents if document.get("execution_id") == "execution-candidate")
        candidate["subject"] = "base"
        with self.assertRaises(ContractIntegrityError) as raised:
            validate_documents(documents)
        self.assertIn("differential.candidate_execution_id must reference a candidate execution", raised.exception.errors)

        candidate["subject"] = "candidate"
        differential["candidate_execution_id"] = "execution-base"
        with self.assertRaises(ContractIntegrityError) as raised:
            validate_documents(documents)
        self.assertIn("differential base and candidate executions must differ", raised.exception.errors)


if __name__ == "__main__":
    unittest.main()
