"""Bounded read-only discovery for a locally materialized repository."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat

from atg.domain.repository_analysis import (
    AnalysisFinding,
    CapabilityObservation,
    ContextFact,
    CoverageObservation,
    RepositoryContextResult,
    RepositoryProfile,
    TestInventoryEntry,
)
from atg.ports.repository import RepositoryContextRequest


_IGNORED_DIRECTORIES = frozenset({".git", ".hg", ".svn", ".venv", "node_modules", "target", "build", "dist", "__pycache__"})
_MANIFESTS = {
    "pyproject.toml": ("python", "python"),
    "pom.xml": ("java", "maven"),
    "build.gradle": ("java", "gradle"),
    "build.gradle.kts": ("java", "gradle"),
    "go.mod": ("go", "go"),
    "package.json": ("javascript", "node"),
    "Cargo.toml": ("rust", "cargo"),
}
_LANGUAGES = {
    ".py": "python",
    ".java": "java",
    ".kt": "kotlin",
    ".go": "go",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rs": "rust",
}
_COVERAGE_REPORTS = frozenset({".coverage", "coverage.json", "coverage.xml", "jacoco.xml", "lcov.info"})
_COVERAGE_CONFIGS = frozenset({".coveragerc", "jacoco.exec"})


class LocalRepositoryContextAdapter:
    """Discover bounded repository metadata without running repository code."""

    producer_identity = {"kind": "repository-analyzer", "id": "atg-local-repository-context", "version": "1"}

    def __init__(self, *, max_files: int = 10_000, max_file_bytes: int = 5_000_000, max_total_bytes: int = 50_000_000) -> None:
        """Configure explicit file and byte limits for one discovery run."""
        if min(max_files, max_file_bytes, max_total_bytes) <= 0:
            raise ValueError("repository discovery limits must be positive")
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._max_total_bytes = max_total_bytes

    def collect(self, request: RepositoryContextRequest) -> RepositoryContextResult:
        """Collect deterministic metadata while skipping unsafe or excess input."""
        root = Path(request.repository_root).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("repository_root must be a directory")

        root_descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        languages: set[str] = set()
        build_systems: set[str] = set()
        manifests: list[str] = []
        instructions: list[str] = []
        source_roots: set[str] = set()
        test_roots: set[str] = set()
        tests: list[TestInventoryEntry] = []
        coverage: list[CoverageObservation] = []
        facts: list[ContextFact] = []
        content_hash = hashlib.sha256()
        total_bytes = 0
        try:
            files, findings = _enumerate_repository(root_descriptor, self._max_files)
            for relative in files:
                path = Path(relative)
                remaining = self._max_total_bytes - total_bytes
                content = _read_bounded(root_descriptor, relative, min(self._max_file_bytes, remaining))
                if content is None:
                    findings.append(AnalysisFinding(
                        f"finding-size-limit-{len(findings) + 1}", "incomplete",
                        "File content was unreadable or excluded by repository discovery limits.", relative,
                    ))
                    continue
                total_bytes += len(content)
                encoded_relative = os.fsencode(relative)
                content_hash.update(len(encoded_relative).to_bytes(8, "big"))
                content_hash.update(encoded_relative)
                content_hash.update(len(content).to_bytes(8, "big"))
                content_hash.update(content)

                language = _LANGUAGES.get(path.suffix.lower())
                if language:
                    languages.add(language)
                if path.name in _MANIFESTS:
                    manifest_language, build_system = _MANIFESTS[path.name]
                    languages.add(manifest_language)
                    build_systems.add(build_system)
                    manifests.append(relative)
                if path.name in {"AGENTS.md", "CLAUDE.md"} or relative.startswith(".cursor/rules/"):
                    instructions.append(relative)
                    facts.append(ContextFact(
                        f"fact-instruction-{len(instructions)}", "discovered",
                        "Repository instruction content is present but is not execution authorization.", relative,
                    ))

                root_kind = _root_kind(relative)
                if root_kind == "source":
                    source_roots.add(_root_path(relative))
                elif root_kind == "test":
                    test_roots.add(_root_path(relative))

                if language and _is_test_file(path.name, relative):
                    tests.append(TestInventoryEntry(relative, language, _framework_hint(language)))
                if path.name in _COVERAGE_REPORTS:
                    coverage.append(CoverageObservation(relative, "report"))
                elif path.name in _COVERAGE_CONFIGS:
                    coverage.append(CoverageObservation(relative, "configuration"))
        finally:
            os.close(root_descriptor)

        for manifest in sorted(manifests):
            facts.append(ContextFact(
                f"fact-manifest-{len(facts) + 1}", "discovered",
                f"Build manifest {manifest} is present.", manifest,
            ))

        profile = RepositoryProfile(
            subject=request.subject,
            languages=tuple(sorted(languages)),
            build_systems=tuple(sorted(build_systems)),
            manifests=tuple(sorted(manifests)),
            instruction_files=tuple(sorted(instructions)),
            source_roots=tuple(sorted(source_roots)),
            test_roots=tuple(sorted(test_roots)),
            scanned_file_count=len(files),
            scanned_content_digest="sha256:" + content_hash.hexdigest(),
        )
        if request.subject.content_digest is None:
            findings.append(AnalysisFinding(
                "finding-materialization-attestation",
                "stale",
                "Repository revision is materializer-attested; no expected content digest was supplied.",
            ))
        elif request.subject.content_digest != profile.scanned_content_digest:
            findings.append(AnalysisFinding(
                "finding-materialization-digest",
                "conflicting",
                "Materialized repository content digest differs from the admitted subject digest.",
            ))
        return RepositoryContextResult(
            profile,
            tuple(sorted(facts, key=lambda fact: fact.fact_id)),
            tuple(sorted(tests, key=lambda test: test.path)),
            tuple(sorted(coverage, key=lambda item: (item.path, item.kind))),
            tuple(findings),
            (
                CapabilityObservation("repository-discovery", "available"),
                CapabilityObservation("test-discovery", "available"),
                CapabilityObservation("coverage-discovery", "available"),
            ),
        )


def _is_test_file(name: str, relative: str) -> bool:
    """Return whether a repository path follows a supported test convention."""
    lower = name.lower()
    return (
        lower.startswith("test_")
        or lower.endswith("_test.py")
        or lower.endswith("_test.go")
        or name.endswith("Test.java")
        or name.endswith("Tests.java")
        or ".spec." in lower
        or ".test." in lower
        or "/test/" in f"/{relative.lower()}/"
        or relative.lower().startswith("tests/")
    )


def _framework_hint(language: str) -> str | None:
    """Return a coarse framework-neutral test runner hint for a language."""
    return {
        "python": "python-test",
        "java": "junit-compatible",
        "kotlin": "junit-compatible",
        "go": "go-test",
        "javascript": "javascript-test",
        "typescript": "javascript-test",
        "rust": "cargo-test",
    }.get(language)


def _root_kind(relative: str) -> str | None:
    """Classify a conventional repository root as source or test content."""
    parts = relative.split("/")
    if parts[:2] == ["src", "main"] or parts[0] in {"lib", "app"}:
        return "source"
    if parts[:2] == ["src", "test"] or parts[0] in {"test", "tests"}:
        return "test"
    if parts[0] == "src":
        return "source"
    return None


def _root_path(relative: str) -> str:
    """Return the conventional source or test root containing a path."""
    parts = relative.split("/")
    return "/".join(parts[:2]) if parts[:2] in (["src", "main"], ["src", "test"]) else parts[0]


def _enumerate_repository(root_descriptor: int, max_files: int) -> tuple[list[str], list[AnalysisFinding]]:
    """Enumerate repository files through no-follow directory descriptors."""
    files: list[str] = []
    findings: list[AnalysisFinding] = []
    stack = [""]
    entries_seen = 0
    entry_limit = max_files * 4
    stopped = False
    while stack and not stopped:
        prefix = stack.pop()
        try:
            descriptor = _open_directory(root_descriptor, prefix)
        except OSError:
            findings.append(AnalysisFinding(
                f"finding-directory-read-{len(findings) + 1}", "incomplete",
                "Repository directory could not be opened safely.", prefix or ".",
            ))
            continue
        try:
            with os.scandir(descriptor) as iterator:
                entries = []
                for entry in iterator:
                    entries_seen += 1
                    if entries_seen > entry_limit:
                        findings.append(AnalysisFinding(
                            "finding-entry-limit", "incomplete",
                            f"Repository discovery stopped at the {entry_limit}-entry limit.",
                        ))
                        stopped = True
                        break
                    entries.append(entry)
            for entry in sorted(entries, key=lambda item: item.name, reverse=True):
                relative = f"{prefix}/{entry.name}".lstrip("/")
                if entry.name in _IGNORED_DIRECTORIES:
                    continue
                if entry.is_symlink():
                    findings.append(AnalysisFinding(
                        f"finding-symlink-{len(findings) + 1}", "unsupported",
                        "Symbolic links are not followed during repository discovery.", relative,
                    ))
                elif entry.is_dir(follow_symlinks=False):
                    stack.append(relative)
                elif entry.is_file(follow_symlinks=False):
                    if len(files) >= max_files:
                        findings.append(AnalysisFinding(
                            "finding-file-limit", "incomplete",
                            f"Repository discovery stopped at the {max_files}-file limit.",
                        ))
                        stopped = True
                        break
                    files.append(relative)
        finally:
            os.close(descriptor)
    return sorted(files), findings


def _open_directory(root_descriptor: int, relative: str) -> int:
    """Open one repository directory lazily through no-follow descriptors."""
    directory = os.dup(root_descriptor)
    try:
        for part in filter(None, relative.split("/")):
            child = os.open(
                part,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        return directory
    except Exception:
        os.close(directory)
        raise


def _read_bounded(root_descriptor: int, relative: str, limit: int) -> bytes | None:
    """Read a regular file through a no-follow descriptor within a hard limit."""
    if limit < 0:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory = os.dup(root_descriptor)
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            child = os.open(
                part,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], flags, dir_fd=directory)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                return None
            chunks: list[bytes] = []
            read = 0
            while read <= limit:
                chunk = os.read(descriptor, min(65_536, limit - read + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                read += len(chunk)
                if read > limit:
                    return None
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError:
        return None
    finally:
        os.close(directory)
