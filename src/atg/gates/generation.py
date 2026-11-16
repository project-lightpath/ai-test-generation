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
    target_ok = not target_refs or any(
        target.lower() in materialized.lower() for target in target_refs
    )

    checks = (
        _check("approved_path", path_error is None, path_error),
        _check("syntax", validation.syntax_ok),
        _check("format", validation.format_ok),
        _check("compile", validation.compile_ok),
        _check("collection", validation.collected > 0, "no tests collected" if validation.collected < 1 else None),
        _check("assertion", assertion_ok, "missing or weakened assertion" if not assertion_ok else None),
        _check("target", target_ok, "wrong target" if not target_ok else None),
    )
    findings = tuple(
        check.get("detail", f"{check['name']} check failed")
        for check in checks
        if check["status"] == "fail"
    )
    return ("verified" if not findings else "rejected"), findings, checks
