import json
import pathlib
import sys
from dataclasses import replace
from tempfile import TemporaryDirectory
import unittest

from jsonschema import Draft202012Validator, FormatChecker, RefResolver

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.adapters.persistence.sqlite_run_state import SQLiteRunStateStore  # noqa: E402
from atg.adapters.analysis.java_facts import JavaSemanticFactsReplayAdapter  # noqa: E402
from atg.adapters.analysis.vpe_context import VPEAnalysisContextReplayAdapter  # noqa: E402
from atg.adapters.repository.local import LocalRepositoryContextAdapter  # noqa: E402
from atg.application.admission import AdmissionRequest, AdmissionService, CheckResult  # noqa: E402
from atg.application.analysis import AnalysisConflictError, AnalysisRequest, AnalysisService  # noqa: E402
from atg.domain.contract_integrity import validate_documents  # noqa: E402
from atg.domain.repository_analysis import RepositorySubject  # noqa: E402
from atg.domain.repository_analysis import AnalysisFinding  # noqa: E402
from atg.domain.run_state import StaleEventError  # noqa: E402


CONTRACT_ROOT = pathlib.Path(__file__).parents[2] / "contracts" / "v1"


def validate_document(document, schema_name):
    """Validate a generated document against its canonical contract schema."""
    schemas = {path.name: json.loads(path.read_text()) for path in CONTRACT_ROOT.glob("*.schema.json")}
    schema = schemas[f"{schema_name}.schema.json"]
    store = {**schemas, schemas["common.schema.json"]["$id"]: schemas["common.schema.json"]}
    resolver = RefResolver.from_schema(schema, store=store)
    Draft202012Validator(schema, resolver=resolver, format_checker=FormatChecker()).validate(document)


class PassingCheck:
    def evaluate(self, request):
        """Return one deterministic passing admission check."""
        return CheckResult("fixed", True, "pass")


class UnauthorizedContextAdapter:
    """Return useful facts plus a deterministic authorization violation."""

    def collect(self, request):
        """Delegate discovery and append an unauthorized-context finding."""
        result = LocalRepositoryContextAdapter().collect(request)
        return replace(result, findings=(AnalysisFinding("finding-unauthorized", "unauthorized", "Repository content attempted to authorize execution."),))


def create_repository(root: pathlib.Path) -> None:
    """Create a small repository with one manifest and one test."""
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'analysis-fixture'\n")
    (root / "src" / "app.py").write_text("def value():\n    return 1\n")
    (root / "tests" / "test_app.py").write_text("def test_value():\n    assert True\n")


def admit(store) -> None:
    """Create the durable admitted run required by Analyze."""
    AdmissionService(store, (PassingCheck(),)).admit(AdmissionRequest(
        request_id="request-1",
        run_id="run-1",
        submitted_at="2026-11-09T17:00:00Z",
        idempotency_key="admission-key-1",
    ))


def analysis_request(
    root: pathlib.Path,
    *,
    revision="abc123",
    expected_revision=1,
    objective_refs=("objective-1",),
    supplied_evidence=(),
    supplied_objectives=(),
    requested_capabilities=(),
) -> AnalysisRequest:
    """Build a deterministic Analyze request for the fixture repository."""
    return AnalysisRequest(
        analysis_id="analysis-1",
        request_id="request-1",
        run_id="run-1",
        repository_root=str(root),
        subject=RepositorySubject("https://git.example/fixture.git", revision),
        expected_revision=expected_revision,
        occurred_at="2099-11-09T17:00:00Z",
        objective_refs=tuple(objective_refs),
        supplied_evidence=tuple(supplied_evidence),
        supplied_objectives=tuple(supplied_objectives),
        requested_capabilities=tuple(requested_capabilities),
    )


def supplied_evidence(evidence_id="evidence-driver-1"):
    """Build canonical supplied evidence bound to the fixture subject."""
    return {
        "contract": "atg.evidence",
        "version": "1.0",
        "evidence_id": evidence_id,
        "category": "repository",
        "subject": {"kind": "git-repository", "id": "https://git.example/fixture.git", "version": "abc123"},
        "source": {
            "identity": {"kind": "issue", "id": "issue-1"},
            "locator": "https://issues.example/issue-1",
            "retrieved_at": "2099-11-09T17:00:00Z",
        },
        "observed_at": "2099-11-09T17:00:00Z",
        "claims": ["The affected behavior must reject invalid input."],
        "artifacts": [],
        "limitations": [],
    }


def supplied_objective(objective_id="objective-1", evidence_refs=("evidence-driver-1",), statement="Reject invalid input"):
    """Build a canonical supplied objective for candidate derivation."""
    return {
        "contract": "atg.objective",
        "version": "1.0",
        "objective_id": objective_id,
        "statement": statement,
        "kind": "regression",
        "driver_type": "bug_report",
        "priority": "high",
        "acceptance": ["Invalid input is rejected."],
        "evidence_refs": list(evidence_refs),
        "target_refs": ["target-handler"],
        "assumptions": [],
        "gaps": [],
    }


class AnalysisServiceTests(unittest.TestCase):
    def test_analysis_persists_schema_valid_bundle_and_review_checkpoint(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = SQLiteRunStateStore()
            admit(store)

            record = AnalysisService(store, LocalRepositoryContextAdapter()).analyze(analysis_request(root))

            validate_document(record.manifest, "analysis")
            validate_document(record.checkpoint, "checkpoint")
            for evidence in record.evidence:
                validate_document(evidence, "evidence")
            validate_document(record.provenance, "provenance")
            objective = {
                "contract": "atg.objective",
                "objective_id": "objective-1",
                "evidence_refs": [record.evidence[0]["evidence_id"]],
            }
            validate_documents([
                store.get_run("run-1").to_document(),
                {"contract": "atg.request", "request_id": "request-1", "run_id": "run-1", "objective_ids": ["objective-1"]},
                objective,
                *record.evidence,
                record.provenance,
                record.manifest,
                record.checkpoint,
            ])
            self.assertFalse(record.duplicate)
            self.assertEqual(record.manifest["outcome"], "needs_human_review")
            self.assertEqual(record.manifest["objective_refs"], ["objective-1"])
            self.assertEqual(record.checkpoint["reviewed_artifact"]["artifact_id"], "artifact-analysis-1")
            self.assertEqual(store.get_run("run-1").status, "analysis_review")
            self.assertEqual(store.get_run("run-1").revision, 4)
            self.assertEqual(
                [event.event_type for event in store.events("run-1")],
                ["admit", "analysis_started", "analysis_completed", "checkpoint_created"],
            )
            store.close()

    def test_duplicate_returns_stored_bundle_without_rescanning(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = SQLiteRunStateStore()
            admit(store)
            service = AnalysisService(store, LocalRepositoryContextAdapter())
            request = analysis_request(root)
            first = service.analyze(request)
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()

            duplicate = service.analyze(request)

            self.assertTrue(duplicate.duplicate)
            self.assertEqual(duplicate.manifest, first.manifest)
            self.assertEqual(len(tuple(store.events("run-1"))), 4)
            store.close()

    def test_reusing_analysis_identity_for_different_subject_conflicts(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = SQLiteRunStateStore()
            admit(store)
            service = AnalysisService(store, LocalRepositoryContextAdapter())
            service.analyze(analysis_request(root))

            with self.assertRaises(AnalysisConflictError):
                service.analyze(analysis_request(root, revision="different"))
            store.close()

    def test_analysis_request_must_match_admitted_request(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = SQLiteRunStateStore()
            admit(store)
            request = analysis_request(root)
            request = AnalysisRequest(
                request.analysis_id,
                "request-other",
                request.run_id,
                request.repository_root,
                request.subject,
                request.expected_revision,
                request.occurred_at,
                request.objective_refs,
            )

            with self.assertRaises(AnalysisConflictError):
                AnalysisService(store, LocalRepositoryContextAdapter()).analyze(request)
            self.assertEqual(store.get_run("run-1").status, "admitted")
            store.close()

    def test_completed_analysis_survives_store_restart(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "repository"
            root.mkdir()
            create_repository(root)
            database = pathlib.Path(directory) / "state.sqlite3"
            first = SQLiteRunStateStore(database)
            admit(first)
            expected = AnalysisService(first, LocalRepositoryContextAdapter()).analyze(analysis_request(root))
            first.close()

            restarted = SQLiteRunStateStore(database)
            stored = restarted.get_analysis("analysis-1")
            self.assertIsNotNone(stored)
            self.assertEqual(stored.manifest, expected.manifest)
            self.assertEqual(restarted.replay("run-1").status, "analysis_review")
            restarted.close()

    def test_supplied_objective_is_persisted_as_evidence_bounded_candidate(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = SQLiteRunStateStore()
            admit(store)
            request = analysis_request(
                root,
                supplied_evidence=(supplied_evidence(),),
                supplied_objectives=(supplied_objective(),),
            )

            record = AnalysisService(store, LocalRepositoryContextAdapter()).analyze(request)

            self.assertEqual(record.objectives, (supplied_objective(),))
            validate_document(record.objectives[0], "objective")
            candidate = record.manifest["objective_candidates"][0]
            self.assertEqual(candidate["disposition"], "candidate")
            self.assertEqual(candidate["validation_role"], "regression")
            self.assertEqual(candidate["expected_behavior_source"], "attributable")
            self.assertIn("evidence-driver-1", record.manifest["evidence_refs"])
            self.assertIsNotNone(record.checkpoint)
            validate_documents([
                store.get_run("run-1").to_document(),
                {"contract": "atg.request", "request_id": "request-1", "run_id": "run-1", "objective_ids": ["objective-1"]},
                *record.evidence,
                *record.objectives,
                record.provenance,
                record.manifest,
                record.checkpoint,
            ])
            store.close()

    def test_uncited_supplied_objective_is_persisted_but_not_reviewable(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = SQLiteRunStateStore()
            admit(store)
            request = analysis_request(
                root,
                supplied_objectives=(supplied_objective(evidence_refs=()),),
            )

            record = AnalysisService(store, LocalRepositoryContextAdapter()).analyze(request)

            self.assertEqual(record.manifest["objective_candidates"][0]["disposition"], "needs_evidence")
            self.assertEqual(record.manifest["outcome"], "blocked")
            self.assertIsNone(record.checkpoint)
            self.assertEqual(store.get_run("run-1").status, "blocked")
            store.close()

    def test_supplied_document_order_does_not_change_idempotency_digest(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            evidence_documents = (supplied_evidence("evidence-driver-1"), supplied_evidence("evidence-driver-2"))
            objective_documents = (
                supplied_objective("objective-1", ("evidence-driver-1",), "First behavior"),
                supplied_objective("objective-2", ("evidence-driver-2",), "Second behavior"),
            )
            first = analysis_request(
                root,
                objective_refs=("objective-1", "objective-2"),
                supplied_evidence=evidence_documents,
                supplied_objectives=objective_documents,
            )
            second = analysis_request(
                root,
                objective_refs=("objective-1", "objective-2"),
                supplied_evidence=tuple(reversed(evidence_documents)),
                supplied_objectives=tuple(reversed(objective_documents)),
            )
            self.assertEqual(first.digest(), second.digest())

    def test_replay_analyzers_merge_evidence_capabilities_and_raw_artifacts(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            fixture_root = pathlib.Path(__file__).parents[2] / "fixtures" / "replay"
            vpe = json.loads((fixture_root / "vpe-analysis-context.json").read_text())
            java = json.loads((fixture_root / "java-semantic-call-facts.json").read_text())
            store = SQLiteRunStateStore()
            admit(store)
            request = analysis_request(
                root,
                requested_capabilities=("repository-preparation", "java-semantic-calls"),
            )

            record = AnalysisService(
                store,
                LocalRepositoryContextAdapter(),
                (VPEAnalysisContextReplayAdapter(vpe), JavaSemanticFactsReplayAdapter(java)),
            ).analyze(request)

            capabilities = {item["capability"]: item for item in record.manifest["capabilities"]}
            self.assertEqual(capabilities["repository-preparation"]["status"], "available")
            self.assertTrue(capabilities["repository-preparation"]["required"])
            self.assertEqual(capabilities["java-semantic-calls"]["status"], "available")
            self.assertTrue(any(ref.startswith("artifact-vpe-context-") for ref in record.manifest["artifact_refs"]))
            self.assertTrue(any(ref.startswith("artifact-java-semantic-facts-") for ref in record.manifest["artifact_refs"]))
            self.assertTrue(any(evidence["source"]["identity"]["kind"] == "java-analysis-adapter" for evidence in record.evidence))
            self.assertIsNotNone(record.checkpoint)
            store.close()

    def test_missing_required_analyzer_capability_is_not_evaluable(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = SQLiteRunStateStore()
            admit(store)
            request = analysis_request(root, requested_capabilities=("java-semantic-calls",))

            record = AnalysisService(store, LocalRepositoryContextAdapter()).analyze(request)

            capability = next(item for item in record.manifest["capabilities"] if item["capability"] == "java-semantic-calls")
            self.assertEqual(capability["status"], "unavailable")
            self.assertTrue(capability["required"])
            self.assertEqual(record.manifest["outcome"], "not_evaluable")
            self.assertIsNone(record.checkpoint)
            store.close()

    def test_non_reviewable_gate_outcomes_create_no_checkpoint(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            store = SQLiteRunStateStore()
            admit(store)

            inconclusive = AnalysisService(store, LocalRepositoryContextAdapter()).analyze(analysis_request(root))

            self.assertEqual(inconclusive.manifest["outcome"], "not_evaluable")
            self.assertIsNone(inconclusive.checkpoint)
            self.assertEqual(store.get_run("run-1").status, "inconclusive")
            self.assertEqual([event.event_type for event in store.events("run-1")][-1], "analysis_inconclusive")
            store.close()

        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = SQLiteRunStateStore()
            admit(store)

            blocked = AnalysisService(store, UnauthorizedContextAdapter()).analyze(analysis_request(root))

            self.assertEqual(blocked.manifest["outcome"], "blocked")
            self.assertIsNone(blocked.checkpoint)
            self.assertEqual(store.get_run("run-1").status, "blocked")
            self.assertEqual([event.event_type for event in store.events("run-1")][-1], "analysis_blocked")
            store.close()

    def test_analysis_requires_admitted_run_and_expected_revision(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = SQLiteRunStateStore()
            store.create_run("run-1", "2026-11-09T17:00:00Z")
            service = AnalysisService(store, LocalRepositoryContextAdapter())
            with self.assertRaises(AnalysisConflictError):
                service.analyze(analysis_request(root, expected_revision=0))

            store.close()
            store = SQLiteRunStateStore()
            admit(store)
            with self.assertRaises(StaleEventError):
                service = AnalysisService(store, LocalRepositoryContextAdapter())
                service.analyze(analysis_request(root, expected_revision=0))
            store.close()


if __name__ == "__main__":
    unittest.main()
