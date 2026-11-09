"""Framework-neutral repository analysis values."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import PurePosixPath


FACT_ORIGINS = frozenset({"supplied", "discovered", "inferred"})
FINDING_KINDS = frozenset({"missing", "stale", "conflicting", "ambiguous", "unsupported", "unauthorized", "incomplete"})
COVERAGE_KINDS = frozenset({"configuration", "report", "static_reference", "runtime"})
CAPABILITY_STATUSES = frozenset({"available", "partial", "unavailable"})


def _validate_relative_path(value: str, field: str) -> None:
    """Reject empty, absolute, or parent-traversing repository paths."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty repository-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must be a repository-relative path")


@dataclass(frozen=True)
class RepositorySubject:
    """Immutable identity of the repository content being analyzed."""

    repository_id: str
    revision: str

    def __post_init__(self) -> None:
        """Require stable repository and revision identities."""
        if not self.repository_id or not self.revision:
            raise ValueError("repository subject requires repository_id and revision")


@dataclass(frozen=True)
class ContextFact:
    """One attributable supplied, discovered, or inferred repository fact."""

    fact_id: str
    origin: str
    claim: str
    source_path: str | None = None
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate fact identity, origin, claim, and optional citation path."""
        if not self.fact_id or not self.claim:
            raise ValueError("context fact requires fact_id and claim")
        if self.origin not in FACT_ORIGINS:
            raise ValueError(f"unsupported fact origin: {self.origin}")
        if self.source_path is not None:
            _validate_relative_path(self.source_path, "source_path")
        if any(not reference for reference in self.evidence_refs):
            raise ValueError("fact evidence references must be non-empty")


@dataclass(frozen=True)
class CapabilityObservation:
    """Availability of one explicitly named analysis capability."""

    capability: str
    status: str
    required: bool = False
    detail: str | None = None

    def __post_init__(self) -> None:
        """Validate capability identity and controlled availability status."""
        if not self.capability:
            raise ValueError("capability identity is required")
        if self.status not in CAPABILITY_STATUSES:
            raise ValueError(f"unsupported capability status: {self.status}")


@dataclass(frozen=True)
class TestInventoryEntry:
    """A discovered repository-native test source."""

    path: str
    language: str
    framework_hint: str | None = None

    def __post_init__(self) -> None:
        """Validate the test location and language classification."""
        _validate_relative_path(self.path, "test path")
        if not self.language:
            raise ValueError("test inventory entry requires a language")


@dataclass(frozen=True)
class CoverageObservation:
    """A coverage-related artifact without an implied execution claim."""

    path: str
    kind: str
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        """Validate the coverage location and evidence kind."""
        _validate_relative_path(self.path, "coverage path")
        if self.kind not in COVERAGE_KINDS:
            raise ValueError(f"unsupported coverage observation kind: {self.kind}")
        if self.evidence_ref is not None and not self.evidence_ref:
            raise ValueError("coverage evidence_ref must be non-empty when provided")


@dataclass(frozen=True)
class AnalysisFinding:
    """An explicit limitation or gap discovered during repository analysis."""

    finding_id: str
    kind: str
    description: str
    path: str | None = None

    def __post_init__(self) -> None:
        """Validate finding identity, classification, and optional path."""
        if not self.finding_id or not self.description:
            raise ValueError("analysis finding requires finding_id and description")
        if self.kind not in FINDING_KINDS:
            raise ValueError(f"unsupported finding kind: {self.kind}")
        if self.path is not None:
            _validate_relative_path(self.path, "finding path")


@dataclass(frozen=True)
class RepositoryProfile:
    """Normalized read-only profile of one immutable repository subject."""

    subject: RepositorySubject
    languages: tuple[str, ...]
    build_systems: tuple[str, ...]
    manifests: tuple[str, ...]
    instruction_files: tuple[str, ...]
    source_roots: tuple[str, ...]
    test_roots: tuple[str, ...]
    scanned_file_count: int
    scanned_content_digest: str


@dataclass(frozen=True)
class RepositoryContextResult:
    """Deterministic repository profile, inventory, evidence facts, and gaps."""

    profile: RepositoryProfile
    facts: tuple[ContextFact, ...]
    tests: tuple[TestInventoryEntry, ...]
    coverage: tuple[CoverageObservation, ...]
    findings: tuple[AnalysisFinding, ...]
    capabilities: tuple[CapabilityObservation, ...] = ()

    def digest(self) -> str:
        """Return a stable digest over the normalized analysis result."""
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
