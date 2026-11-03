"""Validate ATG schemas, fixtures, and optional complete-schema overrides."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource


EXAMPLES = {
    "request": "request.json",
    "evidence": "evidence.json",
    "objective": "objective.json",
    "plan": "plan.json",
    "feedback": "feedback.json",
    "generation": "generation.json",
    "run-state": "run-state.json",
    "run-event": "run-event-checkpoint.json",
    "checkpoint": "checkpoint.json",
    "execution": "execution.json",
    "quality": "quality.json",
    "provenance": "provenance.json",
    "preflight": "preflight.json",
    "signed-differential": "signed-differential.json",
}


def load_schema_documents(schema_root: Path, override_root: Path | None = None) -> dict[str, dict]:
    """Load base schemas, replacing complete files from an explicit profile."""

    schemas = {}
    for path in schema_root.glob("*.schema.json"):
        name = path.stem.removesuffix(".schema")
        selected = override_root / path.name if override_root and (override_root / path.name).exists() else path
        schemas[name] = json.loads(selected.read_text())
    return schemas


def validator_for(name: str, schemas: Mapping[str, dict]) -> Draft202012Validator:
    common = schemas["common"]
    registry = Registry().with_resource(common["$id"], Resource.from_contents(common))
    return Draft202012Validator(
        schemas[name], registry=registry, format_checker=FormatChecker()
    )


def validate_contracts(
    schema_root: Path,
    examples_root: Path,
    compatibility_root: Path,
    invalid_root: Path,
    override_root: Path | None = None,
) -> dict[str, dict]:
    schemas = load_schema_documents(schema_root, override_root)
    for name, schema in schemas.items():
        Draft202012Validator.check_schema(schema)
        validator_for(name, schemas)

    for name, filename in EXAMPLES.items():
        validator_for(name, schemas).validate(json.loads((examples_root / filename).read_text()))
    validator_for("compatibility", schemas).validate(
        json.loads((compatibility_root / "v1-run-state.json").read_text())
    )

    invalid_event = json.loads((invalid_root / "run-event-missing-decision.json").read_text())
    try:
        validator_for("run-event", schemas).validate(invalid_event)
    except ValidationError:
        pass
    else:
        raise AssertionError("invalid run-event fixture unexpectedly validated")
    return schemas


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema-root", type=Path, default=Path("contracts/v1"))
    parser.add_argument("--examples-root", type=Path, default=Path("contracts/examples"))
    parser.add_argument("--compatibility-root", type=Path, default=Path("contracts/compatibility"))
    parser.add_argument("--invalid-root", type=Path, default=Path("contracts/invalid"))
    parser.add_argument("--override-root", type=Path)
    args = parser.parse_args()
    validate_contracts(
        args.schema_root,
        args.examples_root,
        args.compatibility_root,
        args.invalid_root,
        args.override_root,
    )
    print("Contract schemas and fixtures validated")


if __name__ == "__main__":
    main()
