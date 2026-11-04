import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.domain.run_state import (  # noqa: E402
    DuplicateEventError,
    InvalidTransitionError,
    RunEvent,
    RunState,
    StaleEventError,
    SupersessionError,
)


def event(run_id, event_id, revision, event_type, **kwargs):
    occurred_at = kwargs.pop("occurred_at", f"2026-11-03T17:00:{revision:02d}Z")
    return RunEvent(
        event_id=event_id,
        run_id=run_id,
        event_type=event_type,
        expected_revision=revision,
        occurred_at=occurred_at,
        **kwargs,
    )


class RunStateTests(unittest.TestCase):
    def test_full_lifecycle_requires_durable_review_decisions(self):
        state = RunState.initial("run-1")
        state = state.apply(event("run-1", "e1", 0, "admit")).state
        state = state.apply(event("run-1", "e2", 1, "analysis_started")).state
        state = state.apply(event("run-1", "e3", 2, "analysis_completed")).state
        state = state.apply(
            event("run-1", "e4", 3, "checkpoint_created", checkpoint_id="cp-analysis", checkpoint_kind="analysis_review")
        ).state
        state = state.apply(
            event(
                "run-1", "e5", 4, "decision_recorded", checkpoint_id="cp-analysis", decision="approved",
                decision_id="decision-analysis", actor_id="reviewer-1", actor_role="owner",
                authorization_ref="approval-policy-1",
            )
        ).state
        self.assertEqual(state.status, "planning")
        state = state.apply(event("run-1", "e6", 5, "planning_completed")).state
        state = state.apply(
            event("run-1", "e7", 6, "checkpoint_created", checkpoint_id="cp-plan", checkpoint_kind="plan_review")
        ).state
        state = state.apply(
            event(
                "run-1", "e8", 7, "decision_recorded", checkpoint_id="cp-plan", decision="approved",
                decision_id="decision-plan", actor_id="reviewer-1", actor_role="owner",
                authorization_ref="approval-policy-1",
            )
        ).state
        state = state.apply(event("run-1", "e9", 8, "generation_started")).state
        state = state.apply(event("run-1", "e10", 9, "generation_completed")).state
        state = state.apply(
            event(
                "run-1",
                "e11",
                10,
                "checkpoint_created",
                checkpoint_id="cp-code",
                checkpoint_kind="code_review",
            )
        ).state
        state = state.apply(
            event(
                "run-1", "e12", 11, "decision_recorded", checkpoint_id="cp-code", decision="approved",
                decision_id="decision-code", actor_id="reviewer-1", actor_role="owner",
                authorization_ref="approval-policy-1",
            )
        ).state
        state = state.apply(event("run-1", "e13", 12, "execution_started")).state
        state = state.apply(event("run-1", "e14", 13, "execution_completed")).state
        state = state.apply(
            event(
                "run-1",
                "e15",
                14,
                "checkpoint_created",
                checkpoint_id="cp-exec",
                checkpoint_kind="execution_review",
            )
        ).state
        state = state.apply(
            event(
                "run-1", "e16", 15, "decision_recorded", checkpoint_id="cp-exec", decision="approved",
                decision_id="decision-exec", actor_id="reviewer-1", actor_role="owner",
                authorization_ref="approval-policy-1",
            )
        ).state
        state = state.apply(
            event(
                "run-1", "e17", 16, "checkpoint_created", checkpoint_id="cp-quality", checkpoint_kind="quality_review"
            )
        ).state
        state = state.apply(
            event(
                "run-1", "e18", 17, "decision_recorded", checkpoint_id="cp-quality", decision="approved",
                decision_id="decision-quality", actor_id="reviewer-1", actor_role="owner",
                authorization_ref="approval-policy-1",
            )
        ).state
        self.assertEqual(state.status, "completed")
        self.assertEqual(state.revision, 18)

    def test_exact_retry_is_idempotent(self):
        state = RunState.initial("run-1")
        admitted = event("run-1", "e1", 0, "admit")
        state = state.apply(admitted).state
        retry = state.apply(admitted)
        self.assertTrue(retry.duplicate)
        self.assertEqual(retry.state, state)

    def test_reusing_event_id_with_new_payload_is_rejected(self):
        state = RunState.initial("run-1")
        state = state.apply(event("run-1", "e1", 0, "admit")).state
        with self.assertRaises(DuplicateEventError):
            state.apply(event("run-1", "e1", 1, "analysis_started"))

    def test_stale_revision_is_rejected(self):
        state = RunState.initial("run-1")
        state = state.apply(event("run-1", "e1", 0, "admit")).state
        with self.assertRaises(StaleEventError):
            state.apply(event("run-1", "e2", 0, "analysis_started"))

    def test_event_timestamps_cannot_move_state_backwards(self):
        state = RunState.initial("run-1")
        state = state.apply(
            event("run-1", "e1", 0, "admit", occurred_at="2026-11-03T17:00:01Z")
        ).state
        with self.assertRaises(InvalidTransitionError):
            state.apply(
                event("run-1", "e2", 1, "analysis_started", occurred_at="2026-11-03T17:00:00Z")
            )

    def test_checkpoint_replacement_requires_explicit_supersession(self):
        state = RunState.initial("run-1")
        state = state.apply(event("run-1", "e1", 0, "admit")).state
        state = state.apply(
            event("run-1", "e2", 1, "checkpoint_created", checkpoint_id="cp-1", checkpoint_kind="plan_review")
        ).state
        with self.assertRaises(SupersessionError):
            state.apply(
                event("run-1", "e3", 2, "checkpoint_created", checkpoint_id="cp-2", checkpoint_kind="plan_review")
            )
        state = state.apply(
            event(
                "run-1",
                "e3",
                2,
                "checkpoint_created",
                checkpoint_id="cp-2",
                checkpoint_kind="plan_review",
                supersedes="cp-1",
            )
        ).state
        self.assertEqual(state.active_checkpoint_id, "cp-2")
        self.assertEqual(state.superseded_checkpoint_ids, ("cp-1",))
        with self.assertRaises(SupersessionError):
            state.apply(
                event(
                    "run-1", "e4", 3, "decision_recorded", checkpoint_id="cp-1", decision="approved",
                    decision_id="decision-old", actor_id="reviewer-1", actor_role="owner",
                    authorization_ref="approval-policy-1",
                )
            )

    def test_rejected_review_is_terminal_and_cannot_emit_evidence_flow(self):
        state = RunState.initial("run-1")
        state = state.apply(event("run-1", "e1", 0, "admit")).state
        state = state.apply(
            event("run-1", "e2", 1, "checkpoint_created", checkpoint_id="cp-1", checkpoint_kind="plan_review")
        ).state
        state = state.apply(
            event(
                "run-1",
                "e3",
                2,
                "decision_recorded",
                checkpoint_id="cp-1",
                decision="blocked",
                reason="repository identity could not be verified",
                decision_id="decision-blocked",
                actor_id="reviewer-1",
                actor_role="owner",
                authorization_ref="approval-policy-1",
            )
        ).state
        self.assertEqual(state.status, "blocked")
        with self.assertRaises(InvalidTransitionError):
            state.apply(event("run-1", "e4", 3, "analysis_started"))

    def test_checkpoint_expiry_returns_to_resumable_stage(self):
        state = RunState.initial("run-1")
        state = state.apply(event("run-1", "e1", 0, "admit")).state
        state = state.apply(
            event(
                "run-1", "e2", 1, "checkpoint_created", checkpoint_id="cp-1",
                checkpoint_kind="plan_review", expires_at="2026-11-03T17:10:00Z",
            )
        ).state
        with self.assertRaises(InvalidTransitionError):
            state.apply(
                event(
                    "run-1", "e3", 2, "checkpoint_expired", checkpoint_id="cp-1",
                )
            )
        state = state.apply(
            event(
                "run-1", "e3", 2, "checkpoint_expired", checkpoint_id="cp-1",
                occurred_at="2026-11-03T17:10:00.5Z",
            )
        ).state
        self.assertEqual(state.status, "admitted")
        self.assertIsNone(state.active_checkpoint_id)

        naive = RunState.initial("run-2")
        naive = naive.apply(event("run-2", "e1", 0, "admit")).state
        naive = naive.apply(
            event(
                "run-2", "e2", 1, "checkpoint_created", checkpoint_id="cp-2",
                checkpoint_kind="plan_review", expires_at="2026-11-03T17:10:00Z",
            )
        ).state
        with self.assertRaises(InvalidTransitionError):
            naive.apply(
                event(
                    "run-2", "e3", 2, "checkpoint_expired", checkpoint_id="cp-2",
                    occurred_at="2026-11-03T17:10:01",
                )
            )

    def test_blocked_run_can_be_explicitly_retried(self):
        state = RunState.initial("run-1")
        state = state.apply(event("run-1", "e1", 0, "admit")).state
        state = state.apply(
            event("run-1", "e2", 1, "checkpoint_created", checkpoint_id="cp-1", checkpoint_kind="plan_review")
        ).state
        state = state.apply(
            event(
                "run-1", "e3", 2, "decision_recorded", checkpoint_id="cp-1", decision="blocked",
                decision_id="decision-blocked", actor_id="reviewer-1", actor_role="owner",
                authorization_ref="approval-policy-1",
                reason="dependency unavailable",
            )
        ).state
        with self.assertRaises(InvalidTransitionError):
            state.apply(event("run-1", "e4", 3, "analysis_started"))

        state = state.apply(
            event("run-1", "e4", 3, "retry_requested", reason="dependency restored")
        ).state
        self.assertEqual(state.status, "admitted")
        state = state.apply(event("run-1", "e5", 4, "analysis_started")).state
        self.assertEqual(state.status, "analyzing")

    def test_rejected_decision_becomes_terminal_blocked_outcome(self):
        state = RunState.initial("run-1")
        state = state.apply(event("run-1", "e1", 0, "admit")).state
        state = state.apply(
            event("run-1", "e2", 1, "checkpoint_created", checkpoint_id="cp-1", checkpoint_kind="plan_review")
        ).state
        state = state.apply(
            event(
                "run-1", "e3", 2, "decision_recorded", checkpoint_id="cp-1", decision="rejected",
                decision_id="decision-rejected", actor_id="reviewer-1", actor_role="owner",
                authorization_ref="approval-policy-1", reason="plan is out of scope",
            )
        ).state
        self.assertEqual(state.status, "blocked")
        self.assertEqual(state.terminal_reason, "plan is out of scope")

    def test_unknown_event_and_wrong_run_are_rejected(self):
        state = RunState.initial("run-1")
        with self.assertRaises(InvalidTransitionError):
            state.apply(event("run-2", "e1", 0, "admit"))
        with self.assertRaises(InvalidTransitionError):
            state.apply(event("run-1", "e2", 0, "unknown_event"))

    def test_checkpoint_replacement_cannot_change_review_kind(self):
        state = RunState.initial("run-1")
        state = state.apply(event("run-1", "e1", 0, "admit")).state
        state = state.apply(
            event("run-1", "e2", 1, "checkpoint_created", checkpoint_id="cp-plan", checkpoint_kind="plan_review")
        ).state
        with self.assertRaises(InvalidTransitionError):
            state.apply(
                event(
                    "run-1", "e3", 2, "checkpoint_created", checkpoint_id="cp-code",
                    checkpoint_kind="code_review", supersedes="cp-plan",
                )
            )

    def test_review_decision_requires_authorization_metadata(self):
        state = RunState.initial("run-1")
        state = state.apply(event("run-1", "e1", 0, "admit")).state
        state = state.apply(
            event("run-1", "e2", 1, "checkpoint_created", checkpoint_id="cp-plan", checkpoint_kind="plan_review")
        ).state
        with self.assertRaises(InvalidTransitionError):
            state.apply(
                event("run-1", "e3", 2, "decision_recorded", checkpoint_id="cp-plan", decision="approved")
            )

    def test_corrupt_checkpoint_status_fails_as_transition_error(self):
        state = RunState(
            run_id="run-1",
            status="coding",
            revision=0,
            updated_at="2026-11-03T17:00:00Z",
            active_checkpoint_id="checkpoint-corrupt",
        )
        with self.assertRaises(InvalidTransitionError):
            state.apply(
                event(
                    "run-1", "e1", 0, "decision_recorded", checkpoint_id="checkpoint-corrupt",
                    decision="approved", decision_id="decision-1", actor_id="reviewer-1",
                    actor_role="owner", authorization_ref="approval-policy-1",
                )
            )

    def test_persisted_state_rejects_coercible_corruption(self):
        document = RunState(run_id="run-1", revision=1, updated_at="2026-11-03T17:00:00Z").to_document()
        document["revision"] = "1"
        with self.assertRaises(ValueError):
            RunState.from_document(document)
        document["revision"] = 1
        document["run_id"] = 123
        with self.assertRaises(ValueError):
            RunState.from_document(document)

    def test_quality_outcomes_are_explicit(self):
        expected = {
            "quality_passed": "completed",
            "quality_blocked": "blocked",
            "quality_inconclusive": "inconclusive",
            "quality_gap": "stated_gap",
        }
        for event_type, expected_status in expected.items():
            with self.subTest(event_type=event_type):
                state = RunState(
                    run_id="run-1",
                    status="quality_review",
                    revision=0,
                    updated_at="2026-11-03T17:00:00Z",
                    active_checkpoint_id="checkpoint-quality",
                    active_checkpoint_expires_at="2026-11-04T17:00:00Z",
                )
                result = state.apply(event("run-1", "e1", 0, event_type, reason="quality evidence"))
                self.assertEqual(result.state.status, expected_status)
                self.assertIsNone(result.state.active_checkpoint_id)
                self.assertIsNone(result.state.active_checkpoint_expires_at)

    def test_feedback_can_return_work_to_an_earlier_stage(self):
        cases = {
            "return_to_analyze": ("code_review", "analyzing"),
            "return_to_plan": ("code_review", "planning"),
            "return_to_code": ("execution_review", "coding"),
            "return_to_execute": ("quality_review", "executing"),
        }
        for event_type, (status, expected_status) in cases.items():
            with self.subTest(event_type=event_type):
                state = RunState(
                    run_id="run-1",
                    status=status,
                    revision=0,
                    updated_at="2026-11-03T17:00:00Z",
                    active_checkpoint_id="checkpoint-1",
                    active_checkpoint_expires_at="2026-11-04T17:00:00Z",
                    checkpoint_return_status="planning",
                )
                result = state.apply(event("run-1", "e1", 0, event_type, reason="review feedback"))
                self.assertEqual(result.state.status, expected_status)
                self.assertIsNone(result.state.active_checkpoint_id)
                self.assertIsNone(result.state.checkpoint_return_status)


if __name__ == "__main__":
    unittest.main()
