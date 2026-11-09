import json
import pathlib
import sys
from tempfile import TemporaryDirectory
import unittest

from jsonschema import Draft202012Validator, FormatChecker, RefResolver

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.adapters.persistence.sqlite_run_state import SQLiteRunStateStore  # noqa: E402
from atg.adapters.repository.local import LocalRepositoryContextAdapter  # noqa: E402
from atg.application.admission import AdmissionRequest, AdmissionService, CheckResult  # noqa: E402
from atg.application.analysis import AnalysisConflictError, AnalysisRequest, AnalysisService  # noqa: E402
from atg.domain.contract_integrity import validate_documents  # noqa: E402
from atg.domain.repository_analysis import RepositorySubject  # noqa: E402
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


def analysis_request(root: pathlib.Path, *, revision="abc123", expected_revision=1) -> AnalysisRequest:
    """Build a deterministic Analyze request for the fixture repository."""
    return AnalysisRequest(
        analysis_id="analysis-1",
        request_id="request-1",
        run_id="run-1",
        repository_root=str(root),
        subject=RepositorySubject("https://git.example/fixture.git", revision),
        expected_revision=expected_revision,
        occurred_at="2099-11-09T17:00:00Z",
        objective_refs=("objective-1",),
    )


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
