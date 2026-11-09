"""Boundary for replayable repository analyzer evidence providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from atg.domain.repository_analysis import AnalysisFinding, CapabilityObservation, ContextFact, RepositorySubject


@dataclass(frozen=True)
class RawAnalysisArtifact:
    """Provider-specific content retained behind canonical artifact metadata."""

    artifact_id: str
    media_type: str
    content: Mapping[str, object]


@dataclass(frozen=True)
class AnalyzerEvidenceResult:
    """Normalized analyzer evidence, facts, capabilities, gaps, and raw output."""

    facts: tuple[ContextFact, ...]
    evidence: tuple[Mapping[str, object], ...]
    capabilities: tuple[CapabilityObservation, ...]
    findings: tuple[AnalysisFinding, ...]
    artifacts: tuple[RawAnalysisArtifact, ...]


class RepositoryAnalyzerPort(Protocol):
    """Translate bounded external analyzer output into ATG-owned evidence."""

    def collect(self, subject: RepositorySubject) -> AnalyzerEvidenceResult:
        """Return normalized replay evidence for one immutable subject."""
