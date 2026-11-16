from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping

from atg.domain.digests import require_sha256_digest


_ALLOWED_PATH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+\.(java|py|kt|ts|js|go)$")
_MAX_CHANGED_FILES = 10


def normalize_patch(patch: str) -> bytes:
    lines = patch.replace("\r\n", "\n").split("\n")
    normalized = [line.rstrip() for line in lines]
    while normalized and normalized[-1] == "":
        normalized.pop()
    return "\n".join(normalized).encode() + (b"\n" if normalized else b"")


def compute_patch_digest(patch: str) -> str:
    return "sha256:" + hashlib.sha256(normalize_patch(patch)).hexdigest()


def validate_changed_files(changed_files: object, allowed_roots: tuple[str, ...] = ("src/test/", "tests/")) -> None:
    if not isinstance(changed_files, list) or not changed_files:
        raise ValueError("changed_files must be a non-empty list")
    if len(changed_files) != len(set(changed_files)):
        raise ValueError("changed_files must contain unique entries")
    if len(changed_files) > _MAX_CHANGED_FILES:
        raise ValueError("changed_files exceeds maximum allowed")
    for path in changed_files:
        if not isinstance(path, str) or not path:
            raise ValueError("changed_files entry must be a non-empty string")
        if ".." in path or path.startswith("/") or path.startswith("\\"):
            raise ValueError(f"changed_files path traversal rejected: {path!r}")
        if not any(path.startswith(root) for root in allowed_roots):
            raise ValueError(f"changed_files outside allowed roots: {path!r}")
        if not _ALLOWED_PATH_PATTERN.fullmatch(path):
            raise ValueError(f"changed_files has unsupported extension: {path!r}")


def validate_generation_manifest(manifest: Mapping[str, object], plan_manifest: Mapping[str, object] | None = None) -> None:
    require_sha256_digest(manifest.get("plan_artifact_digest"), "plan_artifact_digest")
    if plan_manifest is not None:
        if manifest.get("plan_id") != plan_manifest.get("plan_id"):
            raise ValueError("generation plan_id does not match approved plan")
        if manifest.get("plan_revision") != plan_manifest.get("plan_revision"):
            raise ValueError("generation plan_revision does not match approved plan")
    if manifest.get("status") in {"generated", "verified"}:
        if not manifest.get("patch_digest"):
            raise ValueError("generated generation requires patch_digest")
        require_sha256_digest(manifest.get("patch_digest"), "patch_digest")
        validate_changed_files(manifest.get("changed_files"))
    expected = manifest.get("patch_digest")
    patch = manifest.get("patch_text")
    if expected is not None and isinstance(patch, str):
        computed = compute_patch_digest(patch)
        if computed.lower() != str(expected).lower():
            raise ValueError("patch_digest does not match patch content")


def generation_request_digest(generation_id: str, plan_id: str, run_id: str, expected_revision: int) -> str:
    payload = {"generation_id": generation_id, "plan_id": plan_id, "run_id": run_id, "expected_revision": expected_revision}
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
