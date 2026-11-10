"""Runtime validation for canonical ATG contract documents."""

from __future__ import annotations

import json
import os
import sysconfig
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource


class ContractValidationError(ValueError):
    """Raised when an untrusted document fails its canonical schema."""


@lru_cache(maxsize=1)
def _schemas() -> dict[str, dict]:
    """Load and cache the repository's canonical v1 schemas."""
    root = contract_asset_path("v1")
    return {
        path.stem.removesuffix(".schema"): json.loads(path.read_text())
        for path in root.glob("*.schema.json")
    }


def contract_asset_path(relative: str) -> Path:
    """Locate canonical contract assets in a checkout or installed distribution."""
    configured = os.environ.get("ATG_CONTRACT_ROOT")
    candidates = [
        Path(configured) if configured else None,
        Path(__file__).parents[3] / "contracts",
        Path(sysconfig.get_path("data")) / "share" / "atg" / "contracts",
    ]
    for candidate in candidates:
        path = candidate / relative if candidate is not None else None
        if path is not None and path.exists():
            return path
    raise ContractValidationError("canonical ATG contract assets are unavailable")


def validate_contract_document(document: Mapping[str, object], expected: str) -> None:
    """Validate one untrusted document against an expected contract schema."""
    schemas = _schemas()
    schema = schemas[expected]
    common = schemas["common"]
    registry = Registry().with_resource(common["$id"], Resource.from_contents(common))
    try:
        Draft202012Validator(schema, registry=registry, format_checker=FormatChecker()).validate(document)
    except ValidationError as exc:
        raise ContractValidationError(f"invalid {expected} contract document") from exc
