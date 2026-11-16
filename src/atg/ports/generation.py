"""Ports for bounded test generation and deterministic validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True)
class PatchCandidate:
    """One untrusted generated patch and its materialized test files."""

    patch_text: str
    materialized_files: Mapping[str, str]


@dataclass(frozen=True)
class ValidationResult:
    """Deterministic precheck results for one generated candidate."""

    syntax_ok: bool
    format_ok: bool
    compile_ok: bool
    collected: int
    errors: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


class GenerationPort(Protocol):
    """Produce an initial candidate or bounded repair from untrusted output."""

    def generate(self, request: object, attempt: int, feedback: tuple[str, ...]) -> PatchCandidate | None:
        """Return the candidate for an attempt, or None when no repair exists."""


class ValidationPort(Protocol):
    """Validate materialized tests without accepting model self-assessment."""

    def validate(self, candidate: PatchCandidate) -> ValidationResult:
        """Run deterministic syntax, format, compile, and collection prechecks."""
