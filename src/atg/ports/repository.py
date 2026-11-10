"""Boundary for bounded, read-only repository context discovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from atg.domain.repository_analysis import RepositoryContextResult, RepositorySubject


@dataclass(frozen=True)
class RepositoryContextRequest:
    """Request to inspect one locally materialized immutable repository."""

    subject: RepositorySubject
    repository_root: str


class RepositoryContextPort(Protocol):
    """Discover repository facts without executing repository-controlled code."""

    producer_identity: Mapping[str, object]

    def collect(self, request: RepositoryContextRequest) -> RepositoryContextResult:
        """Return a bounded repository profile and supporting observations."""
