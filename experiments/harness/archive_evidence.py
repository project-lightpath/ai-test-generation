"""Export a scanned FLPATH-5050 run without its Maven workspaces.

The source run must be quiescent. The runner's passed evidence scan is the
credential gate; this tool selects and hashes evidence but does not rescan it.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

SCHEMA = "flpath-5050.evidence-archive.v1"
MAX_FILES = 300
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
STAGES = ("analysis", "plan", "code", "execute")
COVERAGE_CASES = ("baseline", "mutant", "coverage-control", "coverage-candidate")


def checked_regular(root: Path, relative: Path) -> Optional[Path]:
    """Reject linked or special selected evidence, including parent components."""
    current = root
    for part in relative.parts:
        if part in ("", ".", ".."):
            raise ValueError(f"unsafe evidence path: {relative}")
        current = current / part
        if not current.exists() and not current.is_symlink():
            return None
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"linked evidence: {relative}")
        if current != root / relative and not stat.S_ISDIR(mode):
            raise ValueError(f"non-directory evidence parent: {relative}")
    if not stat.S_ISREG(current.lstat().st_mode):
        raise ValueError(f"special evidence file: {relative}")
    return current


def read_record(root: Path, relative: Path, required: bool = False) -> Optional[Dict[str, object]]:
    source = checked_regular(root, relative)
    if source is None:
        if required:
            raise ValueError(f"missing evidence: {relative}")
        return None
    if source.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"oversized evidence: {relative}")
    value = json.loads(source.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {relative}")
    return value


def candidate_paths(root: Path, name: str, outcome: str) -> List[Path]:
    folder = root / name
    if not folder.is_dir() or folder.is_symlink():
        raise ValueError(f"missing or linked candidate directory: {name}")
    paths = []
    for item in folder.iterdir():
        if item.is_symlink():
            raise ValueError(f"linked candidate entry: {name}/{item.name}")
        if item.is_file() and item.suffix in (".json", ".log"):
            paths.append(Path(name, item.name))
        elif not item.is_dir() and not item.is_file():
            raise ValueError(f"special candidate entry: {name}/{item.name}")
    evaluation_dir = folder / "evaluation"
    if evaluation_dir.is_symlink():
        raise ValueError(f"linked evaluation directory: {name}")
    for item in evaluation_dir.iterdir() if evaluation_dir.is_dir() else ():
        if item.is_symlink():
            raise ValueError(f"linked evaluation entry: {name}/{item.name}")
        if item.is_file() and item.suffix in (".json", ".log"):
            paths.append(Path(name, "evaluation", item.name))
    for item in ("invocation.json", "result.json", "sanitized-config.json"):
        paths.append(Path(name, "generated", item))
    evaluation = read_record(root, Path(name, "evaluation", "evaluation.json"))
    if evaluation and isinstance(evaluation.get("test"), dict):
        relative = evaluation["test"].get("path")
        if not isinstance(relative, str):
            raise ValueError(f"invalid generated test path: {name}")
        test_path = PurePosixPath(relative)
        if (test_path.is_absolute() or ".." in test_path.parts
                or test_path.parts[:3] != ("src", "test", "java")
                or test_path.suffix != ".java"):
            raise ValueError(f"unsafe generated test path: {name}")
        located = [Path(name, workspace, *test_path.parts) for workspace in ("generated", "workspace")
                   if checked_regular(root, Path(name, workspace, *test_path.parts))]
        if not located and outcome == "discriminates":
            raise ValueError(f"successful candidate lacks generated test: {name}")
        paths.extend(located)
    for case in COVERAGE_CASES:
        paths.append(Path(name, "evaluation", case, "target", "site", "jacoco", "jacoco.xml"))
    return paths


def selected_paths(root: Path) -> Tuple[List[Path], Dict[str, object]]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("run root must be a real directory")
    summary = read_record(root, Path("summary.json"), required=True)
    if summary.get("evidence_scan") != "passed":
        raise ValueError("run lacks a passed credential evidence scan")
    candidates = summary.get("candidate_results")
    if not isinstance(candidates, dict) or not candidates:
        raise ValueError("run lacks candidate results")
    paths = [Path(item) for item in ("summary.json", "prepare.json", "run-spec.json",
                                    "replay-manifest.json")]
    for name, outcome in candidates.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            raise ValueError(f"invalid candidate name: {name}")
        if not isinstance(outcome, str):
            raise ValueError(f"invalid candidate outcome: {name}")
        paths.extend(candidate_paths(root, name, outcome))
        paths.extend(Path("checkpoints", name, f"{stage}.json") for stage in STAGES)
    for required in ("prepare.json", "run-spec.json"):
        read_record(root, Path(required), required=True)
    return sorted(set(paths), key=str), summary


def copy_hashed(source: Path, destination: Path) -> Tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    checksum = hashlib.sha256()
    count = 0
    try:
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError(f"special evidence file: {source}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as target:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    count += len(chunk)
                    if count > MAX_FILE_BYTES:
                        raise ValueError(f"oversized evidence: {source}")
                    checksum.update(chunk)
                    target.write(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return count, "sha256:" + checksum.hexdigest()


def archive(run_root: Path, output_dir: Path) -> Path:
    if run_root.is_symlink() or output_dir.exists() or output_dir.is_symlink():
        raise ValueError("source must be real and output directory must be new")
    parent = output_dir.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("output parent must be an existing real directory")
    root = run_root.resolve(strict=True)
    paths, summary = selected_paths(root)
    if output_dir.resolve() == root or root in output_dir.resolve().parents:
        raise ValueError("output must be outside the run root")
    staging = Path(tempfile.mkdtemp(prefix=".evidence-export-", dir=parent))
    files: Dict[str, Dict[str, object]] = {}
    total = 0
    try:
        for relative in paths:
            source = checked_regular(root, relative)
            if source is None:
                continue
            if len(files) >= MAX_FILES:
                raise ValueError("evidence file count limit exceeded")
            size, digest = copy_hashed(source, staging / relative)
            total += size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("evidence total size limit exceeded")
            files[relative.as_posix()] = {"bytes": size, "sha256": digest}
        manifest = {"schema": SCHEMA, "run_id": summary.get("run_id"),
                    "evidence_scan": "passed", "file_count": len(files),
                    "total_bytes": total, "files": files,
                    "excluded": ["Maven workspaces and build outputs except JaCoCo XML",
                                 "other generated files outside the evaluated Java test"]}
        (staging / "archive-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        staging.rename(output_dir)
    except BaseException:
        shutil.rmtree(staging)
        raise
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    print(archive(args.run_root, args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
