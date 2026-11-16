"""Ports for bounded test generation and deterministic validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol


@dataclass(frozen=True)
class PatchCandidate:
    """One untrusted generated patch and its materialized test files."""

    patch_text: str
    materialized_files: Mapping[str, str]
    base_files: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationResult:
    """Deterministic precheck results for one generated candidate."""

    syntax_ok: bool | None
    format_ok: bool | None
    compile_ok: bool | None
    collected: int | None
    errors: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()


class GenerationPort(Protocol):
    """Produce an initial candidate or bounded repair from untrusted output."""

    def generate(self, request: object, attempt: int, feedback: tuple[str, ...]) -> PatchCandidate | None:
        """Return the candidate for an attempt, or None when no repair exists."""


class ValidationPort(Protocol):
    """Validate materialized tests without accepting model self-assessment."""

    def validate(self, candidate: PatchCandidate) -> ValidationResult:
        """Run deterministic syntax, format, compile, and collection prechecks."""


@dataclass(frozen=True)
class MaterializationResult:
    """Exact patch materialization result for one configured subject workspace."""

    workspace_identity: Mapping[str, str]
    changed_files: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    patch_digest: str
    materialized_test_digest: str

    def to_manifest(self) -> dict[str, object]:
        """Serialize verified allowed paths and expected content digests."""
        return {
            "subject": dict(self.workspace_identity),
            "TESTS_ADDED": list(self.changed_files),
            "allowed_paths": list(self.allowed_paths),
            "expected_patch_digest": self.patch_digest,
            "materialized_test_digest": self.materialized_test_digest,
        }


class PatchMaterializationPort(Protocol):
    """Apply an already-approved patch into a configured isolated workspace."""

    def materialize(
        self,
        candidate: PatchCandidate,
        workspace_root: str,
        workspace_identity: Mapping[str, str],
        allowed_paths: tuple[str, ...],
        expected_patch_digest: str,
        expected_materialized_digest: str,
    ) -> MaterializationResult:
        """Write only approved files and verify the resulting content digests."""


@dataclass(frozen=True)
class SscTektonCompatibilityResult:
    """Optional legacy-field projection; never replaces ATG canonical records."""

    tests_added: tuple[str, ...]
    pr_url: str | None
    run_ref: str | None
    evidence_ref: str | None
    terminal_status: str

    def to_document(self) -> dict[str, object]:
        """Return the bounded compatibility field names used by the SSC adapter."""
        return {
            "TESTS_ADDED": list(self.tests_added),
            "PR_URL": self.pr_url,
            "RUN_REF": self.run_ref,
            "EVIDENCE_REF": self.evidence_ref,
            "terminal_status": self.terminal_status,
        }
