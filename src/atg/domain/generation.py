"""Generation identities, path authorization, and artifact integrity helpers."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import difflib
from typing import Mapping

from atg.domain.digests import digests_equal, require_sha256_digest


_ALLOWED_PATH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+\.(java|py|kt|ts|js|go)$")
_DIFF_PATH_PATTERN = re.compile(r"^diff --git a/(.+) b/(.+)$")
_MAX_CHANGED_FILES = 10


def compute_patch_digest(patch: str) -> str:
    """Bind the exact UTF-8 patch bytes without semantic normalization."""
    return "sha256:" + hashlib.sha256(patch.encode()).hexdigest()


def compute_materialized_digest(files: Mapping[str, str]) -> str:
    """Bind the exact path-to-content mapping used by execution."""
    encoded = json.dumps(dict(files), sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def canonical_patch(base_files: Mapping[str, str], materialized_files: Mapping[str, str]) -> str:
    """Render the deterministic git-style patch for the exact file contents."""
    parts: list[str] = []
    for path in sorted(materialized_files):
        before = base_files.get(path)
        after = materialized_files[path]
        if before == after:
            continue
        created = before is None
        parts.append(f"diff --git a/{path} b/{path}\n")
        if created:
            parts.append("new file mode 100644\n")
        old_lines = before.splitlines(keepends=True) if before is not None else []
        new_lines = after.splitlines(keepends=True)
        from_name = "/dev/null" if created else f"a/{path}"
        to_name = f"b/{path}"
        for line in difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=from_name,
            tofile=to_name,
            n=3,
            lineterm="\n",
        ):
            parts.append(line)
            if line.startswith((" ", "+", "-")) and not line.endswith("\n"):
                parts.append("\n\\ No newline at end of file\n")
    return "".join(parts)


def derive_patch_paths(patch: str) -> tuple[str, ...]:
    """Return changed paths from canonical git diff headers."""
    paths: list[str] = []
    for line in patch.splitlines():
        match = _DIFF_PATH_PATTERN.fullmatch(line)
        if match is None:
            continue
        before, after = match.groups()
        if before != after:
            raise ValueError("renamed generated paths are not supported")
        paths.append(after)
    if not paths:
        raise ValueError("patch requires at least one git diff path")
    if len(paths) != len(set(paths)):
        raise ValueError("patch contains duplicate file sections")
    return tuple(paths)


def validate_changed_files(
    changed_files: object,
    allowed_roots: tuple[str, ...],
) -> tuple[str, ...]:
    """Validate repository-relative paths against plan-owned test roots."""
    if not isinstance(changed_files, (list, tuple)) or not changed_files:
        raise ValueError("changed_files must be a non-empty sequence")
    if not allowed_roots:
        raise ValueError("approved plan does not declare allowed paths")
    if len(changed_files) != len(set(changed_files)):
        raise ValueError("changed_files must contain unique entries")
    if len(changed_files) > _MAX_CHANGED_FILES:
        raise ValueError("changed_files exceeds maximum allowed")
    validated: list[str] = []
    for path in changed_files:
        if not isinstance(path, str) or not path:
            raise ValueError("changed_files entry must be a non-empty string")
        if (
            path.startswith(("/", "\\"))
            or "\\" in path
            or posixpath.normpath(path) != path
            or path.startswith("../")
        ):
            raise ValueError(f"changed_files path traversal rejected: {path!r}")
        if not path.startswith(allowed_roots):
            raise ValueError(f"changed_files outside approved plan paths: {path!r}")
        if not _ALLOWED_PATH_PATTERN.fullmatch(path):
            raise ValueError(f"changed_files has unsupported extension: {path!r}")
        validated.append(path)
    return tuple(validated)


def validate_candidate_paths(
    patch: str,
    materialized_files: Mapping[str, str],
    allowed_roots: tuple[str, ...],
    base_files: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Require patch headers, materialized files, and approved paths to agree."""
    patch_paths = derive_patch_paths(patch)
    materialized_paths = tuple(materialized_files)
    if set(patch_paths) != set(materialized_paths):
        raise ValueError("patch paths differ from materialized test files")
    if patch != canonical_patch(base_files or {}, materialized_files):
        raise ValueError("patch content differs from materialized test files")
    return validate_changed_files(materialized_paths, allowed_roots)


def validate_identity(identity: object, field: str) -> dict[str, object]:
    """Validate an attributable model, tool, worker, or subject identity."""
    if not isinstance(identity, Mapping):
        raise ValueError(f"{field} identity is required")
    if not isinstance(identity.get("kind"), str) or not identity["kind"]:
        raise ValueError(f"{field}.kind is required")
    if not isinstance(identity.get("id"), str) or not identity["id"]:
        raise ValueError(f"{field}.id is required")
    if identity.get("version") is not None and not isinstance(identity["version"], str):
        raise ValueError(f"{field}.version must be a string")
    if identity.get("digest") is not None:
        require_sha256_digest(identity["digest"], f"{field}.digest")
    return dict(identity)


def artifact_digest(content: object, media_type: str) -> tuple[str, int]:
    """Compute an artifact digest using its persisted representation."""
    if media_type == "text/x-diff":
        encoded = str(content).encode()
    else:
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest(), len(encoded)


def validate_generation_manifest(
    manifest: Mapping[str, object],
    plan_manifest: Mapping[str, object] | None = None,
) -> None:
    """Validate plan binding and successful materialized-test metadata."""
    require_sha256_digest(manifest.get("plan_artifact_digest"), "plan_artifact_digest")
    if plan_manifest is not None:
        if manifest.get("plan_id") != plan_manifest.get("plan_id"):
            raise ValueError("generation plan_id does not match approved plan")
        if manifest.get("plan_revision") != plan_manifest.get("plan_revision"):
            raise ValueError("generation plan_revision does not match approved plan")
    if manifest.get("status") in {"generated", "verified"}:
        if plan_manifest is not None:
            validate_changed_files(
                manifest.get("changed_files", []),
                tuple(plan_manifest.get("allowed_paths", [])),
            )
        require_sha256_digest(manifest.get("patch_digest"), "patch_digest")
        require_sha256_digest(
            manifest.get("materialized_test_digest"), "materialized_test_digest"
        )
        if not manifest.get("attempts") or not manifest.get("artifacts"):
            raise ValueError("successful generation requires attempts and artifacts")
    subjects = manifest.get("subject_materializations", [])
    expected_subjects = {json.dumps(manifest.get(field), sort_keys=True) for field in ("base", "candidate")}
    actual_subjects = {
        json.dumps(item.get("subject"), sort_keys=True)
        for item in subjects
        if isinstance(item, Mapping)
    }
    if actual_subjects != expected_subjects or any(
        not digests_equal(item.get("test_digest"), manifest.get("materialized_test_digest"))
        for item in subjects
        if isinstance(item, Mapping)
    ):
        raise ValueError("generation subject materialization digests do not match base and candidate")
    attempts = manifest.get("attempts", [])
    if attempts:
        provenance_fields = (
            "model", "model_endpoint", "tool", "worker", "prompt_digest",
            "config_digest", "invocation_digest",
        )
        if any(manifest.get(field) != attempts[-1].get(field) for field in provenance_fields):
            raise ValueError("generation-level invocation provenance differs from final attempt")

    handoff = manifest.get("materialization_handoff")
    if handoff is not None:
        if not isinstance(handoff, Mapping) or manifest.get("status") not in {"generated", "verified"}:
            raise ValueError("materialization handoff requires a successful generation")
        if handoff.get("status") != "ready":
            raise ValueError("materialization handoff status is invalid")
        for field in (
            "generation_id", "run_id", "plan_id", "plan_revision", "plan_artifact_digest",
            "plan_checkpoint_id", "plan_decision_id",
        ):
            if handoff.get(field) != manifest.get(field):
                raise ValueError(f"materialization handoff {field} differs from generation")
        if (
            not digests_equal(handoff.get("patch_digest"), manifest.get("patch_digest"))
            or not digests_equal(
                handoff.get("materialized_test_digest"),
                manifest.get("materialized_test_digest"),
            )
        ):
            raise ValueError("materialization handoff digests differ from generation")
        allowed_paths = tuple(handoff.get("allowed_paths", []))
        changed_files = validate_changed_files(handoff.get("changed_files", []), allowed_paths)
        if tuple(manifest.get("changed_files", [])) != changed_files:
            raise ValueError("materialization handoff paths differ from generation")
        if plan_manifest is not None and allowed_paths != tuple(plan_manifest.get("allowed_paths", [])):
            raise ValueError("materialization handoff paths differ from approved plan")
        expected_subjects = [
            {"subject": manifest.get("base"), "test_digest": manifest.get("materialized_test_digest")},
            {"subject": manifest.get("candidate"), "test_digest": manifest.get("materialized_test_digest")},
        ]
        if list(handoff.get("subjects", [])) != expected_subjects:
            raise ValueError("materialization handoff subjects differ from generation")
        if attempts and (
            handoff.get("patch_artifact_id") != attempts[-1].get("patch_artifact_id")
            or handoff.get("materialized_artifact_id") != attempts[-1].get("materialized_artifact_id")
            or handoff.get("base_artifact_id") != attempts[-1].get("base_artifact_id")
        ):
            raise ValueError("materialization handoff artifact references differ from final attempt")


def validate_artifact_bundle(
    manifest: Mapping[str, object], artifacts: tuple[Mapping[str, object], ...]
) -> None:
    """Recompute every persisted artifact and close manifest references."""
    metadata_by_id: dict[str, Mapping[str, object]] = {}
    content_by_id: dict[str, object] = {}
    for stored in artifacts:
        metadata = stored.get("artifact")
        if not isinstance(metadata, Mapping):
            raise ValueError("generation artifact metadata is missing")
        artifact_id = metadata.get("artifact_id")
        if not isinstance(artifact_id, str) or artifact_id in metadata_by_id:
            raise ValueError("generation artifact IDs must be unique")
        digest, size = artifact_digest(stored.get("content"), str(metadata.get("media_type")))
        if not digests_equal(metadata.get("digest"), digest) or metadata.get("size_bytes") != size:
            raise ValueError("generation artifact metadata mismatch")
        metadata_by_id[artifact_id] = metadata
        content_by_id[artifact_id] = stored.get("content")
    if list(manifest.get("artifacts", [])) != list(metadata_by_id.values()):
        raise ValueError("generation artifact references are not closed")
    attempts = manifest.get("attempts", [])
    for attempt in attempts:
        if attempt.get("patch_artifact_id") not in metadata_by_id:
            raise ValueError("generation attempt patch artifact is missing")
        if attempt.get("materialized_artifact_id") not in metadata_by_id:
            raise ValueError("generation attempt materialized artifact is missing")
        if attempt.get("base_artifact_id") not in metadata_by_id:
            raise ValueError("generation attempt base-files artifact is missing")
        patch_id = attempt["patch_artifact_id"]
        materialized_id = attempt["materialized_artifact_id"]
        base_id = attempt["base_artifact_id"]
        patch = content_by_id[patch_id]
        materialized = content_by_id[materialized_id]
        base = content_by_id[base_id]
        if (
            metadata_by_id[patch_id].get("media_type") != "text/x-diff"
            or metadata_by_id[materialized_id].get("media_type")
            != "application/vnd.atg.materialized-tests+json"
            or metadata_by_id[base_id].get("media_type") != "application/vnd.atg.base-files+json"
            or not isinstance(patch, str)
            or not isinstance(materialized, Mapping)
            or not isinstance(base, Mapping)
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in materialized.items())
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in base.items())
        ):
            raise ValueError("generation attempt artifacts have invalid content or media types")
        path_check_passed = any(
            check.get("name") == "approved_path" and check.get("status") == "pass"
            for check in attempt.get("checks", [])
        )
        if path_check_passed and patch != canonical_patch(base, materialized):
            raise ValueError("generation attempt patch does not match base and materialized artifacts")
        if not digests_equal(compute_patch_digest(patch), attempt.get("output_digest")):
            raise ValueError("generation attempt output digest differs from patch artifact")
    if attempts:
        final = attempts[-1]
        patch = metadata_by_id[final["patch_artifact_id"]]
        materialized = metadata_by_id[final["materialized_artifact_id"]]
        if not digests_equal(patch.get("digest"), manifest.get("patch_digest")):
            raise ValueError("generation patch digest differs from final attempt")
        if not digests_equal(
            materialized.get("digest"), manifest.get("materialized_test_digest")
        ):
            raise ValueError("generation materialized digest differs from final attempt")
