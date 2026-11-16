"""Deterministic local generation and validation adapters.

These adapters exercise the trust boundary without claiming live Maven, JUnit,
model, or execution-provider integration.
"""

from __future__ import annotations

import ast
import errno
import os
import stat
import threading
import uuid
from pathlib import Path

from atg.domain.digests import digests_equal
from atg.domain.generation import (
    compute_materialized_digest,
    compute_patch_digest,
    validate_candidate_paths,
)
from atg.ports.generation import GeneratedCandidate, PatchCandidate, ValidationResult


class ReplayGenerationAdapter:
    """Return configured recorded outputs without accepting them over HTTP."""

    def __init__(self, outputs=None):
        self._lock = threading.RLock()
        self._outputs = dict(outputs or {})

    def configure(self, generation_id: str, outputs: tuple[GeneratedCandidate, ...]) -> None:
        """Install deterministic test outputs for one generation identity."""
        with self._lock:
            self._outputs[generation_id] = tuple(outputs)

    def generate(
        self, request, attempt: int, feedback: tuple[str, ...]
    ) -> GeneratedCandidate | None:
        """Return a pre-recorded candidate for the requested attempt."""
        with self._lock:
            outputs = self._outputs.get(request.generation_id, ())
        return outputs[attempt - 1] if 1 <= attempt <= len(outputs) else None


class DeterministicValidationAdapter:
    """Run local syntax checks without pretending to be a framework runner."""

    def validate(self, candidate: PatchCandidate) -> ValidationResult:
        """Check syntax markers, formatting, compile markers, and collection."""
        errors: list[str] = []
        syntax_ok: bool | None = True
        format_ok = True
        compile_ok: bool | None = True
        unsupported: set[str] = {"collection"}

        for path, content in candidate.materialized_files.items():
            lines = content.splitlines()
            if any(line != line.rstrip() or "\t" in line for line in lines):
                format_ok = False
                errors.append(f"format check failed: {path}")
            if path.endswith(".py"):
                try:
                    ast.parse(content)
                    compile(content, path, "exec")
                except SyntaxError:
                    syntax_ok = False
                    errors.append(f"syntax check failed: {path}")
            else:
                syntax_ok = None
                compile_ok = None
                unsupported.update(("syntax", "compile"))
        return ValidationResult(
            syntax_ok=syntax_ok,
            format_ok=format_ok,
            compile_ok=compile_ok,
            collected=None,
            errors=tuple(errors),
            limitations=(
                "Framework collection is not run by the static development adapter.",
            ),
            unsupported=tuple(sorted(unsupported)),
        )


class FilesystemPatchMaterializer:
    """Write generated tests safely into a configured isolated workspace."""

    MAX_PATCH_BYTES = 5_000_000
    MAX_FILE_BYTES = 1_000_000
    MAX_TOTAL_BYTES = 5_000_000

    def materialize(
        self,
        candidate: PatchCandidate,
        workspace_root: str,
        workspace_identity,
        allowed_paths: tuple[str, ...],
        expected_patch_digest: str,
        expected_materialized_digest: str,
    ):
        """Apply exact validated files, refusing traversal and symlink escapes."""
        patch_bytes = candidate.patch_text.encode()
        if len(patch_bytes) > self.MAX_PATCH_BYTES:
            raise ValueError("patch exceeds maximum size")
        if not digests_equal(compute_patch_digest(candidate.patch_text), expected_patch_digest):
            raise ValueError("patch digest mismatch")
        changed_files = validate_candidate_paths(
            candidate.patch_text,
            candidate.materialized_files,
            allowed_paths,
            candidate.base_files,
        )
        total_bytes = sum(len(content.encode()) for content in candidate.materialized_files.values())
        if total_bytes > self.MAX_TOTAL_BYTES or any(
            len(content.encode()) > self.MAX_FILE_BYTES
            for content in candidate.materialized_files.values()
        ):
            raise ValueError("materialized tests exceed size limits")
        materialized_digest = compute_materialized_digest(candidate.materialized_files)
        if not digests_equal(materialized_digest, expected_materialized_digest):
            raise ValueError("materialized test digest mismatch")

        root = Path(workspace_root)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("workspace root must be a real directory")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        root_fd = os.open(root, flags)
        try:
            for path in changed_files:
                parent_fd = os.dup(root_fd)
                parts = path.split("/")
                try:
                    for component in parts[:-1]:
                        try:
                            os.mkdir(component, mode=0o755, dir_fd=parent_fd)
                        except FileExistsError:
                            pass
                        try:
                            next_fd = os.open(component, flags, dir_fd=parent_fd)
                        except OSError as exc:
                            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                                raise ValueError("workspace path contains a symlink or non-directory") from exc
                            raise
                        os.close(parent_fd)
                        parent_fd = next_fd
                    name = parts[-1]
                    expected_base = candidate.base_files.get(path)
                    try:
                        existing = _read_relative_file(parent_fd, name, self.MAX_FILE_BYTES)
                    except OSError as exc:
                        if exc.errno == errno.ELOOP:
                            raise ValueError(f"workspace target is a symlink: {path}") from exc
                        raise
                    content = candidate.materialized_files[path].encode()
                    if existing != content:
                        if existing is None:
                            if expected_base is not None:
                                raise ValueError(f"base file missing from workspace: {path}")
                        elif expected_base is None or existing != expected_base.encode():
                            raise ValueError(f"workspace base content mismatch: {path}")
                        temp_name = f".atg-{uuid.uuid4().hex}.tmp"
                        file_fd = os.open(
                            temp_name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                            0o600,
                            dir_fd=parent_fd,
                        )
                        try:
                            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                                raise ValueError("temporary materialization target is not a regular file")
                            view = memoryview(content)
                            while view:
                                view = view[os.write(file_fd, view):]
                            os.fsync(file_fd)
                        except Exception:
                            os.unlink(temp_name, dir_fd=parent_fd)
                            raise
                        finally:
                            os.close(file_fd)
                        try:
                            os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                        except Exception:
                            os.unlink(temp_name, dir_fd=parent_fd)
                            raise
                        os.fsync(parent_fd)
                    actual = _read_relative_file(parent_fd, name, self.MAX_FILE_BYTES)
                    if actual != content:
                        raise ValueError(f"materialized content verification failed: {path}")
                finally:
                    os.close(parent_fd)
        finally:
            os.close(root_fd)

        from atg.ports.generation import MaterializationResult

        return MaterializationResult(
            workspace_identity=dict(workspace_identity),
            changed_files=changed_files,
            allowed_paths=allowed_paths,
            patch_digest=expected_patch_digest,
            materialized_test_digest=materialized_digest,
        )


def _read_relative_file(parent_fd: int, name: str, limit: int) -> bytes | None:
    """Read a regular file relative to an already-safe directory descriptor."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        file_fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ValueError("workspace target is not a regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_fd, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise ValueError("workspace base file exceeds size limit")
        return b"".join(chunks)
    finally:
        os.close(file_fd)
