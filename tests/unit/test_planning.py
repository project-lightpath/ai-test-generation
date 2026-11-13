import json
import pathlib
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.adapters.persistence.sqlite_run_state import SQLiteRunStateStore  # noqa: E402
from atg.adapters.repository.local import LocalRepositoryContextAdapter  # noqa: E402
from atg.application.analysis import AnalysisService  # noqa: E402
from atg.application.planning import PlanningConflictError, PlanningRequest, PlanningService  # noqa: E402
from atg.application.feedback import FeedbackRequest  # noqa: E402
from atg.domain.run_state import RunEvent, StaleEventError  # noqa: E402
from tests.unit.test_analysis import analysis_request, admit, create_repository, supplied_evidence, supplied_objective  # noqa: E402


def prepare_planning_store(root: pathlib.Path):
    """Create an admitted run with an approved analysis and planning state."""
    store = SQLiteRunStateStore()
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
        "analysis-decision", "run-1", "decision_recorded", state.revision, "2099-11-09T17:00:01Z",
        checkpoint_id=analysis.checkpoint["checkpoint_id"], decision="approved", decision_id="decision-analysis",
        actor_id="reviewer-1", actor_role="owner", authorization_ref="auth-analysis",
    ))
    return store


class PlanningServiceTests(unittest.TestCase):
    def test_plan_persists_artifact_bound_review_and_coverage(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = prepare_planning_store(root)
            state = store.get_run("run-1")
            request = PlanningRequest("plan-1", "analysis-1", "run-1", state.revision, "2099-11-09T17:00:02Z")

            record = PlanningService(store).plan(request)

            self.assertEqual(record.manifest["status"], "draft")
            self.assertEqual(record.manifest["coverage_matrix"], [{"objective_ref": "objective-1", "disposition": "planned"}])
            self.assertEqual(record.checkpoint["reviewed_artifact"]["media_type"], "application/vnd.atg.plan+json")
            self.assertEqual(store.get_run("run-1").status, "plan_review")
            self.assertEqual(store.get_run("run-1").active_plan_id, "plan-1")
            self.assertEqual(store.get_plan("plan-1").manifest, record.manifest)
            store.apply(RunEvent(
                "plan-decision", "run-1", "decision_recorded", 7, "2099-11-09T17:00:03Z",
                checkpoint_id="checkpoint-plan-1", decision="approved", decision_id="decision-plan",
                actor_id="reviewer-1", actor_role="owner", authorization_ref="auth-plan",
            ))
            approved = store.get_plan("plan-1")
            self.assertEqual(approved.manifest["status"], "approved")
            self.assertEqual(approved.checkpoint["state"], "approved")
            self.assertEqual(store.get_checkpoint("checkpoint-plan-1")["state"], "approved")
            self.assertEqual(store.get_run("run-1").status, "coding")
            store.close()

    def test_plan_duplicate_is_idempotent(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "repo"
            root.mkdir()
            create_repository(root)
            database = pathlib.Path(directory) / "plans.sqlite3"
            store = prepare_planning_store(root)
            # The adapter persists the bundle and returns the stored record on retry.
            state = store.get_run("run-1")
            request = PlanningRequest("plan-1", "analysis-1", "run-1", state.revision, "2099-11-09T17:00:02Z")
            service = PlanningService(store)
            first = service.plan(request)
            duplicate = service.plan(request)
            self.assertTrue(duplicate.duplicate)
            self.assertEqual(json.dumps(first.to_storage_document(), sort_keys=True), json.dumps(duplicate.to_storage_document(), sort_keys=True))
            store.close()

    def test_plan_rejects_stale_revision_and_conflicting_identity(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = prepare_planning_store(root)
            service = PlanningService(store)
            state = store.get_run("run-1")
            with self.assertRaises(StaleEventError):
                service.plan(PlanningRequest("plan-stale", "analysis-1", "run-1", state.revision - 1, "2099-11-09T17:00:02Z"))
            request = PlanningRequest("plan-1", "analysis-1", "run-1", state.revision, "2099-11-09T17:00:02Z")
            service.plan(request)
            with self.assertRaises(PlanningConflictError):
                service.plan(PlanningRequest("plan-1", "analysis-other", "run-1", state.revision, "2099-11-09T17:00:02Z"))
            store.close()

    def test_feedback_creates_superseding_plan_revision(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            create_repository(root)
            store = prepare_planning_store(root)
            state = store.get_run("run-1")
            record = PlanningService(store).plan(PlanningRequest("plan-1", "analysis-1", "run-1", state.revision, "2099-11-09T17:00:02Z"))
            store.apply(RunEvent("plan-decision", "run-1", "decision_recorded", 7, "2099-11-09T17:00:03Z", checkpoint_id="checkpoint-plan-1", decision="approved", decision_id="decision-plan", actor_id="reviewer-1", actor_role="owner", authorization_ref="auth-plan"))
            feedback = FeedbackRequest("feedback-1", "run-1", "plan-1", 1, record.manifest["artifact_refs"][0] and record.artifacts[0]["artifact"]["digest"], "checkpoint-plan-1", "2099-11-09T17:00:04Z", "Clarify expected behavior")
            revised = PlanningService(store).revise_with_feedback(feedback)
            self.assertEqual(revised.manifest["plan_revision"], 2)
            self.assertEqual(revised.manifest["supersedes"], "plan-1")
            self.assertEqual(revised.manifest["feedback_refs"], ["feedback-1"])
            self.assertEqual(store.get_run("run-1").status, "plan_review")
            self.assertIsNotNone(store.get_feedback("feedback-1"))
            store.close()


if __name__ == "__main__":
    unittest.main()
