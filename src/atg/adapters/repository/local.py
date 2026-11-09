"""Bounded read-only discovery for a locally materialized repository."""

from __future__ import annotations

import hashlib
from pathlib import Path

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

        files: list[Path] = []
        findings: list[AnalysisFinding] = []
        for candidate in sorted(root.rglob("*"), key=lambda path: path.as_posix()):
            relative = candidate.relative_to(root)
            if any(part in _IGNORED_DIRECTORIES for part in relative.parts):
                continue
            if candidate.is_symlink():
                findings.append(AnalysisFinding(
                    f"finding-symlink-{len(findings) + 1}",
                    "unsupported",
                    "Symbolic links are not followed during repository discovery.",
                    relative.as_posix(),
                ))
                continue
            if not candidate.is_file():
                continue
            if len(files) >= self._max_files:
                findings.append(AnalysisFinding(
                    "finding-file-limit", "incomplete",
                    f"Repository discovery stopped at the {self._max_files}-file limit.",
                ))
                break
            files.append(candidate)

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

        for path in files:
            relative = path.relative_to(root).as_posix()
            size = path.stat().st_size
            if size > self._max_file_bytes or total_bytes + size > self._max_total_bytes:
                findings.append(AnalysisFinding(
                    f"finding-size-limit-{len(findings) + 1}", "incomplete",
                    "File content was excluded by repository discovery byte limits.", relative,
                ))
                continue
            content = path.read_bytes()
            total_bytes += size
            content_hash.update(relative.encode("utf-8"))
            content_hash.update(b"\0")
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
        or lower.endswith("test.java")
        or lower.endswith("tests.java")
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
