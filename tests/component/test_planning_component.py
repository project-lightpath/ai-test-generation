import unittest

from tests.component.test_analysis_component import AnalysisHTTPComponentTests


class PlanningHTTPComponentTests(unittest.TestCase):
    """Exercise the plan, checkpoint, and feedback HTTP boundary together."""

    def setUp(self):
        AnalysisHTTPComponentTests.setUp(self)

    def tearDown(self):
        AnalysisHTTPComponentTests.tearDown(self)

    def request(self, method, path, body=None, headers=None):
        return AnalysisHTTPComponentTests.request(self, method, path, body, headers)

    def admit(self):
        return AnalysisHTTPComponentTests.admit(self)

    def analysis_body(self):
        body = AnalysisHTTPComponentTests.analysis_body(self)
        body.update({
            "objective_refs": ["objective-1"],
            "supplied_evidence": [{
                "contract": "atg.evidence", "version": "1.0", "evidence_id": "evidence-1",
                "category": "repository", "subject": {"kind": "git-repository", "id": self.repository_id, "version": "abc123", "digest": self.repository_digest},
                "source": {"identity": {"kind": "issue", "id": "issue-1"}, "locator": "https://issues.example/1", "retrieved_at": "2099-11-09T20:00:00Z"},
                "observed_at": "2099-11-09T20:00:00Z", "claims": ["The behavior is defined."], "artifacts": [], "limitations": [],
            }],
            "supplied_objectives": [{
                "contract": "atg.objective", "version": "1.0", "objective_id": "objective-1",
                "statement": "Validate behavior.", "kind": "regression", "driver_type": "bug_report", "priority": "high",
                "acceptance": ["Behavior is validated."], "evidence_refs": ["evidence-1"], "target_refs": ["target-handler"], "assumptions": [], "gaps": [],
            }],
        })
        return body

    def test_plan_checkpoint_feedback_replay_and_supersession(self):
        self.admit()
        status, analysis = self.request("POST", "/analyses", self.analysis_body())
        self.assertEqual(status, 201)
        status, state = self.request("GET", "/runs/run-http-analysis")
        checkpoint = analysis["checkpoint"]
        decision = {
            "run_id": "run-http-analysis", "expected_revision": state["revision"], "occurred_at": "2099-11-09T20:03:00Z",
            "decision": "approved", "decision_id": "decision-analysis-http", "actor_id": "reviewer-1",
            "actor_role": "owner", "authorization_ref": "auth-analysis-http",
        }
        status, _ = self.request("POST", f"/checkpoints/{checkpoint['checkpoint_id']}/decision", decision)
        self.assertEqual(status, 200)
        status, state = self.request("GET", "/runs/run-http-analysis")
        plan = {"plan_id": "plan-http-1", "analysis_id": "analysis-http-1", "run_id": "run-http-analysis", "expected_revision": state["revision"], "occurred_at": "2099-11-09T20:04:00Z"}
        status, created = self.request("POST", "/plans", plan)
        self.assertEqual(status, 201)
        status, duplicate = self.request("POST", "/plans", plan)
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["duplicate"])
        status, state = self.request("GET", "/runs/run-http-analysis")
        plan_decision = {
            "run_id": "run-http-analysis", "expected_revision": state["revision"], "occurred_at": "2099-11-09T20:05:00Z",
            "decision": "approved", "decision_id": "decision-plan-http", "actor_id": "reviewer-1",
            "actor_role": "owner", "authorization_ref": "auth-plan-http",
        }
        self.assertEqual(self.request("POST", "/checkpoints/checkpoint-plan-http-1/decision", plan_decision)[0], 200)
        feedback = {
            "feedback_id": "feedback-http-1", "run_id": "run-http-analysis", "plan_revision": 1,
            "plan_artifact_digest": created["artifacts"][0]["digest"], "checkpoint_id": "checkpoint-plan-http-1",
            "created_at": "2099-11-09T20:06:00Z", "message": "Clarify expected behavior.",
            "category": "requirement_clarification", "severity": "warning", "actor_id": "reviewer-1",
        }
        status, revised = self.request("POST", "/plans/plan-http-1/feedback", feedback)
        self.assertEqual(status, 201)
        self.assertEqual(revised["manifest"]["supersedes"], "plan-http-1")
        status, replay = self.request("POST", "/plans/plan-http-1/feedback", feedback)
        self.assertEqual(status, 200)
        self.assertTrue(replay["duplicate"])


if __name__ == "__main__":
    unittest.main()
