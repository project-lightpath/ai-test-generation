import json
import pathlib
import sqlite3
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
from atg.application.analysis import AnalysisConflictError, AnalysisProcessingError, AnalysisRequest, AnalysisService  # noqa: E402
from atg.domain.schema_validation import ContractValidationError  # noqa: E402
from atg.domain.contract_integrity import validate_documents  # noqa: E402
from atg.domain.repository_analysis import CapabilityObservation, RepositorySubject  # noqa: E402
from atg.domain.repository_analysis import AnalysisFinding  # noqa: E402
from atg.domain.run_state import StaleEventError  # noqa: E402
from atg.ports.repository import RepositoryContextRequest  # noqa: E402
from atg.ports.analyzer import AnalyzerEvidenceResult  # noqa: E402


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


class BlockingCheck:
    """Return a deterministic blocking admission check."""

    def evaluate(self, request):
        """Block admission before Analyze can inspect repository content."""
        return CheckResult("fixed", True, "fail", "blocked for test")


class UnauthorizedContextAdapter:
    """Return useful facts plus a deterministic authorization violation."""

    producer_identity = {"kind": "repository-analyzer", "id": "unauthorized-test-adapter", "version": "1"}

    def collect(self, request):
        """Delegate discovery and append an unauthorized-context finding."""
        result = LocalRepositoryContextAdapter().collect(request)
        return replace(result, findings=(AnalysisFinding("finding-unauthorized", "unauthorized", "Repository content attempted to authorize execution."),))


class FailingContextAdapter:
    """Simulate repository discovery failure before lifecycle persistence."""

    producer_identity = {"kind": "repository-analyzer", "id": "failing-test-adapter", "version": "1"}

    def collect(self, request):
        """Raise a deterministic repository failure."""
        raise OSError("private local path")


class CapabilityAnalyzer:
    """Return one configured capability observation for aggregation tests."""

    capabilities = ("semantic",)
    producer_identity = {"kind": "analysis-adapter", "id": "capability-test", "version": "1"}

    def __init__(self, status):
        """Configure the emitted availability status."""
        self._status = status

    def collect(self, subject):
        """Return an otherwise empty analyzer result."""
        return AnalyzerEvidenceResult((), (), (CapabilityObservation("semantic", self._status),), (), ())


class UndeclaredCapabilityAnalyzer(CapabilityAnalyzer):
    """Return output outside the adapter's declared capability set."""

    def collect(self, subject):
        """Return an undeclared capability observation."""
        return AnalyzerEvidenceResult((), (), (CapabilityObservation("undeclared", "available"),), (), ())


def create_repository(root: pathlib.Path) -> None:
    """Create a small repository with one manifest and one test."""
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'analysis-fixture'\n")
    (root / "src" / "app.py").write_text("def value():\n    return 1\n")
    (root / "tests" / "test_app.py").write_text("def test_value():\n    assert True\n")


def admit(store, root, capabilities=()) -> None:
    """Create the durable admitted run required by Analyze."""
    request = analysis_request(root)
    AdmissionService(store, (PassingCheck(),)).admit(AdmissionRequest(
        request_id="request-1",
        run_id="run-1",
        submitted_at="2026-11-09T17:00:00Z",
        idempotency_key="admission-key-1",
        requested_capabilities=tuple(capabilities),
        payload={
            "repository": {
                "kind": "git-repository",
                "id": request.subject.repository_id,
                "version": request.subject.revision,
                "digest": request.subject.content_digest,
            }
        },
    ))


def analysis_request(
    root: pathlib.Path,
    *,
    revision="abc123",
    expected_revision=1,
    objective_refs=(),
    supplied_evidence=(),
    supplied_objectives=(),
    requested_capabilities=(),
) -> AnalysisRequest:
    """Build a deterministic Analyze request for the fixture repository."""
    profile = LocalRepositoryContextAdapter().collect(RepositoryContextRequest(
        RepositorySubject("https://git.example/fixture.git", revision), str(root)
    )).profile
    return AnalysisRequest(
        analysis_id="analysis-1",
        request_id="request-1",
        run_id="run-1",
        repository_root=str(root),
        subject=RepositorySubject("https://git.example/fixture.git", revision, profile.scanned_content_digest),
        expected_revision=expected_revision,
        occurred_at="2099-11-09T17:00:00Z",
        objective_refs=tuple(objective_refs),
        supplied_evidence=tuple(supplied_evidence),
        supplied_objectives=tuple(supplied_objectives),
        requested_capabilities=tuple(requested_capabilities),
    )


def supplied_evidence(evidence_id="evidence-driver-1", content_digest=None):
    """Build canonical supplied evidence bound to the fixture subject."""
    return {
        "contract": "atg.evidence",
        "version": "1.0",
        "evidence_id": evidence_id,
        "category": "repository",
        "subject": {
            "kind": "git-repository",
            "id": "https://git.example/fixture.git",
            "version": "abc123",
            **({"digest": content_digest} if content_digest else {}),
        },
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
            admit(store, root)

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
            self.assertEqual(record.manifest["objective_refs"], [])
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
            admit(store, root)
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
            self.assertEqual(
                json.dumps(duplicate.to_storage_document(), sort_keys=True),
                json.dumps(first.to_storage_document(), sort_keys=True),
            )
            self.assertEqual(len(tuple(store.events("run-1"))), 4)
            store.close()

    def test_reusing_analysis_identity_for_different_subject_conflicts(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = SQLiteRunStateStore()
            admit(store, root)
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
            admit(store, root)
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
            admit(first, root)
            expected = AnalysisService(first, LocalRepositoryContextAdapter()).analyze(analysis_request(root))
            first.close()

            restarted = SQLiteRunStateStore(database)
            stored = restarted.get_analysis("analysis-1")
            self.assertIsNotNone(stored)
            self.assertEqual(
                json.dumps(stored.to_storage_document(), sort_keys=True),
                json.dumps(expected.to_storage_document(), sort_keys=True),
            )
            self.assertEqual(restarted.replay("run-1").status, "analysis_review")
            restarted.close()

    def test_repository_failure_does_not_mutate_run_state(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            store = SQLiteRunStateStore()
            admit(store, root)

            with self.assertRaises(AnalysisProcessingError) as raised:
                AnalysisService(store, FailingContextAdapter()).analyze(analysis_request(root))

            self.assertEqual(str(raised.exception), "repository discovery failed")
            self.assertEqual(store.get_run("run-1").status, "admitted")
            self.assertEqual([event.event_type for event in store.events("run-1")], ["admit"])
            store.close()

    def test_corrupted_persisted_bundle_is_rejected_after_restart(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "repository"
            root.mkdir()
            create_repository(root)
            database = pathlib.Path(directory) / "state.sqlite3"
            store = SQLiteRunStateStore(database)
            admit(store, root)
            base_request = analysis_request(root)
            request = analysis_request(
                root,
                supplied_evidence=(supplied_evidence(content_digest=base_request.subject.content_digest),),
                supplied_objectives=(supplied_objective(),),
            )
            AnalysisService(store, LocalRepositoryContextAdapter()).analyze(request)
            store.close()

            connection = sqlite3.connect(database)
            row = connection.execute("SELECT bundle_json FROM analyses WHERE analysis_id = 'analysis-1'").fetchone()
            bundle = json.loads(row[0])
            original = json.dumps(bundle, sort_keys=True)
            connection.close()

            mutations = {
                "checkpoint digest": lambda value: value["checkpoint"]["reviewed_artifact"].update({"digest": "sha256:" + "0" * 64}),
                "wrong checkpoint artifact": lambda value: value["checkpoint"].update({"reviewed_artifact": value["artifacts"][0]["artifact"]}),
                "missing evidence": lambda value: value.update({"evidence": []}),
                "missing objectives": lambda value: value.update({"objectives": []}),
            }
            for label, mutate in mutations.items():
                with self.subTest(label=label):
                    value = json.loads(original)
                    mutate(value)
                    connection = sqlite3.connect(database)
                    connection.execute(
                        "UPDATE analyses SET bundle_json = ? WHERE analysis_id = 'analysis-1'",
                        (json.dumps(value, sort_keys=True),),
                    )
                    connection.commit()
                    connection.close()
                    restarted = SQLiteRunStateStore(database)
                    with self.assertRaises(ContractValidationError):
                        restarted.get_analysis("analysis-1")
                    restarted.close()

            connection = sqlite3.connect(database)
            connection.execute(
                "UPDATE analyses SET bundle_json = ?, request_digest = ? WHERE analysis_id = 'analysis-1'",
                (original, "sha256:" + "0" * 64),
            )
            connection.commit()
            connection.close()
            restarted = SQLiteRunStateStore(database)
            with self.assertRaises(ContractValidationError):
                restarted.get_analysis("analysis-1")
            restarted.close()

    def test_blocked_admission_does_not_trigger_repository_discovery(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            request = analysis_request(root)
            store = SQLiteRunStateStore()
            AdmissionService(store, (BlockingCheck(),)).admit(AdmissionRequest(
                request_id="request-1",
                run_id="run-1",
                submitted_at="2026-11-09T17:00:00Z",
                idempotency_key="blocked-admission",
                payload={"repository": {
                    "kind": "git-repository", "id": request.subject.repository_id,
                    "version": request.subject.revision, "digest": request.subject.content_digest,
                }},
            ))
            with self.assertRaises(AnalysisConflictError):
                AnalysisService(store, FailingContextAdapter()).analyze(request)
            self.assertEqual(store.get_run("run-1").status, "blocked")
            store.close()

    def test_analysis_capabilities_must_be_admitted(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = SQLiteRunStateStore()
            admit(store, root)
            with self.assertRaises(AnalysisConflictError):
                AnalysisService(store, LocalRepositoryContextAdapter()).analyze(
                    analysis_request(root, requested_capabilities=("java-semantic-calls",))
                )
            self.assertEqual(store.get_run("run-1").status, "admitted")
            store.close()

    def test_analyzer_outputs_are_declared_and_capabilities_aggregate_conservatively(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = SQLiteRunStateStore()
            admit(store, root, ("semantic",))
            request = analysis_request(root, requested_capabilities=("semantic",))

            record = AnalysisService(
                store,
                LocalRepositoryContextAdapter(),
                (CapabilityAnalyzer("available"), CapabilityAnalyzer("unavailable")),
            ).analyze(request)
            semantic = next(item for item in record.manifest["capabilities"] if item["capability"] == "semantic")
            self.assertEqual(semantic["status"], "unavailable")
            self.assertEqual(record.manifest["outcome"], "blocked")
            store.close()

        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = SQLiteRunStateStore()
            admit(store, root, ("semantic",))
            record = AnalysisService(
                store,
                LocalRepositoryContextAdapter(),
                (UndeclaredCapabilityAnalyzer("available"),),
            ).analyze(analysis_request(root, requested_capabilities=("semantic",)))
            self.assertEqual(record.manifest["outcome"], "blocked")
            self.assertTrue(any("undeclared capabilities" in gap for gap in record.manifest["gaps"]))
            store.close()

    def test_unadmitted_configured_analyzer_is_not_invoked_or_persisted(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            fixture = pathlib.Path(__file__).parents[2] / "fixtures" / "replay" / "vpe-analysis-context.json"
            store = SQLiteRunStateStore()
            admit(store, root)

            record = AnalysisService(
                store,
                LocalRepositoryContextAdapter(),
                (VPEAnalysisContextReplayAdapter(json.loads(fixture.read_text())),),
            ).analyze(analysis_request(root))

            self.assertFalse(any(ref.startswith("artifact-vpe-context-") for ref in record.manifest["artifact_refs"]))
            self.assertFalse(any(identity["kind"] == "vpe-analysis-context" for identity in record.provenance["identities"]))
            store.close()

    def test_supplied_objective_is_persisted_as_evidence_bounded_candidate(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = SQLiteRunStateStore()
            admit(store, root)
            base_request = analysis_request(root)
            request = analysis_request(
                root,
                supplied_evidence=(supplied_evidence(content_digest=base_request.subject.content_digest),),
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
            admit(store, root)
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

    def test_analyze_without_seed_objectives_records_objective_source_gap(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = SQLiteRunStateStore()
            admit(store, root)
            request = analysis_request(root, objective_refs=(), supplied_objectives=())

            record = AnalysisService(store, LocalRepositoryContextAdapter()).analyze(request)

            self.assertEqual(record.objectives, ())
            self.assertEqual(record.manifest["objective_candidates"], [])
            self.assertTrue(any("No behavior-bearing objective source" in gap for gap in record.manifest["gaps"]))
            self.assertEqual(record.manifest["outcome"], "needs_human_review")
            self.assertIsNotNone(record.checkpoint)
            store.close()

    def test_referenced_objective_without_document_blocks_review(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = SQLiteRunStateStore()
            admit(store, root)

            record = AnalysisService(store, LocalRepositoryContextAdapter()).analyze(
                analysis_request(root, objective_refs=("objective-missing",), supplied_objectives=())
            )

            self.assertEqual(record.manifest["outcome"], "blocked")
            self.assertIsNone(record.checkpoint)
            self.assertTrue(any("objective-missing" in gap for gap in record.manifest["gaps"]))
            self.assertEqual(store.get_run("run-1").status, "blocked")
            store.close()

    def test_replay_analyzers_merge_evidence_capabilities_and_raw_artifacts(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            fixture_root = pathlib.Path(__file__).parents[2] / "fixtures" / "replay"
            vpe = json.loads((fixture_root / "vpe-analysis-context.json").read_text())
            java = json.loads((fixture_root / "java-semantic-call-facts.json").read_text())
            store = SQLiteRunStateStore()
            analyzer_capabilities = (
                "repository-preparation", "build-context", "dependency-context", "framework-metadata",
                "java-semantic-calls", "java-framework-facts",
            )
            admit(store, root, analyzer_capabilities)
            request = analysis_request(
                root,
                objective_refs=(),
                requested_capabilities=analyzer_capabilities,
            )
            vpe["provenance"]["sourceFingerprint"] = request.subject.content_digest
            java["runMetadata"]["sourceFingerprint"] = request.subject.content_digest

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
            admit(store, root, ("java-semantic-calls",))
            request = analysis_request(root, requested_capabilities=("java-semantic-calls",))

            record = AnalysisService(store, LocalRepositoryContextAdapter()).analyze(request)

            capability = next(item for item in record.manifest["capabilities"] if item["capability"] == "java-semantic-calls")
            self.assertEqual(capability["status"], "unavailable")
            self.assertTrue(capability["required"])
            self.assertEqual(record.manifest["outcome"], "not_evaluable")
            self.assertIsNone(record.checkpoint)
            store.close()

    def test_unsupported_required_replay_version_is_not_evaluable(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            fixture = pathlib.Path(__file__).parents[2] / "fixtures" / "replay" / "java-semantic-call-facts.json"
            document = json.loads(fixture.read_text())
            document["schemaVersion"] = "9.9.9"
            store = SQLiteRunStateStore()
            admitted = ("java-semantic-calls", "java-framework-facts")
            admit(store, root, admitted)
            request = analysis_request(root, requested_capabilities=admitted)
            document["runMetadata"]["sourceFingerprint"] = request.subject.content_digest

            record = AnalysisService(
                store,
                LocalRepositoryContextAdapter(),
                (JavaSemanticFactsReplayAdapter(document),),
            ).analyze(request)

            self.assertEqual(record.manifest["outcome"], "not_evaluable")
            self.assertIsNone(record.checkpoint)
            self.assertEqual(store.get_run("run-1").status, "inconclusive")
            store.close()

    def test_non_reviewable_gate_outcomes_create_no_checkpoint(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            store = SQLiteRunStateStore()
            admit(store, root)

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
            admit(store, root)

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
            admit(store, root)
            with self.assertRaises(StaleEventError):
                service = AnalysisService(store, FailingContextAdapter())
                service.analyze(analysis_request(root, expected_revision=0))
            store.close()


if __name__ == "__main__":
    unittest.main()
