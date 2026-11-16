import json
import pathlib
import sys
import unittest
from tempfile import TemporaryDirectory

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.adapters.persistence.sqlite_run_state import SQLiteRunStateStore
from atg.adapters.repository.local import LocalRepositoryContextAdapter
from atg.application.analysis import AnalysisService
from atg.application.generation import (
    GenerationConflictError,
    GenerationRequest,
    GenerationService,
)
from atg.application.planning import PlanningRequest, PlanningService
from atg.domain.schema_validation import ContractValidationError
from atg.domain.run_state import RunEvent
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
    patch = f"diff --git a/{path} b/{path}\n--- /dev/null\n+++ b/{path}\n+{content.replace(chr(10), chr(10) + '+')}"
    return PatchCandidate(patch, {path: content})


def request(state, generation_id="generation-1", first=None, repairs=(), key="generation-key"):
    first = first or candidate()
    return GenerationRequest(
        generation_id=generation_id,
        plan_id="plan-1",
        run_id="run-1",
        expected_revision=state.revision,
        created_at="2099-11-09T17:00:04Z",
        idempotency_key=key,
        patch_text=first.patch_text,
        materialized_files=first.materialized_files,
        base=BASE,
        candidate=CANDIDATE,
        model=MODEL,
        tool=TOOL,
        worker=WORKER,
        repair_candidates=tuple(repairs),
    )


class GenerationServiceTests(unittest.TestCase):
    def test_generation_persists_exact_artifacts_and_events(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            record = GenerationService(store).generate(request(store.get_run("run-1")))
            self.assertEqual(record.manifest["status"], "verified")
            self.assertEqual(len(record.artifacts), 2)
            self.assertEqual(record.artifacts[0]["content"], request(store.get_run("run-1")).patch_text)
            persisted = store.get_generation("generation-1")
            self.assertEqual(persisted.manifest, record.manifest)
            state = store.get_run("run-1")
            self.assertEqual(state.completed_generation_id, "generation-1")
            self.assertFalse(state.generation_in_progress)
            store.close()

    def test_generation_rejects_unapproved_path_without_completion(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            invalid = candidate(path="src/main/java/App.java")
            record = GenerationService(store).generate(request(store.get_run("run-1"), first=invalid))
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
            record = GenerationService(store).generate(request(
                store.get_run("run-1"), generation_id="generation-mismatch", first=mismatch
            ))
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
                record = GenerationService(store).generate(request(
                    store.get_run("run-1"), generation_id, candidate(content)
                ))
                self.assertEqual(record.manifest["status"], "rejected")
                store.close()

    def test_generation_repairs_are_bounded_and_attributed(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            bad = candidate("def test_target_handler():\n    # target-handler\n    value = 1\n")
            record = GenerationService(store).generate(request(
                store.get_run("run-1"), first=bad, repairs=(candidate(),)
            ))
            self.assertEqual(record.manifest["status"], "verified")
            self.assertEqual(len(record.attempts), 2)
            self.assertEqual(record.attempts[0]["model"], MODEL)
            self.assertEqual(record.attempts[0]["tool"], TOOL)
            self.assertEqual(record.attempts[0]["worker"], WORKER)
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
                        return bad
                    count = store._connection.execute(
                        "SELECT COUNT(*) AS count FROM generation_attempts WHERE generation_id = ?",
                        (generation_request.generation_id,),
                    ).fetchone()["count"]
                    self.assertEqual(count, 1)
                    return good if attempt == 2 else None

                def assertEqual(self, left, right):
                    if left != right:
                        raise AssertionError(f"{left!r} != {right!r}")

            record = GenerationService(store, generator=CheckingGenerator()).generate(
                request(store.get_run("run-1"), first=bad, repairs=(good,))
            )
            self.assertEqual(record.manifest["status"], "verified")
            store.close()

    def test_three_failed_attempts_are_inconclusive(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            bad = candidate("def test_target_handler():\n    # target-handler\n    value = 1\n")
            record = GenerationService(store).generate(request(
                store.get_run("run-1"), first=bad, repairs=(bad, bad)
            ))
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
            service = GenerationService(store)
            first = service.generate(request(state))
            duplicate = service.generate(request(state))
            self.assertTrue(duplicate.duplicate)
            self.assertEqual(first.manifest, duplicate.manifest)
            changed = candidate("def test_target_handler():\n    # target-handler\n    assert 3 == 3\n")
            with self.assertRaises(GenerationConflictError):
                service.generate(request(state, first=changed))
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
                GenerationService(store).generate(request(store.get_run("run-1")))
            store.close()

    def test_persisted_generation_tampering_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "repo"
            root.mkdir()
            create_repository(root)
            database = pathlib.Path(directory) / "generation.sqlite3"
            store, _ = prepare_coding_store(root, database)
            GenerationService(store).generate(request(store.get_run("run-1")))
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
            GenerationService(store).generate(request(store.get_run("run-1")))
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
