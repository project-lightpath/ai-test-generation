"""Deterministic local generation and validation adapters.

These adapters exercise the trust boundary without claiming live Maven, JUnit,
model, or execution-provider integration.
"""

from __future__ import annotations

import ast

from atg.ports.generation import PatchCandidate, ValidationResult


class RequestGenerationAdapter:
    """Read bounded candidates from the development request fixture."""

    def generate(self, request, attempt: int, feedback: tuple[str, ...]) -> PatchCandidate | None:
        """Return exactly one request candidate per bounded attempt."""
        candidates = request.candidates
        return candidates[attempt - 1] if attempt <= len(candidates) else None


class DeterministicValidationAdapter:
    """Apply local static prechecks without executing repository code."""

    def validate(self, candidate: PatchCandidate) -> ValidationResult:
        """Check syntax markers, formatting, compile markers, and collection."""
        errors: list[str] = []
        syntax_ok = True
        format_ok = True
        compile_ok = True
        collected = 0

        for path, content in candidate.materialized_files.items():
            lines = content.splitlines()
            if any(line != line.rstrip() or "\t" in line for line in lines):
                format_ok = False
                errors.append(f"format check failed: {path}")
            if path.endswith(".py"):
                try:
                    ast.parse(content)
                except SyntaxError:
                    syntax_ok = False
                    errors.append(f"syntax check failed: {path}")
                collected += sum(
                    1 for line in lines if line.lstrip().startswith("def test_")
                )
            elif path.endswith((".java", ".kt")):
                if (
                    content.count("{") != content.count("}")
                    or content.count("(") != content.count(")")
                    or "SYNTAX_ERROR" in content
                ):
                    syntax_ok = False
                    errors.append(f"syntax check failed: {path}")
                collected += content.count("@Test")
            else:
                collected += content.count("test(")
            if "COMPILE_ERROR" in content:
                compile_ok = False
                errors.append(f"compile precheck failed: {path}")

        if collected == 0:
            errors.append("no tests collected")
        return ValidationResult(
            syntax_ok=syntax_ok,
            format_ok=format_ok,
            compile_ok=compile_ok,
            collected=collected,
            errors=tuple(errors),
            limitations=(
                "Static development prechecks do not prove framework compilation or collection.",
            ),
        )
