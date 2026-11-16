from __future__ import annotations

import re

_ASSERT_PATTERN = re.compile(r"\bassert\w*\s*\(|assertThat|assertEquals|assertTrue|assertFalse|assertNotNull|assertThrows|assertArrayEquals", re.IGNORECASE)
_TRIVIAL_ASSERT = re.compile(r"assertTrue\s*\(\s*true\s*\)|assertFalse\s*\(\s*false\s*\)|assertEquals\s*\(\s*1\s*,\s*1\s*\)", re.IGNORECASE)


def validate_collection(collected: int) -> None:
    if not isinstance(collected, int) or collected < 1:
        raise ValueError("no tests collected")


def validate_assertions(patch_text: str) -> None:
    if not isinstance(patch_text, str) or not _ASSERT_PATTERN.search(patch_text):
        raise ValueError("no meaningful assertion")
    if _TRIVIAL_ASSERT.search(patch_text):
        raise ValueError("weakened assertion")


def validate_objective_target(patch_text: str, objective_refs: list[str]) -> None:
    if not objective_refs:
        return
    lowered = patch_text.lower()
    if not any(ref.lower() in lowered for ref in objective_refs):
        raise ValueError("wrong target")


def evaluate_generation(patch_text: str, collected: int, objective_refs: list[str]) -> tuple[str, str | None]:
    try:
        validate_collection(collected)
    except ValueError as exc:
        return "rejected", str(exc)
    try:
        validate_assertions(patch_text)
    except ValueError as exc:
        return "rejected", str(exc)
    try:
        validate_objective_target(patch_text, objective_refs)
    except ValueError as exc:
        return "rejected", str(exc)
    return "verified", None
