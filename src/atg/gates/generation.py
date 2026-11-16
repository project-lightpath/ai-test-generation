"""Deterministic quality gates for generated native tests."""

from __future__ import annotations

import re
from typing import Mapping

from atg.ports.generation import PatchCandidate, ValidationResult


_ASSERT_PATTERN = re.compile(
    r"\bassert\w*\s*\(|assertThat|assertEquals|assertTrue|assertFalse|"
    r"assertNotNull|assertThrows|assertArrayEquals|\bassert\s+",
    re.IGNORECASE,
)
_TRIVIAL_ASSERT = re.compile(
    r"assertTrue\s*\(\s*true\s*\)|assertFalse\s*\(\s*false\s*\)|"
    r"assertEquals\s*\(\s*([\w.\"']+)\s*,\s*\1\s*\)|"
    r"\bassert\s+True\b|\bassert\s+([\w.\"']+)\s*==\s*\2\b",
    re.IGNORECASE,
)


def _check(name: str, passed: bool, detail: str | None = None) -> dict[str, str]:
    document = {"name": name, "status": "pass" if passed else "fail"}
    if detail:
        document["detail"] = detail
    return document


def _validation_check(
    name: str, result: bool | None, unsupported: set[str]
) -> dict[str, str]:
    if name in unsupported or result is None:
        return {"name": name, "status": "not_supported", "detail": f"{name} validation is not configured"}
    return _check(name, result, f"{name} validation failed" if not result else None)


def evaluate_generation(
    candidate: PatchCandidate,
    validation: ValidationResult,
    target_refs: tuple[str, ...],
    path_error: str | None = None,
) -> tuple[str, tuple[str, ...], tuple[Mapping[str, str], ...]]:
    """Evaluate one untrusted candidate and return attributable gate checks."""
    materialized = "\n".join(candidate.materialized_files.values())
    added_lines = "\n".join(
        line[1:]
        for line in candidate.patch_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    removed_assertions = sum(
        1
        for line in candidate.patch_text.splitlines()
        if line.startswith("-") and not line.startswith("---") and _ASSERT_PATTERN.search(line[1:])
    )
    added_assertions = len(_ASSERT_PATTERN.findall(added_lines))
    assertion_ok = bool(_ASSERT_PATTERN.search(materialized)) and not _TRIVIAL_ASSERT.search(materialized)
    if removed_assertions > added_assertions:
        assertion_ok = False
    target_ok = bool(target_refs) and any(
        target.lower() in materialized.lower() for target in target_refs
    )
    unsupported = set(validation.unsupported)
    checks = [
        _check("approved_path", path_error is None, path_error),
        _validation_check("syntax", validation.syntax_ok, unsupported),
        _validation_check("format", validation.format_ok, unsupported),
        _validation_check("compile", validation.compile_ok, unsupported),
        (
            {"name": "collection", "status": "not_supported", "detail": "collection validation is not configured"}
            if "collection" in unsupported or validation.collected is None
            else _check("collection", validation.collected > 0, "no tests collected" if validation.collected < 1 else None)
        ),
        _check("assertion", assertion_ok, "missing or weakened assertion" if not assertion_ok else None),
        (
            {"name": "target", "status": "not_supported", "detail": "objective target references are unavailable"}
            if not target_refs
            else _check("target", target_ok, "wrong target" if not target_ok else None)
        ),
    ]
    findings = tuple(
        check.get("detail", f"{check['name']} check failed")
        for check in checks
        if check["status"] != "pass"
    )
    status = (
        "rejected" if any(check["status"] == "fail" for check in checks)
        else "inconclusive" if any(check["status"] == "not_supported" for check in checks)
        else "verified"
    )
    return status, findings, tuple(checks)
