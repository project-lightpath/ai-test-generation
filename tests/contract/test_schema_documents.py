import json
import pathlib
import sys
import unittest

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))
from atg.domain.run_state import RunEvent, RunState  # noqa: E402


CONTRACTS = pathlib.Path(__file__).parents[2] / "contracts" / "v1"
EXPECTED = {
    "request",
    "evidence",
    "objective",
    "plan",
    "checkpoint",
    "feedback",
    "generation",
    "execution",
    "quality",
    "provenance",
    "preflight",
    "compatibility",
    "signed-differential",
    "run-state",
    "run-event",
}


class ContractSchemaTests(unittest.TestCase):
    def test_version_one_contains_all_ticket_contracts(self):
        actual = {path.stem.removesuffix(".schema") for path in CONTRACTS.glob("*.schema.json")}
        self.assertEqual(actual, EXPECTED | {"common"})

    def test_every_contract_has_discriminator_and_version(self):
        for name in EXPECTED:
            with self.subTest(name=name):
                schema = json.loads((CONTRACTS / f"{name}.schema.json").read_text())
                self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertEqual(schema["properties"]["version"]["$ref"], "common.schema.json#/$defs/contract_version")
                self.assertIn("contract", schema["required"])
                self.assertIn("version", schema["required"])

    def test_common_outcome_is_explicit_about_non_pass_results(self):
        common = json.loads((CONTRACTS / "common.schema.json").read_text())
        outcome = common["$defs"]["outcome"]["enum"]
        self.assertIn("blocked", outcome)
        self.assertIn("inconclusive", outcome)
        self.assertIn("stated_gap", outcome)
        self.assertIn("not_reached", outcome)

    def test_representative_examples_validate_against_real_schemas(self):
        examples = {
            "request.json": "request",
            "evidence.json": "evidence",
            "objective.json": "objective",
            "plan.json": "plan",
            "feedback.json": "feedback",
            "generation.json": "generation",
            "run-state.json": "run-state",
            "run-event-checkpoint.json": "run-event",
            "checkpoint.json": "checkpoint",
            "execution.json": "execution",
            "execution-base.json": "execution",
            "quality.json": "quality",
            "provenance.json": "provenance",
            "preflight.json": "preflight",
            "signed-differential.json": "signed-differential",
        }
        for filename, schema_name in examples.items():
            with self.subTest(filename=filename):
                self._validator(schema_name).validate(self._load("examples", filename))
        self._validator("compatibility").validate(self._load("compatibility", "v1-run-state.json"))

    def test_invalid_event_fixture_is_rejected_by_event_schema(self):
        with self.assertRaises(ValidationError):
            self._validator("run-event").validate(self._load("invalid", "run-event-missing-decision.json"))

    def test_unsupported_positive_differential_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._validator("signed-differential").validate(
                self._load("invalid", "signed-differential-no-supporting-comparison.json")
            )

    def test_checkpoint_state_and_decision_must_agree(self):
        checkpoint = self._load("examples", "checkpoint.json")
        checkpoint["decision"]["value"] = "rejected"
        with self.assertRaises(ValidationError):
            self._validator("checkpoint").validate(checkpoint)

    def test_positive_quality_outcome_requires_a_passing_check(self):
        quality = self._load("examples", "quality.json")
        quality["checks"][0]["status"] = "inconclusive"
        with self.assertRaises(ValidationError):
            self._validator("quality").validate(quality)

    def test_invalid_timestamp_is_rejected_when_format_checking_is_enabled(self):
        request = self._load("examples", "request.json")
        request["submitted_at"] = "2026-99-99T99:99:99Z"
        with self.assertRaises(ValidationError):
            self._validator("request").validate(request)

    def test_serialized_run_states_validate_for_initial_active_and_blocked_states(self):
        initial = RunState(run_id="run-1", updated_at="2026-11-03T17:00:00Z")
        self._validator("run-state").validate(initial.to_document())

        active = initial.apply(
            RunEvent(
                event_id="event-admit",
                run_id="run-1",
                event_type="admit",
                expected_revision=0,
                occurred_at="2026-11-03T17:00:01Z",
            )
        ).state
        active = active.apply(
            RunEvent(
                event_id="event-checkpoint",
                run_id="run-1",
                event_type="checkpoint_created",
                expected_revision=1,
                occurred_at="2026-11-03T17:00:02Z",
                checkpoint_id="checkpoint-1",
                checkpoint_kind="plan_review",
                expires_at="2026-11-04T17:00:02Z",
            )
        ).state
        self._validator("run-state").validate(active.to_document())

        blocked = RunState(
            run_id="run-1",
            status="blocked",
            revision=1,
            updated_at="2026-11-03T17:00:03Z",
            terminal_reason="dependency unavailable",
        )
        self._validator("run-state").validate(blocked.to_document())

    def test_all_non_common_schemas_are_self_consistent(self):
        for name in EXPECTED:
            with self.subTest(name=name):
                schema = json.loads((CONTRACTS / f"{name}.schema.json").read_text())
                Draft202012Validator.check_schema(schema)

    @staticmethod
    def _load(directory, filename):
        path = pathlib.Path(__file__).parents[2] / "contracts" / directory / filename
        return json.loads(path.read_text())

    @staticmethod
    def _validator(name):
        root = pathlib.Path(__file__).parents[2] / "contracts" / "v1"
        common = json.loads((root / "common.schema.json").read_text())
        schema = json.loads((root / f"{name}.schema.json").read_text())
        registry = Registry().with_resource(common["$id"], Resource.from_contents(common))
        return Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())


if __name__ == "__main__":
    unittest.main()
