import pathlib
import sys
import unittest
from tempfile import TemporaryDirectory

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.adapters.persistence.sqlite_run_state import SQLiteRunStateStore
from atg.application.generation import GenerationConflictError, GenerationRequest, GenerationService
from atg.application.planning import PlanningRequest, PlanningService
from atg.domain.run_state import RunEvent
from tests.unit.test_analysis import analysis_request, admit, create_repository, supplied_evidence, supplied_objective
from atg.adapters.repository.local import LocalRepositoryContextAdapter
from atg.application.analysis import AnalysisService


def prepare_coding_store(root: pathlib.Path):
    store = SQLiteRunStateStore()
    base_request = analysis_request(root)
    evidence = supplied_evidence(content_digest=base_request.subject.content_digest)
    request = analysis_request(root, objective_refs=("objective-1",), supplied_evidence=(evidence,), supplied_objectives=(supplied_objective(),))
    admit(store, root)
    analysis = AnalysisService(store, LocalRepositoryContextAdapter()).analyze(request)
    state = store.get_run("run-1")
    store.apply(RunEvent("analysis-decision", "run-1", "decision_recorded", state.revision, "2099-11-09T17:00:01Z", checkpoint_id=analysis.checkpoint["checkpoint_id"], decision="approved", decision_id="decision-analysis", actor_id="reviewer-1", actor_role="owner", authorization_ref="auth-analysis"))
    state = store.get_run("run-1")
    plan = PlanningService(store).plan(PlanningRequest("plan-1", "analysis-1", "run-1", state.revision, "2099-11-09T17:00:02Z"))
    store.apply(RunEvent("plan-decision", "run-1", "decision_recorded", 7, "2099-11-09T17:00:03Z", checkpoint_id="checkpoint-plan-1", decision="approved", decision_id="decision-plan", actor_id="reviewer-1", actor_role="owner", authorization_ref="auth-plan"))
    return store, plan


class GenerationServiceTests(unittest.TestCase):
    def test_generation_persists_and_emits_events(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, plan = prepare_coding_store(root)
            state = store.get_run("run-1")
            patch = "diff --git a/src/test/java/AppTest.java\n+assertEquals(objective-1, 1)\n"
            req = GenerationRequest("gen-1", "plan-1", "run-1", state.revision, "2099-11-09T17:00:04Z", patch, ["src/test/java/AppTest.java"], collected=1)
            bundle = GenerationService(store).generate(req)
            self.assertEqual(bundle["patch_digest"], req.patch_digest())
            self.assertEqual(bundle["status"], "verified")
            self.assertIsNotNone(store.get_generation("gen-1"))
            self.assertIsNotNone(store.get_run("run-1").completed_generation_id)
            self.assertFalse(store.get_run("run-1").generation_in_progress)
            store.close()

    def test_generation_rejects_unapproved_path(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            state = store.get_run("run-1")
            patch = "change"
            with self.assertRaises(ValueError):
                GenerationRequest("gen-2", "plan-1", "run-1", state.revision, "2099-11-09T17:00:04Z", patch, ["src/main/java/App.java"], collected=1)

    def test_generation_rejects_no_assertion(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            state = store.get_run("run-1")
            patch = "no asserts here"
            req = GenerationRequest("gen-3", "plan-1", "run-1", state.revision, "2099-11-09T17:00:04Z", patch, ["src/test/java/AppTest.java"], collected=1)
            with self.assertRaises(GenerationConflictError):
                GenerationService(store).generate(req)
            store.close()

    def test_generation_rejects_no_collection(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            state = store.get_run("run-1")
            patch = "assertEquals(objective-1, 1)"
            req = GenerationRequest("gen-4", "plan-1", "run-1", state.revision, "2099-11-09T17:00:04Z", patch, ["src/test/java/AppTest.java"], collected=0)
            with self.assertRaises(GenerationConflictError):
                GenerationService(store).generate(req)
            store.close()

    def test_generation_duplicate_is_idempotent(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            state = store.get_run("run-1")
            patch = "assertEquals(objective-1, 1)"
            req = GenerationRequest("gen-5", "plan-1", "run-1", state.revision, "2099-11-09T17:00:04Z", patch, ["src/test/java/AppTest.java"], collected=1)
            svc = GenerationService(store)
            first = svc.generate(req)
            second = svc.generate(req)
            self.assertEqual(first, second)
            store.close()

    def test_generation_rejects_unapproved_plan(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = SQLiteRunStateStore()
            admit(store, root)
            state = store.get_run("run-1")
            patch = "assertEquals(x, 1)"
            req = GenerationRequest("gen-6", "plan-1", "run-1", state.revision, "2099-11-09T17:00:04Z", patch, ["src/test/java/AppTest.java"], collected=1)
            with self.assertRaises(Exception):
                GenerationService(store).generate(req)
            store.close()

    def test_generation_rejects_weakened_assertion(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store, _ = prepare_coding_store(root)
            state = store.get_run("run-1")
            patch = "assertTrue(true) objective-1"
            req = GenerationRequest("gen-7", "plan-1", "run-1", state.revision, "2099-11-09T17:00:04Z", patch, ["src/test/java/AppTest.java"], collected=1)
            with self.assertRaises(GenerationConflictError):
                GenerationService(store).generate(req)
            store.close()

if __name__ == "__main__":
    unittest.main()
