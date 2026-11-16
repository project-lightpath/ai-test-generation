import json
import ast
import pathlib
import sys
import unittest
from dataclasses import asdict, replace
from tempfile import TemporaryDirectory

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.adapters.generation.deterministic import FilesystemPatchMaterializer
from atg.adapters.persistence.sqlite_run_state import SQLiteRunStateStore
from atg.adapters.repository.local import LocalRepositoryContextAdapter
from atg.application.analysis import AnalysisService
from atg.application.generation import (
    GenerationAdapterUnavailableError,
    GenerationConflictError,
    GenerationRecord,
    GenerationRequest,
    GenerationService,
    validate_generation_record,
)
from atg.application.planning import PlanningRequest, PlanningService
from atg.domain.schema_validation import ContractValidationError
from atg.domain.generation import (
    canonical_patch,
    compute_materialized_digest,
    compute_patch_digest,
)
from atg.ports.generation import (
    GeneratedCandidate,
    GenerationProvenance,
    ValidationResult,
)
from atg.domain.run_state import InvalidEventError, RunEvent, event_fingerprint
from atg.ports.generation import PatchCandidate
from tests.unit.test_analysis import (
    analysis_request,
    admit,
    create_repository,
    supplied_evidence,
    supplied_objective,
)


BASE = {"kind": "git-tree", "id": "base-tree", "version": "abc123"}
CANDIDATE = {"kind": "git-tree", "id": "candidate-tree", "version": "def456"}
MODEL = {"kind": "model", "id": "model-alias", "version": "1"}
TOOL = {"kind": "generation-tool", "id": "bounded-generator", "version": "1"}
WORKER = {"kind": "worker", "id": "worker-1", "version": "1"}
ENDPOINT = {"kind": "model-endpoint", "id": "local-fake-endpoint", "version": "1"}
FAKE_PROVENANCE = GenerationProvenance(
    model=MODEL,
    model_endpoint=ENDPOINT,
    tool=TOOL,
    worker=WORKER,
    prompt_digest="sha256:" + "1" * 64,
    config_digest="sha256:" + "2" * 64,
    invocation_digest="sha256:" + "3" * 64,
)


class FixtureValidationAdapter:
    """Test-only profile that supplies deterministic successful tool results."""

    def validate(self, patch_candidate):
        content = "\n".join(patch_candidate.materialized_files.values())
        syntax_ok = "SYNTAX_ERROR" not in content
        try:
            ast.parse(content)
        except SyntaxError:
            syntax_ok = False
        return ValidationResult(
            syntax_ok=syntax_ok,
            format_ok=not any(line != line.rstrip() or "\t" in line for line in content.splitlines()),
            compile_ok="COMPILE_ERROR" not in content,
            collected=content.count("def test_") + content.count("@Test"),
        )


class FixtureGenerationAdapter:
    """Return pre-recorded adapter outputs independently of HTTP input."""

    def __init__(self, outputs):
        self.outputs = tuple(outputs)

    def generate(self, request, attempt, feedback):
        return self.outputs[attempt - 1] if attempt <= len(self.outputs) else None


def generated(patch_candidate):
    return GeneratedCandidate(patch_candidate, FAKE_PROVENANCE)


def generation_service(store, outputs=None, *, validator=None, generator=None):
    return GenerationService(
        store,
        generator=generator or FixtureGenerationAdapter(outputs or (generated(candidate()),)),
        validator=validator or FixtureValidationAdapter(),
    )


def prepare_coding_store(root: pathlib.Path, database=":memory:"):
    store = SQLiteRunStateStore(database)
    base_request = analysis_request(root)
    evidence = supplied_evidence(content_digest=base_request.subject.content_digest)
    request = analysis_request(
        root,
        objective_refs=("objective-1",),
        supplied_evidence=(evidence,),
        supplied_objectives=(supplied_objective(),),
    )
    admit(store, root)
    analysis = AnalysisService(store, LocalRepositoryContextAdapter()).analyze(request)
    state = store.get_run("run-1")
    store.apply(RunEvent(
        "analysis-decision", "run-1", "decision_recorded", state.revision,
        "2099-11-09T17:00:01Z", checkpoint_id=analysis.checkpoint["checkpoint_id"],
        decision="approved", decision_id="decision-analysis", actor_id="reviewer-1",
        actor_role="owner", authorization_ref="auth-analysis",
    ))
    state = store.get_run("run-1")
    plan = PlanningService(store).plan(PlanningRequest(
        "plan-1", "analysis-1", "run-1", state.revision, "2099-11-09T17:00:02Z"
    ))
    store.apply(RunEvent(
        "plan-decision", "run-1", "decision_recorded", 7,
        "2099-11-09T17:00:03Z", checkpoint_id="checkpoint-plan-1",
        decision="approved", decision_id="decision-plan", actor_id="reviewer-1",
        actor_role="owner", authorization_ref="auth-plan",
    ))
    return store, plan


def candidate(content=None, path="tests/test_generated.py"):
    content = content or "def test_target_handler():\n    # target-handler\n    actual = target_handler()\n    assert actual == 2\n"
    files = {path: content}
    return PatchCandidate(canonical_patch({}, files), files)


def request(state, generation_id="generation-1", key="generation-key", endpoint=ENDPOINT):
    return GenerationRequest(
        generation_id=generation_id,
        plan_id="plan-1",
        run_id="run-1",
        expected_revision=state.revision,
        created_at="2099-11-09T17:00:04Z",
        idempotency_key=key,
        base=BASE,
        candidate=CANDIDATE,
        model_endpoint=endpoint,
    )


class GenerationServiceTests(unittest.TestCase):
    def test_empty_subject_version_is_rejected_before_generation_starts(self):
        state = type("State", (), {"revision": 0})()
        with self.assertRaisesRegex(ValueError, "base.version"):
            replace(request(state), base={**BASE, "version": ""})

    def test_generation_persists_exact_artifacts_and_events(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, plan = prepare_coding_store(root)
            req = request(store.get_run("run-1"))
            record = generation_service(store, (generated(candidate()),)).generate(req)
            self.assertEqual(record.manifest["status"], "verified")
            self.assertEqual(len(record.artifacts), 3)
            self.assertEqual(record.artifacts[0]["content"], candidate().patch_text)
            handoff = record.manifest["materialization_handoff"]
            self.assertEqual(handoff["status"], "ready")
            self.assertEqual(handoff["run_id"], record.manifest["run_id"])
            self.assertEqual(handoff["allowed_paths"], plan.manifest["allowed_paths"])
            self.assertEqual(handoff["changed_files"], ["tests/test_generated.py"])
            self.assertEqual(handoff["patch_digest"], record.manifest["patch_digest"])
            self.assertEqual(
                handoff["materialized_test_digest"],
                record.manifest["materialized_test_digest"],
            )
            self.assertEqual(
                handoff["patch_artifact_id"], record.attempts[-1]["patch_artifact_id"]
            )
            self.assertEqual(
                handoff["materialized_artifact_id"],
                record.attempts[-1]["materialized_artifact_id"],
            )
            self.assertEqual(handoff["base_artifact_id"], record.attempts[-1]["base_artifact_id"])
            self.assertEqual(
                [item["subject"] for item in handoff["subjects"]], [BASE, CANDIDATE]
            )
            persisted = store.get_generation("generation-1")
            self.assertEqual(persisted.manifest, record.manifest)
            state = store.get_run("run-1")
            self.assertEqual(state.completed_generation_id, "generation-1")
            self.assertFalse(state.generation_in_progress)
            store.close()

    def test_persisted_preimage_reconstructs_candidate_for_materialization(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, plan = prepare_coding_store(root)
            path = "tests/test_existing.py"
            before = "def test_target_handler():\n    target_handler()\n    assert value == 0\n"
            after = "def test_target_handler():\n    target_handler()\n    assert value == 1\n"
            original = PatchCandidate(
                canonical_patch({path: before}, {path: after}),
                {path: after},
                {path: before},
            )
            record = generation_service(store, (generated(original),)).generate(
                request(store.get_run("run-1"))
            )
            restored = store.get_generation(record.manifest["generation_id"]).candidate_for_attempt(1)
            self.assertEqual(restored, original)

            workspace = root / "workspace"
            target = workspace / path
            target.parent.mkdir(parents=True)
            target.write_text(before)
            FilesystemPatchMaterializer().materialize(
                restored,
                str(workspace),
                CANDIDATE,
                tuple(plan.manifest["allowed_paths"]),
                compute_patch_digest(restored.patch_text),
                compute_materialized_digest(restored.materialized_files),
            )
            self.assertEqual(target.read_text(), after)
            store.close()

    def test_materialization_handoff_tampering_fails_integrity_validation(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, plan = prepare_coding_store(root)
            record = generation_service(store, (generated(candidate()),)).generate(
                request(store.get_run("run-1"))
            )
            manifest = json.loads(json.dumps(record.manifest))
            manifest["changed_files"] = ["tests/test_never_generated.py"]
            manifest["materialization_handoff"]["changed_files"] = [
                "tests/test_never_generated.py"
            ]
            tampered = GenerationRecord(
                record.request_digest, manifest, record.artifacts, record.attempts
            )
            with self.assertRaises(ContractValidationError):
                validate_generation_record(tampered, plan.manifest)
            store.close()

    def test_verified_manifest_requires_passing_final_attempt_evidence(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, plan = prepare_coding_store(root)
            record = generation_service(store, (generated(candidate()),)).generate(
                request(store.get_run("run-1"))
            )
            manifest = json.loads(json.dumps(record.manifest))
            manifest["attempts"][-1]["status"] = "rejected"
            manifest["attempts"][-1]["checks"][-1]["status"] = "fail"
            tampered = GenerationRecord(
                record.request_digest, manifest, record.artifacts, record.attempts
            )
            with self.assertRaises(ContractValidationError):
                validate_generation_record(tampered, plan.manifest)
            store.close()

    def test_generation_rejects_unapproved_path_without_completion(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            invalid = candidate(path="src/main/java/App.java")
            record = generation_service(store, (generated(invalid),)).generate(request(store.get_run("run-1")))
            self.assertEqual(record.manifest["status"], "rejected")
            self.assertIsNone(store.get_run("run-1").completed_generation_id)
            store.close()

    def test_generation_rejects_patch_materialization_path_mismatch(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            valid = candidate()
            mismatch = PatchCandidate(
                valid.patch_text,
                {"tests/test_different.py": next(iter(valid.materialized_files.values()))},
            )
            record = generation_service(store, (generated(mismatch),)).generate(
                request(store.get_run("run-1"), generation_id="generation-mismatch")
            )
            self.assertEqual(record.manifest["status"], "rejected")
            self.assertEqual(record.attempts[0]["checks"][0]["status"], "fail")
            store.close()

    def test_generation_uses_deterministic_collection_and_assertion_gates(self):
        cases = {
            "no-assertion": "def test_target_handler():\n    # target-handler\n    value = 1\n",
            "no-collection": "# target-handler\nassert 2 == 2\n",
            "weakened": "def test_target_handler():\n    # target-handler\n    assert True\n",
            "wrong-target": "def test_other():\n    assert 2 == 2\n",
            "syntax": "def test_target_handler(:\n    # target-handler\n    assert value == 1\n",
            "format": "def test_target_handler(): \n    # target-handler\n    assert value == 1\n",
            "compile": "def test_target_handler():\n    # target-handler COMPILE_ERROR\n    assert value == 1\n",
        }
        for generation_id, content in cases.items():
            with self.subTest(generation_id=generation_id), TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                create_repository(root)
                store, _ = prepare_coding_store(root)
                record = generation_service(store, (generated(candidate(content)),)).generate(
                    request(store.get_run("run-1"), generation_id)
                )
                self.assertEqual(record.manifest["status"], "rejected")
                store.close()

    def test_default_validator_does_not_claim_java_compile_or_collection(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            path = "tests/GeneratedTest.java"
            content = "class GeneratedTest { @Test void testBehavior() { subject.target_handler(); assertTrue(subject.isValid()); } }\n"
            java_candidate = PatchCandidate(canonical_patch({}, {path: content}), {path: content})
            result = GenerationService(
                store, generator=FixtureGenerationAdapter((generated(java_candidate),))
            ).generate(request(store.get_run("run-1")))
            self.assertEqual(result.manifest["status"], "inconclusive")
            checks = {item["name"]: item["status"] for item in result.attempts[0]["checks"]}
            self.assertEqual(checks["compile"], "not_supported")
            self.assertEqual(checks["collection"], "not_supported")
            limitation_codes = {item["code"] for item in result.manifest["limitations"]}
            self.assertIn("unsupported-compile", limitation_codes)
            self.assertIn("unsupported-collection", limitation_codes)
            self.assertIn("execution-not-performed", limitation_codes)
            store.close()

    def test_unconfigured_service_does_not_start_generation(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            state = store.get_run("run-1")
            with self.assertRaises(GenerationAdapterUnavailableError):
                GenerationService(store).generate(request(state))
            self.assertEqual(store.get_run("run-1").revision, state.revision)
            self.assertFalse(store.get_run("run-1").generation_in_progress)
            store.close()

    def test_adapter_endpoint_must_match_intent_and_can_resume_after_correction(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            req = request(store.get_run("run-1"))
            mismatched = GenerationProvenance(
                model=MODEL,
                model_endpoint={"kind": "model-endpoint", "id": "wrong-endpoint", "version": "1"},
                tool=TOOL,
                worker=WORKER,
                prompt_digest=FAKE_PROVENANCE.prompt_digest,
                config_digest=FAKE_PROVENANCE.config_digest,
                invocation_digest=FAKE_PROVENANCE.invocation_digest,
            )
            with self.assertRaises(GenerationConflictError):
                generation_service(
                    store,
                    (GeneratedCandidate(candidate(), mismatched),),
                ).generate(req)
            self.assertTrue(store.get_run("run-1").generation_in_progress)
            record = generation_service(store, (generated(candidate()),)).generate(req)
            self.assertEqual(record.manifest["status"], "verified")
            store.close()

    def test_resume_rejects_conflicting_request_before_adapter_or_attempt(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            req = request(store.get_run("run-1"))
            mismatched = GenerationProvenance(
                model=MODEL,
                model_endpoint={"kind": "model-endpoint", "id": "wrong", "version": "1"},
                tool=TOOL,
                worker=WORKER,
                prompt_digest=FAKE_PROVENANCE.prompt_digest,
                config_digest=FAKE_PROVENANCE.config_digest,
                invocation_digest=FAKE_PROVENANCE.invocation_digest,
            )
            with self.assertRaises(GenerationConflictError):
                generation_service(store, (GeneratedCandidate(candidate(), mismatched),)).generate(req)
            self.assertTrue(store.get_run("run-1").generation_in_progress)
            self.assertEqual(store.get_generation_attempts(req.generation_id, req.digest()), ((), ()))

            changed = replace(req, created_at="2099-11-09T17:00:05Z")
            calls = []

            class CountingGenerator:
                def generate(self, generation_request, attempt, feedback):
                    calls.append(attempt)
                    return generated(candidate())

            with self.assertRaisesRegex(GenerationConflictError, "different request inputs"):
                generation_service(store, generator=CountingGenerator()).generate(changed)
            self.assertEqual(calls, [])
            self.assertEqual(store.get_generation_attempts(req.generation_id, req.digest()), ((), ()))

            recovered = generation_service(store, (generated(candidate()),)).generate(req)
            self.assertEqual(recovered.manifest["status"], "verified")
            store.close()

    def test_legacy_generation_start_event_remains_readable_and_replayable(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            state = store.get_run("run-1")
            legacy_event = RunEvent(
                event_id="legacy-generation-started",
                run_id="run-1",
                event_type="generation_started",
                expected_revision=state.revision,
                occurred_at="2099-11-09T17:00:05Z",
                generation_id="legacy-generation",
            )
            next_state = state.apply(legacy_event).state
            legacy_payload = asdict(legacy_event)
            legacy_payload.pop("generation_request_digest")
            payload_json = json.dumps(legacy_payload, sort_keys=True, separators=(",", ":"))
            with store._lock:
                store._connection.execute("BEGIN IMMEDIATE")
                store._connection.execute(
                    "INSERT INTO run_events(event_id, run_id, applied_revision, payload_digest, payload_json, occurred_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        legacy_event.event_id,
                        legacy_event.run_id,
                        next_state.revision,
                        event_fingerprint(legacy_event),
                        payload_json,
                        legacy_event.occurred_at,
                    ),
                )
                store._connection.execute(
                    "UPDATE runs SET revision = ?, state_json = ? WHERE run_id = ?",
                    (next_state.revision, json.dumps(next_state.to_document(), sort_keys=True), "run-1"),
                )
                store._connection.execute("COMMIT")

            loaded = tuple(store.events("run-1"))[-1]
            self.assertIsNone(loaded.generation_request_digest)
            self.assertEqual(store.replay("run-1"), next_state)
            with self.assertRaisesRegex(InvalidEventError, "new generation_started events"):
                store.apply(
                    RunEvent(
                        event_id="new-generation-started-without-digest",
                        run_id="run-1",
                        event_type="generation_started",
                        expected_revision=next_state.revision,
                        occurred_at="2099-11-09T17:00:06Z",
                        generation_id="new-generation",
                    )
                )
            store.close()

    def test_invalid_provider_identity_is_not_persisted_and_can_be_corrected(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            req = request(store.get_run("run-1"))
            bad_provenance = GenerationProvenance(
                model={**MODEL, "version": ""},
                model_endpoint=ENDPOINT,
                tool=TOOL,
                worker=WORKER,
                prompt_digest=FAKE_PROVENANCE.prompt_digest,
                config_digest=FAKE_PROVENANCE.config_digest,
                invocation_digest=FAKE_PROVENANCE.invocation_digest,
            )
            with self.assertRaisesRegex(ValueError, "model.version"):
                generation_service(store, (GeneratedCandidate(candidate(), bad_provenance),)).generate(req)
            self.assertEqual(store.get_generation_attempts(req.generation_id, req.digest()), ((), ()))

            record = generation_service(store, (generated(candidate()),)).generate(req)
            self.assertEqual(record.manifest["status"], "verified")
            store.close()

    def test_missing_initial_candidate_remains_resumable(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            req = request(store.get_run("run-1"))

            class EmptyGenerator:
                def generate(self, request, attempt, feedback):
                    return None

            with self.assertRaises(GenerationAdapterUnavailableError):
                generation_service(store, generator=EmptyGenerator()).generate(req)
            self.assertTrue(store.get_run("run-1").generation_in_progress)
            record = generation_service(store, (generated(candidate()),)).generate(req)
            self.assertEqual(record.manifest["status"], "verified")
            store.close()

    def test_generation_repairs_are_bounded_and_attributed(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            bad = candidate("def test_target_handler():\n    # target-handler\n    value = 1\n")
            record = generation_service(store, (generated(bad), generated(candidate()))).generate(
                request(store.get_run("run-1"))
            )
            self.assertEqual(record.manifest["status"], "verified")
            self.assertEqual(len(record.attempts), 2)
            self.assertEqual(record.attempts[0]["model"], MODEL)
            self.assertEqual(record.manifest["model_endpoint"], ENDPOINT)
            self.assertEqual(record.attempts[0]["tool"], TOOL)
            self.assertEqual(record.attempts[0]["worker"], WORKER)
            self.assertEqual(record.attempts[0]["model_endpoint"], ENDPOINT)
            self.assertEqual(record.attempts[0]["prompt_digest"], FAKE_PROVENANCE.prompt_digest)
            self.assertEqual(record.attempts[0]["config_digest"], FAKE_PROVENANCE.config_digest)
            self.assertEqual(record.attempts[0]["invocation_digest"], FAKE_PROVENANCE.invocation_digest)
            store.close()

    def test_attempt_is_persisted_before_next_repair(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            bad = candidate("def test_target_handler():\n    # target-handler\n    value = 1\n")
            good = candidate()

            class CheckingGenerator:
                def generate(self, generation_request, attempt, feedback):
                    if attempt == 1:
                        return generated(bad)
                    count = store._connection.execute(
                        "SELECT COUNT(*) AS count FROM generation_attempts WHERE generation_id = ?",
                        (generation_request.generation_id,),
                    ).fetchone()["count"]
                    self.assertEqual(count, 1)
                    return generated(good) if attempt == 2 else None

                def assertEqual(self, left, right):
                    if left != right:
                        raise AssertionError(f"{left!r} != {right!r}")

            record = generation_service(store, generator=CheckingGenerator()).generate(
                request(store.get_run("run-1"))
            )
            self.assertEqual(record.manifest["status"], "verified")
            store.close()

    def test_process_restart_resumes_after_persisted_attempt(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            bad = candidate("def test_target_handler():\n    # target-handler\n    value = 1\n")
            good = candidate()
            req = request(store.get_run("run-1"))

            class FailingAttemptStore:
                def __getattr__(self, name):
                    return getattr(store, name)

                def record_generation_attempt(self, *args, **kwargs):
                    store.record_generation_attempt(*args, **kwargs)
                    raise RuntimeError("simulated process interruption")

            with self.assertRaisesRegex(RuntimeError, "interruption"):
                generation_service(
                    FailingAttemptStore(), (generated(bad), generated(good))
                ).generate(req)
            self.assertTrue(store.get_run("run-1").generation_in_progress)
            self.assertEqual(len(store.get_generation_attempts("generation-1", req.digest())[0]), 1)

            recovered = generation_service(store, (generated(bad), generated(good))).generate(req)
            self.assertEqual(recovered.manifest["status"], "verified")
            self.assertEqual(len(recovered.attempts), 2)
            self.assertEqual(store.replay("run-1").completed_generation_id, "generation-1")
            store.close()

    def test_three_failed_attempts_are_inconclusive(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            bad = candidate("def test_target_handler():\n    # target-handler\n    value = 1\n")
            record = generation_service(store, (generated(bad), generated(bad), generated(bad))).generate(
                request(store.get_run("run-1"))
            )
            self.assertEqual(record.manifest["status"], "inconclusive")
            self.assertEqual(len(record.attempts), 3)
            self.assertIsNone(store.get_run("run-1").completed_generation_id)
            store.close()

    def test_generation_idempotency_binds_patch_and_identities(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            state = store.get_run("run-1")
            service = generation_service(store, (generated(candidate()),))
            first = service.generate(request(state))
            duplicate = service.generate(request(state))
            self.assertTrue(duplicate.duplicate)
            self.assertEqual(first.manifest, duplicate.manifest)
            with self.assertRaises(GenerationConflictError):
                service.generate(request(state, endpoint={"kind": "model-endpoint", "id": "different", "version": "1"}))
            store.close()

    def test_generation_requires_exact_active_plan_digest(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            row = store._connection.execute("SELECT state_json FROM runs WHERE run_id = 'run-1'").fetchone()
            document = json.loads(row["state_json"])
            document["active_plan_artifact_digest"] = "sha256:" + "f" * 64
            store._connection.execute(
                "UPDATE runs SET state_json = ? WHERE run_id = 'run-1'",
                (json.dumps(document, sort_keys=True),),
            )
            with self.assertRaises(GenerationConflictError):
                generation_service(store, (generated(candidate()),)).generate(request(store.get_run("run-1")))
            store.close()

    def test_persisted_generation_tampering_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "repo"
            root.mkdir()
            create_repository(root)
            database = pathlib.Path(directory) / "generation.sqlite3"
            store, _ = prepare_coding_store(root, database)
            generation_service(store, (generated(candidate()),)).generate(request(store.get_run("run-1")))
            bundle = json.loads(store._connection.execute(
                "SELECT bundle_json FROM generations WHERE generation_id = 'generation-1'"
            ).fetchone()["bundle_json"])
            bundle["artifacts"][0]["content"] += "tampered"
            store._connection.execute(
                "UPDATE generations SET bundle_json = ? WHERE generation_id = 'generation-1'",
                (json.dumps(bundle, sort_keys=True),),
            )
            with self.assertRaises(ContractValidationError):
                store.get_generation("generation-1")
            store.close()

    def test_persisted_attempt_tampering_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            generation_service(store, (generated(candidate()),)).generate(request(store.get_run("run-1")))
            row = store._connection.execute(
                "SELECT document_json FROM generation_attempts WHERE generation_id = 'generation-1' AND attempt = 1"
            ).fetchone()
            attempt = json.loads(row["document_json"])
            attempt["output_digest"] = "sha256:" + "f" * 64
            store._connection.execute(
                "UPDATE generation_attempts SET document_json = ? WHERE generation_id = 'generation-1' AND attempt = 1",
                (json.dumps(attempt, sort_keys=True),),
            )
            with self.assertRaises(ContractValidationError):
                store.get_generation("generation-1")
            store.close()


if __name__ == "__main__":
    unittest.main()
