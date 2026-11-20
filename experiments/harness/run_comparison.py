"""Repeat the FLPATH-5050 four-candidate experiment on a Linux host.

Use --check-only before a live run. The live run reads one EnMaaS key from stdin.
All runtime paths are explicit; this script never installs tools or downloads images.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
import tempfile
import uuid
from pathlib import Path
from typing import BinaryIO, Dict, List, Optional, Tuple

from candidate_evaluation import FIXTURE_DIGEST, SHARED_OBJECTIVE, _run_maven_phase, copy_fixture, test_target, tree_digest
from fixture_profiles import FixtureProfile, PROFILES, QUARKUS, get_profile

ROOT = Path(__file__).resolve().parent
MODEL = "rits/zai-org/glm-5-3"
ENDPOINT = "enmaas-devshift"
OPENAI_URL = "https://api.enmaas.devshift.net/v1"
ANTHROPIC_URL = "https://api.enmaas.devshift.net"
CLAUDE_BINARY_DIGEST = "sha256:24972e3bc859fab2b46ed4c1e51f7d6130f06d3bd550811a114640de3370d0de"
CLAUDE_CONTAINER_PATH = "/opt/claude/node_modules"
CLAUDE_CONTAINER_BIN = CLAUDE_CONTAINER_PATH + "/.bin/claude"
CLAUDE_CONTAINER_PATH_ENV = CLAUDE_CONTAINER_PATH + "/node/bin:/usr/local/bin:/usr/bin:/bin"
PYTHON_IMAGE = "docker.io/library/python:3.11-slim"
PYTHON_IMAGE_ID = "896edfbcbf0c29ad15681617a8886cb9871b7f24c5fde4606114c8ee6bfe4c70"
AIDER_IMAGE = "quay.io/sscdemo/ai-agent-maven-aider:v1.0.1"
AIDER_IMAGE_ID = "80d5a7e0cbd074f9b76c7f0db7441404521055268e3ba7d04df5d1bd2e91b456"
MAVEN_IMAGE = "docker.io/library/maven:3.9.11-eclipse-temurin-21"
MAVEN_IMAGE_ID = "b9611ad9b2f6ebef1c9369a32d794718fc28ebe2b9bb37b81a64b731370907bd"
OBJECTIVE_DIGEST = "b433ea91f94e46ab00a36f25a435b8471ebad93ce8d0113651678be49c1e3c02"
SOURCE_PATHS = (
    "src/main/java/com/projectlightpath/fixture/api/TransferResource.java",
    "src/main/java/com/projectlightpath/fixture/service/DefaultTransferService.java",
    "src/main/java/com/projectlightpath/fixture/target/UserAuthPublicKey.java",
    "src/test/java/com/projectlightpath/fixture/TransferResourceTest.java",
)
EXISTING_TEST = SOURCE_PATHS[-1]
CLAUDE_PROMPT = (
    "Read objective.md and the four Java source files in this workspace. "
    "Follow the objective exactly: create one new native Java test under src/test/java. "
    "Do not change existing files or run commands. Return the new path."
)
VERSIONS = {"aider": "0.86.2", "pydantic-ai": "2.68.0",
            "claude-code": "2.1.296", "langgraph": "1.0.8"}
MAX_LOG_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_MOUNT_BYTES = 8 * 1024 * 1024 * 1024
MAX_CANDIDATE_JSON_BYTES = 1024 * 1024
MAX_CANDIDATE_TEST_BYTES = 256 * 1024
MAX_CANDIDATE_WORKSPACE_ENTRIES = 512
MAX_EVIDENCE_ENTRIES = 10000
MAX_EVIDENCE_FILE_BYTES = 256 * 1024 * 1024
GENERATION_GUARDS = ["--cap-drop=all", "--security-opt=no-new-privileges",
                     "--pids-limit=256", "--memory=2g", "--cpus=2"]
AIDER_METADATA = frozenset((".aider.chat.history.md", ".aider.input.history"))

RUN_SPEC_SCHEMA = "flpath-5050.run-spec.v1"
RUN_SPEC_KEYS = frozenset(("schema", "profile", "candidates", "model_id",
                           "endpoint_id", "timeouts_seconds", "external_candidates"))


def external_candidate(args: argparse.Namespace, name: str) -> Optional[Dict[str, str]]:
    spec = getattr(args, "run_spec_data", None)
    if spec:
        for candidate in spec["external_candidates"]:
            if candidate["name"] == name:
                return candidate
    return None


def tool_version(args: argparse.Namespace, name: str) -> str:
    external = external_candidate(args, name)
    return external["version"] if external else VERSIONS[name]


def validate_external_candidates(value: object) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("external candidates must be a list")
    names = set()
    for candidate in value:
        if not isinstance(candidate, dict) or set(candidate) != {"name", "version", "image", "image_id"}:
            raise ValueError("external candidate fields do not match v1 schema")
        name, version = candidate["name"], candidate["version"]
        image, image_id = candidate["image"], candidate["image_id"]
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", name)
                or name in VERSIONS or name in names):
            raise ValueError("external candidate name must be unique and not built in")
        if not isinstance(version, str) or not 1 <= len(version) <= 128 or "\n" in version:
            raise ValueError("external candidate needs a bounded version")
        if (not isinstance(image, str)
                or not re.fullmatch(r"[a-zA-Z0-9._:/-]+@sha256:[0-9a-f]{64}", image)
                or not isinstance(image_id, str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)):
            raise ValueError("external candidate image and image ID must be digest-pinned")
        names.add(name)
    return value


def load_run_spec(path: Path) -> Dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("run specification must be a regular file")
    record = json.loads(path.read_text())
    if not isinstance(record, dict) or set(record) != RUN_SPEC_KEYS:
        raise ValueError("run specification fields do not match v1 schema")
    if record["schema"] != RUN_SPEC_SCHEMA or record["profile"] not in PROFILES:
        raise ValueError("run specification schema or fixture profile is unsupported")
    external = validate_external_candidates(record["external_candidates"])
    known = set(VERSIONS) | {candidate["name"] for candidate in external}
    candidates = record["candidates"]
    if (not isinstance(candidates, list) or not candidates
            or any(not isinstance(name, str) or name not in known for name in candidates)
            or len(set(candidates)) != len(candidates)):
        raise ValueError("run specification needs unique known candidates")
    if record["model_id"] != MODEL or record["endpoint_id"] != ENDPOINT:
        raise ValueError("run specification model and endpoint must match pinned runtime")
    budgets = record["timeouts_seconds"]
    if (not isinstance(budgets, dict) or set(budgets) != {"analysis", "plan", "code", "execute"}
            or any(type(value) is not int or value < 1 or value > 7200
                   for value in budgets.values())):
        raise ValueError("run specification needs bounded stage timeouts")
    return record


def run_timeout(args: argparse.Namespace, candidate: str, stage: str,
                profile: FixtureProfile) -> int:
    spec = getattr(args, "run_spec_data", None)
    if spec:
        return spec["timeouts_seconds"][stage]
    if stage == "execute":
        return 3900
    if stage in ("analysis", "plan"):
        return 300 if candidate == "aider" else 360
    return generation_timeout(candidate, profile)



def sha256_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def create_host_output(path: Path):
    """Create a new host output without following a candidate-planted link."""
    directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
    finally:
        os.close(directory)
    return os.fdopen(descriptor, "wb")


def write_host_bytes(path: Path, content: bytes, owner: Optional[int] = None) -> None:
    with create_host_output(path) as output:
        if owner is not None:
            os.fchown(output.fileno(), owner, owner)
        output.write(content)


def write_json(path: Path, value: Dict[str, object]) -> None:
    write_host_bytes(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


def write_stage_checkpoint(args: argparse.Namespace, candidate: str, folder: Path,
                           stage: str, status: str, outputs: List[Path],
                           result: Optional[str] = None) -> None:
    """Save a stage result once; downstream stages cannot rewrite its evidence."""
    profile = get_profile(getattr(args, "profile", "quarkus"))
    # Adapters mount the candidate folder read-write; checkpoints stay outside it.
    output_root = getattr(args, "output_root", folder.parent)
    checkpoint_dir = output_root / "checkpoints" / candidate
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    previous = {"plan": "analysis", "code": "plan", "execute": "code"}.get(stage)
    previous_file = checkpoint_dir / (previous + ".json") if previous else None
    records = {}
    for output in outputs:
        require_regular_output(folder, output)
        if output.lstat().st_nlink != 1:
            raise ValueError("stage evidence has hard links: " + str(output.relative_to(folder)))
        records[str(output.relative_to(folder))] = sha256_file(output)
    record = {
        "schema": "flpath-5050.stage-checkpoint.v1",
        "candidate": candidate, "stage": stage, "status": status, "result": result,
        "fixture_tree_sha256": profile.tree_sha256,
        "objective_sha256": profile.objective_sha256,
        "model_id": MODEL, "endpoint_id": ENDPOINT,
        "run_spec_sha256": (sha256_file(output_root / "run-spec.json")
                            if getattr(args, "run_spec_data", None) else None),
        "previous_checkpoint_sha256": (sha256_file(previous_file)
                                        if previous_file and previous_file.is_file() else None),
        "outputs": records,
    }
    path = checkpoint_dir / (stage + ".json")
    write_json(path, record)


def record_blocked_checkpoint(args: argparse.Namespace, candidate: str,
                              folder: Path) -> None:
    for stage, marker in (("execute", "evaluation-run.json"),
                          ("code", "generation.json"), ("plan", "plan-run.json"),
                          ("analysis", "analysis-run.json")):
        output_root = getattr(args, "output_root", folder.parent)
        checkpoint = output_root / "checkpoints" / candidate / (stage + ".json")
        if (folder / marker).is_file() and not checkpoint.exists():
            write_stage_checkpoint(args, candidate, folder, stage, "blocked", [])
            return


def verify_stage_checkpoints(args: argparse.Namespace, candidate: str, folder: Path,
                             stages: List[str]) -> None:
    """Verify host-owned checkpoints and their candidate-writable outputs."""
    checkpoint_dir = args.output_root / "checkpoints" / candidate
    previous_hash = None
    for stage in stages:
        checkpoint = checkpoint_dir / (stage + ".json")
        record = json.loads(bounded_candidate_bytes(
            checkpoint_dir, checkpoint, MAX_CANDIDATE_JSON_BYTES))
        if (not isinstance(record, dict)
                or record.get("schema") != "flpath-5050.stage-checkpoint.v1"
                or record.get("candidate") != candidate or record.get("stage") != stage
                or record.get("status") != "completed"
                or record.get("previous_checkpoint_sha256") != previous_hash):
            raise ValueError("stage checkpoint chain invalid: " + stage)
        outputs = record.get("outputs")
        if not isinstance(outputs, dict) or not outputs:
            raise ValueError("stage checkpoint outputs missing: " + stage)
        for relative, expected in outputs.items():
            if not isinstance(relative, str):
                raise ValueError("stage checkpoint output path invalid: " + stage)
            path = Path(relative)
            if path.is_absolute() or not path.parts or ".." in path.parts:
                raise ValueError("stage checkpoint output path invalid: " + stage)
            target = folder / path
            require_regular_output(folder, target)
            if target.lstat().st_nlink != 1:
                raise ValueError("stage evidence has hard links: " + relative)
            actual = sha256_bytes(bounded_candidate_bytes(
                folder, target, MAX_LOG_BYTES + 64))
            if not isinstance(expected, str) or actual != expected:
                raise ValueError("stage evidence changed after " + stage + ": " + relative)
        previous_hash = sha256_file(checkpoint)


def verify_workspace_stages(folder: Path, workspace: Path, stages: List[str]) -> None:
    """Validate tool workspace copies before they are used as read-only inputs."""
    for stage in stages:
        filename = stage + ".json"
        source = workspace / filename
        require_regular_output(workspace, source)
        if source.lstat().st_nlink != 1:
            raise ValueError("workspace stage evidence has hard links: " + filename)
        actual = bounded_candidate_bytes(workspace, source, MAX_CANDIDATE_JSON_BYTES)
        expected = bounded_candidate_bytes(folder, folder / filename, MAX_CANDIDATE_JSON_BYTES)
        if actual != expected:
            raise ValueError("workspace stage evidence changed: " + filename)


def generation_checkpoints(profile: FixtureProfile) -> List[str]:
    return ["analysis", "plan", "code"] if profile.staged else ["code"]


def safe_environment() -> Dict[str, str]:
    allowed = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
               "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
    return {name: os.environ[name] for name in allowed if name in os.environ}


def image_id(image: str) -> str:
    value = subprocess.check_output(
        ["podman", "image", "inspect", "--format", "{{.Id}}", image],
        universal_newlines=True).strip()
    return value[7:] if value.startswith("sha256:") else value


def container_version(venv: Path, package: str) -> str:
    command = ["podman", "run", "--rm", "--pull=never", "--network=none",
               "-v", str(venv) + ":/venv:ro,Z", PYTHON_IMAGE, "/venv/bin/python",
               "-c", "import importlib.metadata; print(importlib.metadata.version('" + package + "'))"]
    return subprocess.check_output(command, universal_newlines=True).strip()



def offline_maven_readiness(args: argparse.Namespace, profile: FixtureProfile) -> None:
    """Prove the selected cache can build and test the pinned fixture without network."""
    with tempfile.TemporaryDirectory(prefix="flpath-5050-preflight-",
                                     dir=str(args.fixture_root.parent)) as temporary:
        workspace = Path(temporary) / "fixture"
        copy_fixture(args.fixture_root, workspace)
        result = _run_maven_phase(workspace, "preflight", "verify", args.maven_cache,
                                  profile.maven_image, "", profile)
        if result["status"] == "passed":
            return
        detail = " ".join(Path(result["log"]).read_text(errors="replace")[-1200:].split())
        raise ValueError(f"offline Maven verify failed for {profile.name} "
                         f"({result['status']}, exit {result['exit_code']}): {detail}")


def preflight(args: argparse.Namespace, candidates: List[str]) -> Dict[str, str]:
    profile = get_profile(getattr(args, "profile", "quarkus"))
    if args.output_root.exists():
        raise ValueError("output root must be new")
    if tree_digest(args.fixture_root) != profile.tree_sha256:
        raise ValueError("fixture tree differs from pinned " + profile.name + " revision")
    if not profile.objective.is_file() or sha256_file(profile.objective) != profile.objective_sha256:
        raise ValueError("shared objective differs from pinned " + profile.name + " text")
    if not args.maven_cache.is_dir():
        raise ValueError("offline Maven cache is missing")
    for name, image, expected in (("python", PYTHON_IMAGE, PYTHON_IMAGE_ID),
                                  ("maven", profile.maven_image, profile.maven_image_id)):
        if image_id(image) != expected:
            raise ValueError(name + " image identity differs from pinned run")
    offline_maven_readiness(args, profile)
    versions = {}
    if "aider" in candidates:
        if os.geteuid() != 0 or image_id(AIDER_IMAGE) != AIDER_IMAGE_ID:
            raise ValueError("Aider requires root and the pinned image")
        command = ["podman", "run", "--rm", "--pull=never", "--network=none",
                   AIDER_IMAGE, "aider", "--version"]
        if VERSIONS["aider"] not in subprocess.check_output(command, universal_newlines=True):
            raise ValueError("Aider version differs from pinned run")
        versions["aider"] = VERSIONS["aider"]
    for name, package, venv in (("pydantic-ai", "pydantic-ai-slim", args.pydantic_venv),
                                ("langgraph", "langgraph", args.langgraph_venv)):
        if name in candidates:
            adapter = ROOT / "candidates" / ("pydantic_ai" if name == "pydantic-ai" else name) / "adapter.py"
            if not adapter.is_file():
                raise ValueError(name + " adapter is missing from checkout")
            if not venv or not venv.is_dir() or container_version(venv, package) != VERSIONS[name]:
                raise ValueError(name + " runtime differs from pinned version")
            versions[name] = VERSIONS[name]
    if "claude-code" in candidates:
        versions["claude-code"] = check_claude_runtime(args.claude_runtime_dir)
    for name in candidates:
        external = external_candidate(args, name)
        if external:
            if image_id(external["image"]) != external["image_id"][7:]:
                raise ValueError(name + " image identity differs from pinned run spec")
            versions[name] = external["version"]
    return versions


def output_mount_capacity(output_root: Path) -> int:
    mount = output_root.parent.resolve()
    if not mount.is_dir() or mount == Path("/") or not os.path.ismount(str(mount)):
        raise ValueError("live output parent must be a dedicated mounted filesystem")
    stats = os.statvfs(str(mount))
    capacity = stats.f_blocks * stats.f_frsize
    if capacity <= 0 or capacity > MAX_OUTPUT_MOUNT_BYTES:
        raise ValueError("live output mount must have a total capacity of at most 8 GiB")
    return capacity


def file_contains_key(path: Path, secret: bytes) -> bool:
    tail = b""
    with path.open("rb") as source:
        for block in iter(lambda: source.read(65536), b""):
            window = tail + block
            if secret in window:
                return True
            tail = window[-(len(secret) - 1):] if len(secret) > 1 else b""
    return False


def inspect_output_file(path: Path, key: str, secret: bytes) -> Tuple[str, Optional[OSError]]:
    if path.is_symlink():
        path.unlink()
        return "symlink", None
    if key in path.name:
        path.unlink()
        return "credential_path", None
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        path.unlink()
        return "nonregular", None
    if metadata.st_size > MAX_EVIDENCE_FILE_BYTES:
        path.unlink()
        return "oversized", None
    try:
        contains_key = file_contains_key(path, secret)
    except OSError as error:
        path.unlink()
        return "unreadable", error
    if contains_key:
        path.unlink()
        return "credential_file", None
    return "clean", None


def scan_evidence_for_key(root: Path, key: str) -> int:
    if root.is_symlink():
        raise ValueError("output root is a symlink")
    bounded_entries(root, MAX_EVIDENCE_ENTRIES)
    counts = Counter()
    read_error = None
    walk_errors = []
    secret = key.encode()
    for current, directories, files in os.walk(str(root), onerror=walk_errors.append,
                                               followlinks=False):
        for name in list(directories):
            path = Path(current) / name
            if path.is_symlink() or key in name:
                was_symlink = path.is_symlink()
                if was_symlink:
                    path.unlink()
                else:
                    shutil.rmtree(str(path))
                directories.remove(name)
                counts["symlink" if was_symlink else "credential_path"] += 1
        for name in files:
            result, error = inspect_output_file(Path(current) / name, key, secret)
            counts[result] += 1
            read_error = error or read_error
    credential_count = counts["credential_path"] + counts["credential_file"]
    if counts["credential_path"]:
        raise ValueError(f"credential found in output path ({credential_count} entries)")
    if counts["credential_file"]:
        raise ValueError(f"credential found in output ({credential_count} entries)")
    if counts["symlink"]:
        raise ValueError(f"symlink in output ({counts['symlink']} entries)")
    if counts["nonregular"]:
        raise ValueError(f"nonregular output ({counts['nonregular']} entries)")
    if counts["oversized"]:
        raise ValueError(f"oversized output ({counts['oversized']} entries)")
    if read_error:
        raise read_error
    if walk_errors:
        raise walk_errors[0]
    return counts["clean"]


def bounded_entries(root: Path, max_entries: int, max_depth: int = 32) -> List[Path]:
    """Enumerate candidate trees lazily so a huge directory cannot exhaust memory."""
    result = []
    stack = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > max_depth:
            raise ValueError("candidate output directory depth limit exceeded")
        with os.scandir(str(directory)) as entries:
            for entry in entries:
                path = Path(entry.path)
                result.append(path)
                if len(result) > max_entries:
                    raise ValueError("candidate output entry limit exceeded")
                if entry.is_dir(follow_symlinks=False):
                    stack.append((path, depth + 1))
    return result


def bounded_candidate_bytes(root: Path, path: Path, limit: int) -> bytes:
    require_regular_output(root, path)
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise ValueError("candidate output file size limit exceeded")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(limit + 1)
        if len(content) > limit:
            raise ValueError("candidate output file size limit exceeded")
        return content
    finally:
        os.close(descriptor)


def require_regular_output(root: Path, path: Path) -> None:
    """Reject linked or special candidate output before any host read or copy."""
    base = Path(os.path.abspath(str(root)))
    target = Path(os.path.abspath(str(path)))
    try:
        parts = target.relative_to(base).parts
    except ValueError as error:
        raise ValueError("candidate output escapes its directory") from error
    if not parts or not stat.S_ISDIR(base.lstat().st_mode):
        raise ValueError("candidate output root is not a real directory")
    current = base
    for part in parts[:-1]:
        current = current / part
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise ValueError("candidate output has a linked or non-directory parent")
    if not stat.S_ISREG((current / parts[-1]).lstat().st_mode):
        raise ValueError("candidate output is linked or nonregular")


def checkout_identity() -> Dict[str, object]:
    checkout = ROOT.parents[1]
    revision = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        universal_newlines=True).strip()
    status = subprocess.check_output(
        ["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=all"],
        universal_newlines=True)
    return {"atg_checkout_head": revision, "atg_checkout_dirty": bool(status)}


def claude_container_base(runtime: Path, network: str) -> List[str]:
    return ["podman", "run", "--rm", "--pull=never", "--network=" + network,
            "--user", "1001:1001", "--read-only"] + GENERATION_GUARDS + [
            "--tmpfs", "/tmp:rw,nosuid,size=64m",
            "-v", str(runtime) + ":" + CLAUDE_CONTAINER_PATH + ":ro,Z",
            "-e", "PATH=" + CLAUDE_CONTAINER_PATH_ENV]


def check_claude_runtime(runtime: Optional[Path]) -> str:
    if not runtime or not runtime.is_dir():
        raise ValueError("Claude runtime directory is missing")
    binary = runtime / ".bin/claude"
    if not binary.is_file() or sha256_file(binary) != CLAUDE_BINARY_DIGEST:
        raise ValueError("Claude Code binary differs from pinned run")
    command = claude_container_base(runtime, "none") + [PYTHON_IMAGE,
               CLAUDE_CONTAINER_PATH + "/node/bin/node", "--version"]
    if subprocess.check_output(command).strip() != b"v22.23.5":
        raise ValueError("Claude Node runtime differs from pinned version")
    command = claude_container_base(runtime, "none") + [PYTHON_IMAGE,
               CLAUDE_CONTAINER_BIN, "--version"]
    if VERSIONS["claude-code"] not in subprocess.check_output(command, universal_newlines=True):
        raise ValueError("Claude Code version differs from pinned run")
    return VERSIONS["claude-code"]


def stop_process(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def write_output_limit(sink: BinaryIO, content: bytes) -> None:
    """Keep the marker within the same byte limit accepted by the exporter."""
    marker = b"\n[OUTPUT_LIMIT]\n"[:MAX_LOG_BYTES]
    payload_limit = MAX_LOG_BYTES - len(marker)
    if sink.tell() > payload_limit:
        sink.seek(payload_limit)
        sink.truncate()
    else:
        sink.write(content[:payload_limit - sink.tell()])
    sink.write(marker)


def run_bounded(command: List[str], env: Dict[str, str], cwd: Optional[Path],
                log: Path, key: str, timeout: int) -> Tuple[str, Optional[int], float]:
    start = time.monotonic()
    # Candidate containers cannot access this sibling of the candidate folder.
    control = log.parent.parent / ".runner-logs" / log.parent.name
    control.mkdir(parents=True, exist_ok=True)
    host_log = control / log.name
    with create_host_output(host_log) as sink:
        process = subprocess.Popen(command, cwd=str(cwd) if cwd else None, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        secret, pending, status = key.encode(), b"", "completed"
        overlap = max(0, len(secret) - 1)
        try:
            while True:
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    status = "timeout"
                    break
                if not selector.select(remaining):
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                combined = (pending + chunk).replace(secret, b"[REDACTED]")
                safe = combined[:-overlap] if overlap else combined
                pending = combined[-overlap:] if overlap else b""
                if sink.tell() + len(safe) > MAX_LOG_BYTES:
                    write_output_limit(sink, safe)
                    status = "output_limit"
                    break
                sink.write(safe)
            if status == "completed":
                if sink.tell() + len(pending) > MAX_LOG_BYTES:
                    status = "output_limit"
                    write_output_limit(sink, pending.replace(secret, b"[REDACTED]"))
                else:
                    sink.write(pending.replace(secret, b"[REDACTED]"))
        except BaseException:
            stop_process(process)
            raise
        finally:
            selector.close()
            process.stdout.close()
    if status != "completed":
        stop_process(process)
        result = (status, None)
    else:
        try:
            code = process.wait(timeout=max(0.1, timeout - (time.monotonic() - start)))
            result = ("completed" if code == 0 else "failed", code)
        except subprocess.TimeoutExpired:
            stop_process(process)
            result = ("timeout", None)
    write_host_bytes(log, bounded_candidate_bytes(control, host_log, MAX_LOG_BYTES + 64))
    return result[0], result[1], round(time.monotonic() - start, 3)


def workspace_for(args: argparse.Namespace, candidate: str, folder: Path) -> Path:
    profile = get_profile(getattr(args, "profile", "quarkus"))
    workspace = folder / "workspace"
    workspace.mkdir()
    for relative in profile.source_paths:
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(args.fixture_root / relative), str(target))
    shutil.copyfile(str(profile.objective), str(workspace / "objective.md"))
    if candidate == "aider" and "pom.xml" not in profile.source_paths:
        shutil.copyfile(str(args.fixture_root / "pom.xml"), str(workspace / "pom.xml"))
    if candidate in ("aider", "claude-code"):
        for directory, dirs, files in os.walk(str(workspace)):
            os.chown(directory, 1001, 1001)
            for name in files:
                os.chown(str(Path(directory) / name), 1001, 1001)
    return workspace


def generated_test(workspace: Path, args: argparse.Namespace, candidate: str,
                   stages: Optional[Dict[str, bytes]] = None) -> Tuple[Path, str]:
    profile = get_profile(getattr(args, "profile", "quarkus"))
    expected = {relative: (args.fixture_root / relative).read_bytes() for relative in profile.source_paths}
    expected["objective.md"] = profile.objective.read_bytes()
    if candidate == "aider" and "pom.xml" not in profile.source_paths:
        expected["pom.xml"] = (args.fixture_root / "pom.xml").read_bytes()
    expected.update(stages or {})
    entries = bounded_entries(workspace, MAX_CANDIDATE_WORKSPACE_ENTRIES)
    if any(path.is_symlink() or (not path.is_dir() and
                                not stat.S_ISREG(path.lstat().st_mode)) for path in entries):
        raise ValueError("candidate created linked or nonregular workspace output")
    for relative, content in expected.items():
        supplied = workspace / relative
        require_regular_output(workspace, supplied)
        if bounded_candidate_bytes(workspace, supplied, len(content)) != content:
            raise ValueError("candidate changed supplied file: " + relative)
    actual = [path for path in entries if path.is_file()]
    extras = [path for path in actual if str(path.relative_to(workspace)) not in expected]
    if candidate == "aider":
        extras = [path for path in extras
                  if str(path.relative_to(workspace)) not in AIDER_METADATA]
    if len(extras) != 1:
        raise ValueError("candidate must add exactly one test and no other files")
    relative = str(extras[0].relative_to(workspace))
    if not relative.startswith("src/test/java/") or not relative.endswith("Test.java"):
        raise ValueError("candidate added a file outside the native test path")
    if (args.fixture_root / relative).exists():
        raise ValueError("candidate test would replace an existing test")
    bounded_candidate_bytes(workspace, extras[0], MAX_CANDIDATE_TEST_BYTES)
    return extras[0], relative


STAGE_FIELDS = {
    "analysis": ("relevant_files", "existing_coverage", "gap", "feasibility"),
    "plan": ("test_path", "setup", "assertions", "baseline_expectation", "mutant_expectation"),
}


def validate_stage_artifacts(folder: Path, profile: FixtureProfile) -> Optional[Path]:
    if not profile.staged:
        return None
    records = {}
    for stage, fields in STAGE_FIELDS.items():
        try:
            stage_file = folder / (stage + ".json")
            require_regular_output(folder, stage_file)
            record = json.loads(bounded_candidate_bytes(
                folder, stage_file, MAX_CANDIDATE_JSON_BYTES))
        except (OSError, ValueError) as error:
            raise ValueError(stage + " artifact is missing or invalid") from error
        if not isinstance(record, dict) or any(not record.get(field) for field in fields):
            raise ValueError(stage + " artifact is missing required nonempty fields")
        records[stage] = record
    relevant = records["analysis"]["relevant_files"]
    assertions = records["plan"]["assertions"]
    if (not isinstance(relevant, list) or not relevant
            or any(not isinstance(value, str) or not value.strip() for value in relevant)
            or not any(value in profile.source_paths for value in relevant)):
        raise ValueError("analysis must cite at least one supplied source path")
    if (not isinstance(assertions, list) or not assertions
            or any(not isinstance(value, str) or not value.strip() for value in assertions)):
        raise ValueError("plan must include assertion descriptions")
    for stage, fields in STAGE_FIELDS.items():
        for field in fields:
            if field in ("relevant_files", "assertions"):
                continue
            if not isinstance(records[stage][field], str) or not records[stage][field].strip():
                raise ValueError(stage + " field must be nonempty text: " + field)
    if records["analysis"]["feasibility"] != "feasible":
        raise ValueError("candidate did not establish feasible execution")
    planned = records["plan"]["test_path"]
    if not isinstance(planned, str):
        raise ValueError("planned test path must be a string")
    return test_target(planned)


def validate_stage_workspace(workspace: Path, args: argparse.Namespace,
                             candidate: str, stage: str,
                             analysis_bytes: Optional[bytes] = None) -> None:
    if stage not in ("analysis", "plan"):
        raise ValueError("unknown stage workspace: " + stage)
    profile = get_profile(getattr(args, "profile", "quarkus"))
    expected = {relative: (args.fixture_root / relative).read_bytes()
                for relative in profile.source_paths}
    expected["objective.md"] = profile.objective.read_bytes()
    if candidate == "aider" and "pom.xml" not in expected:
        expected["pom.xml"] = (args.fixture_root / "pom.xml").read_bytes()
    allowed = set(expected)
    allowed.add("analysis.json")
    if stage == "plan":
        allowed.add("plan.json")
        if analysis_bytes is None:
            raise ValueError("Plan stage needs the recorded Analyze artifact")
        expected["analysis.json"] = analysis_bytes
    if candidate == "aider":
        allowed.update(AIDER_METADATA)
    paths = [path for path in bounded_entries(workspace, MAX_CANDIDATE_WORKSPACE_ENTRIES)
             if path.is_file() or path.is_symlink()]
    if any(path.is_symlink() for path in paths):
        raise ValueError("candidate created a stage workspace symlink")
    actual = {str(path.relative_to(workspace)) for path in paths}
    if not actual.issubset(allowed) or not (workspace / (stage + ".json")).is_file():
        raise ValueError(stage + " stage created an unexpected file")
    for relative, content in expected.items():
        if bounded_candidate_bytes(workspace, workspace / relative,
                                   len(content)) != content:
            raise ValueError(stage + " stage changed supplied context: " + relative)


def stage_prompt(profile: FixtureProfile, stage: str) -> str:
    objective = profile.objective.read_text()
    if stage == "analysis":
        return ("Read objective.md and the supplied application files. Write analysis.json "
                "as a JSON object with relevant_files (list of paths), existing_coverage, "
                "gap, and feasibility. The feasibility value must be exactly one of "
                'the JSON strings "feasible", "blocked", or "unknown". '
                "Use source evidence. Do not create a Java test, plan.json, or modify other files.\n" + objective)
    if stage == "plan":
        return ("Read objective.md, analysis.json, and the supplied files. Write plan.json "
                "as a JSON object with test_path (new src/test/java/*Test.java), setup, "
                "assertions (list of strings), baseline_expectation, and mutant_expectation. "
                "The defect is hidden; describe the behavior a test "
                "should detect. Do not create a Java test or modify other files.\n" + objective)
    return ("Read objective.md, analysis.json, plan.json, and the supplied files. "
            "Create exactly one new native Java test at the plan.test_path. "
            "Do not change existing files, analysis.json, plan.json, or run commands.\n" + objective)


def record_invocation(folder: Path, candidate: str, command: List[str],
                      config: Dict[str, object], source_revision: str,
                      profile: FixtureProfile = QUARKUS) -> None:
    config_bytes = (json.dumps(config, sort_keys=True, indent=2) + "\n").encode()
    write_host_bytes(folder / "sanitized-config.json", config_bytes)
    key_name = "OPENAI_API_KEY" if candidate == "aider" else "ANTHROPIC_API_KEY"
    redacted = [part + "=[REDACTED]" if part == key_name else part for part in command]
    redacted.append(key_name + "=[REDACTED]")
    write_json(folder / "invocation.json", {
        "candidate": candidate, "tool_version": VERSIONS[candidate],
        "model_id": MODEL, "endpoint_id": ENDPOINT,
        "prompt_sha256": sha256_bytes(profile.objective.read_bytes()),
        "config_sha256": sha256_bytes(config_bytes),
        "redacted_command": redacted, "tool_source_revision": source_revision,
    })


def container_id_path(folder: Path) -> Path:
    """Keep Podman control data outside candidate-writable mounts."""
    return folder.parent / ".container-control" / folder.name / "generator.cid"


def prepare_container_control(folder: Path) -> None:
    container_id_path(folder).parent.mkdir(parents=True, exist_ok=True)


def protected_stage_mounts(folder: Path, container_root: str,
                           stage: Optional[str], include_run_records: bool) -> List[str]:
    """Keep prior stage artifacts read-only even while a later stage is running."""
    previous = (["analysis"] if stage == "plan" else
                ["analysis", "plan"] if stage == "code" else [])
    mounts: List[str] = []
    for name in previous:
        filenames = [name + ".json"]
        if include_run_records:
            filenames.extend([name + "-run.json", name + ".log"])
        for filename in filenames:
            mounts.extend(["-v", str(folder / filename) + ":" + container_root
                           + "/" + filename + ":ro,Z"])
    return mounts


def aider_command(workspace: Path, folder: Path,
                  profile: FixtureProfile = QUARKUS, stage: Optional[str] = None) -> List[str]:
    command = ["podman", "run", "--rm", "--pull=never", "--network=host"] + GENERATION_GUARDS + [
               "--cidfile", str(container_id_path(folder)),
               "-v", str(workspace) + ":/work:Z"] + protected_stage_mounts(
               workspace, "/work", stage, False) + [
               "-w", "/work", "-e", "OPENAI_API_KEY", "-e", "OPENAI_API_BASE", AIDER_IMAGE,
               "aider", "--model", "openai/" + MODEL, "--yes-always",
               "--no-auto-commits", "--no-git", "--no-check-update",
               "--no-show-model-warnings"]
    if stage:
        command.extend(["--message", stage_prompt(profile, stage)])
        if stage != "code":
            command.extend(["--file", stage + ".json"])
    else:
        command.extend(["--message-file", "objective.md"])
    for relative in profile.source_paths:
        command.extend(["--read", relative])
    if stage in ("plan", "code"):
        command.extend(["--read", "analysis.json"])
    if stage == "code":
        command.extend(["--read", "plan.json"])
    return command


def claude_command(args: argparse.Namespace, workspace: Path, folder: Path,
                   profile: FixtureProfile = QUARKUS,
                   stage: Optional[str] = None) -> List[str]:
    command = claude_container_base(args.claude_runtime_dir, "host")
    command.extend(["--cidfile", str(container_id_path(folder)),
                    "-v", str(workspace) + ":/work:Z"])
    command.extend(protected_stage_mounts(workspace, "/work", stage, False))
    command.extend(["--tmpfs", "/claude-config:rw,nosuid,size=64m,mode=1777",
                    "-w", "/work", "-e", "ANTHROPIC_API_KEY",
                    "-e", "ANTHROPIC_BASE_URL", "-e", "CLAUDE_CONFIG_DIR",
                    PYTHON_IMAGE, CLAUDE_CONTAINER_BIN,
                    "-p", ("Use Write/Edit tools to create the requested files. "
                           "Do not use Bash or run commands.\n" + stage_prompt(profile, stage))
                           if stage else CLAUDE_PROMPT,
                    "--bare", "--model", MODEL,
            "--permission-mode", "acceptEdits", "--allowedTools", "Read,Write,Edit,Glob,Grep",
                    "--max-turns", "12" if stage and profile.staged else "8",
                    "--output-format", "json"])
    return command


def adapter_command(args: argparse.Namespace, candidate: str, folder: Path,
                    stage: Optional[str] = None) -> List[str]:
    profile = get_profile(getattr(args, "profile", "quarkus"))
    external = external_candidate(args, candidate)
    command = ["podman", "run", "--rm", "--pull=never", "--network=host"] + GENERATION_GUARDS + [
               "--cidfile", str(container_id_path(folder)),
               "-v", str(args.fixture_root) + ":/fixture:ro,Z",
               "-v", str(profile.objective) + ":/objective.md:ro,Z"]
    protected = protected_stage_mounts(folder, "/output", stage, True) if profile.staged else []
    if external:
        command.extend(["--read-only", "--tmpfs", "/tmp:rw,nosuid,size=64m",
                        "-v", str(folder) + ":/output:Z"])
        command.extend(protected)
        command.extend(["-w", "/output", "-e", "ENMAAS_API_KEY",
                        external["image"], "/adapter"])
    else:
        venv = args.pydantic_venv if candidate == "pydantic-ai" else args.langgraph_venv
        adapter = ROOT / "candidates" / ("pydantic_ai" if candidate == "pydantic-ai" else "langgraph") / "adapter.py"
        command.extend(["-v", str(adapter) + ":/adapter.py:ro,Z",
                        "-v", str(venv) + ":/venv:ro,Z", "-v", str(folder) + ":/output:Z"])
        command.extend(protected)
        command.extend(["-w", "/output", "-e", "ENMAAS_API_KEY", PYTHON_IMAGE,
                        "/venv/bin/python", "/adapter.py"])
    command.extend(["--fixture-root", "/fixture",
               "--objective", "/objective.md", "--output-dir",
               "/output/" + stage if stage in ("analysis", "plan") else "/output/generated",
               "--base-url", OPENAI_URL, "--model-id", MODEL,
               "--profile", profile.name])
    if profile.staged:
        command.extend(["--stage", stage or "code", "--framework", profile.framework,
                        "--instruction", stage_prompt(profile, stage or "code"),
                        "--analysis-file", "/output/analysis.json",
                        "--plan-file", "/output/plan.json"])
        for relative in profile.source_paths:
            command.extend(["--source-path", relative])
    if candidate == "langgraph":
        command.extend(["--endpoint-id", ENDPOINT])
    return command


def run_stage(args: argparse.Namespace, candidate: str, folder: Path,
              workspace: Path, stage: str, env: Dict[str, str],
              environment: List[str], key: str, profile: FixtureProfile) -> None:
    prior_stages = ["analysis"] if stage == "plan" else []
    if prior_stages:
        verify_stage_checkpoints(args, candidate, folder, prior_stages)
        if candidate in ("aider", "claude-code"):
            verify_workspace_stages(folder, workspace, prior_stages)
    if candidate in ("aider", "claude-code"):
        target = workspace / (stage + ".json")
        write_host_bytes(target, b"{}\n", owner=1001)
        command = (aider_command(workspace, folder, profile, stage)
                   if candidate == "aider" else
                   claude_command(args, workspace, folder, profile, stage))
    else:
        target = folder / (stage + ".json")
        command = adapter_command(args, candidate, folder, stage)
    save_command(folder, command, environment, env, stage + "-command.json")
    timeout = run_timeout(args, candidate, stage, profile)
    prepare_container_control(folder)
    try:
        status, code, seconds = run_bounded(command, env, None,
                                            folder / (stage + ".log"), key, timeout)
    finally:
        remove_container(folder)
    write_json(folder / (stage + "-run.json"), {"status": status, "exit_code": code,
                                                "elapsed_seconds": seconds, "elapsed_ms": round(seconds * 1000)})
    if prior_stages:
        verify_stage_checkpoints(args, candidate, folder, prior_stages)
        if candidate in ("aider", "claude-code"):
            verify_workspace_stages(folder, workspace, prior_stages)
    if status != "completed":
        raise ValueError(stage + " stage blocked (see " + stage + ".log)")
    require_regular_output(workspace if candidate in ("aider", "claude-code") else folder,
                           target)
    if candidate in ("aider", "claude-code"):
        if stage == "plan":
            require_regular_output(folder, folder / "analysis.json")
        previous = (bounded_candidate_bytes(folder, folder / "analysis.json",
                                            MAX_CANDIDATE_JSON_BYTES)
                    if stage == "plan" else None)
        validate_stage_workspace(workspace, args, candidate, stage, previous)
        if bounded_candidate_bytes(
                workspace, target, MAX_CANDIDATE_JSON_BYTES) == b"{}\n":
            raise ValueError(stage + " artifact was not authored by the candidate")
    if target != folder / (stage + ".json"):
        write_host_bytes(folder / (stage + ".json"), bounded_candidate_bytes(
            workspace, target, MAX_CANDIDATE_JSON_BYTES))
        verify_workspace_stages(folder, workspace, prior_stages + [stage])
    write_stage_checkpoint(args, candidate, folder, stage, "completed",
                           [folder / (stage + ".json"), folder / (stage + "-run.json"),
                            folder / (stage + ".log")])
    verify_stage_checkpoints(args, candidate, folder, prior_stages + [stage])


def save_command(folder: Path, command: List[str], env_names: List[str],
                 env: Dict[str, str], filename: str = "execution-command.json") -> None:
    write_json(folder / filename, {
        "argv": command,
        "environment": {name: "[REDACTED]" if "KEY" in name else env[name]
                        for name in env_names},
        "credential_source": "runner stdin" if env_names else "none",
    })


def remove_container(folder: Path) -> None:
    cid_file = container_id_path(folder)
    if not cid_file.exists() and not cid_file.is_symlink():
        return
    if not stat.S_ISREG(cid_file.lstat().st_mode):
        raise ValueError("container ID file is linked or nonregular")
    cid = cid_file.read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", cid):
        cid_file.unlink()
        raise ValueError("container ID file has invalid content")
    subprocess.call(["podman", "rm", "-f", cid], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
    cid_file.unlink()


def save_dependencies(args: argparse.Namespace, candidate: str, folder: Path, key: str) -> None:
    if candidate not in ("pydantic-ai", "langgraph"):
        return
    venv = args.pydantic_venv if candidate == "pydantic-ai" else args.langgraph_venv
    command = ["podman", "run", "--rm", "--pull=never", "--network=none",
               "-v", str(venv) + ":/venv:ro,Z", PYTHON_IMAGE,
               "/venv/bin/python", "-m", "pip", "freeze", "--all"]
    save_command(folder, command, [], {}, "dependencies-command.json")
    env = safe_environment()
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    status, code, _ = run_bounded(command, env, None, folder / "pip-freeze.log", key, 60)
    if status != "completed" or code != 0:
        raise ValueError("could not capture Python dependency versions")


def validate_adapter_output(workspace: Path, args: argparse.Namespace) -> Tuple[Path, str]:
    result_file = workspace / "result.json"
    require_regular_output(workspace, result_file)
    result = json.loads(bounded_candidate_bytes(
        workspace, result_file, MAX_CANDIDATE_JSON_BYTES))
    if not isinstance(result, dict) or not isinstance(result.get("path"), str):
        raise ValueError("adapter result needs a test path")
    relative = str(test_target(result["path"]))
    generated = workspace / relative
    require_regular_output(workspace, generated)
    if sha256_bytes(bounded_candidate_bytes(
            workspace, generated, MAX_CANDIDATE_TEST_BYTES)) != result["content_sha256"]:
        raise ValueError("adapter test hash mismatch")
    return generated, relative


def generation_timeout(candidate: str, profile: FixtureProfile) -> int:
    if profile.staged and candidate in ("pydantic-ai", "langgraph"):
        return 600
    return 300 if candidate == "aider" else 360


def generate(args: argparse.Namespace, candidate: str, folder: Path,
             key: str) -> Tuple[Path, str]:
    profile = get_profile(getattr(args, "profile", "quarkus"))
    env = safe_environment()
    if candidate in ("aider", "claude-code"):
        workspace = workspace_for(args, candidate, folder)
        if candidate == "aider":
            command = aider_command(workspace, folder, profile,
                                    "code" if profile.staged else None)
            env.update({"OPENAI_API_KEY": key, "OPENAI_API_BASE": OPENAI_URL})
            source = AIDER_IMAGE_ID
            config = {"candidate": candidate, "model_id": MODEL, "endpoint_id": ENDPOINT,
                      "base_url": OPENAI_URL, "image": AIDER_IMAGE,
                      "image_id": AIDER_IMAGE_ID, "tool_version": VERSIONS[candidate]}
            environment = ["OPENAI_API_KEY", "OPENAI_API_BASE"]
        else:
            command = claude_command(args, workspace, folder, profile,
                                     "code" if profile.staged else None)
            env.update({"ANTHROPIC_API_KEY": key, "ANTHROPIC_BASE_URL": ANTHROPIC_URL,
                        "CLAUDE_CONFIG_DIR": "/claude-config",
                        "PATH": CLAUDE_CONTAINER_PATH_ENV})
            source = CLAUDE_BINARY_DIGEST
            config = {"candidate": candidate, "model_id": MODEL, "endpoint_id": ENDPOINT,
                      "base_url": ANTHROPIC_URL, "prompt_wrapper": CLAUDE_PROMPT,
                      "tool_version": VERSIONS[candidate], "binary_sha256": source,
                      "container_image_id": PYTHON_IMAGE_ID, "container_user": "1001:1001"}
            environment = ["ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR", "PATH"]
        config["source_context"] = {relative: sha256_file(args.fixture_root / relative)
                                    for relative in profile.source_paths}
        config["credential"] = "[REDACTED]"
    else:
        command = adapter_command(args, candidate, folder,
                                  "code" if profile.staged else None)
        env["ENMAAS_API_KEY"] = key
        environment = ["ENMAAS_API_KEY"]
        workspace = folder / "generated"
    stages = None
    planned_path = None
    if profile.staged:
        for stage in ("analysis", "plan"):
            run_stage(args, candidate, folder, workspace, stage, env, environment, key, profile)
        planned_path = validate_stage_artifacts(folder, profile)
        write_json(folder / "stage-validation.json", {
            "analysis": "validated_structure", "plan": "validated_structure",
            "semantic_review": "pending", "test_path": str(planned_path),
        })
        if (args.fixture_root / planned_path).exists():
            raise ValueError("planned test replaces an existing fixture test")
        stages = {stage + ".json": bounded_candidate_bytes(
            folder, folder / (stage + ".json"), MAX_CANDIDATE_JSON_BYTES)
                  for stage in ("analysis", "plan")}
    if candidate in ("aider", "claude-code"):
        if stages:
            config["stage_sha256"] = {name: sha256_bytes(content)
                                      for name, content in stages.items()}
        record_invocation(folder, candidate, command, config, source, profile)
    save_command(folder, command, environment, env)
    timeout = run_timeout(args, candidate, "code", profile)
    if profile.staged:
        verify_stage_checkpoints(args, candidate, folder, ["analysis", "plan"])
        if candidate in ("aider", "claude-code"):
            verify_workspace_stages(folder, workspace, ["analysis", "plan"])
    prepare_container_control(folder)
    try:
        status, code, seconds = run_bounded(command, env, None,
                                            folder / "generation.log", key, timeout)
    finally:
        remove_container(folder)
    write_json(folder / "generation.json", {"status": status, "exit_code": code,
                                            "elapsed_seconds": seconds, "elapsed_ms": round(seconds * 1000),
                                            "timeout_seconds": timeout})
    if profile.staged:
        verify_stage_checkpoints(args, candidate, folder, ["analysis", "plan"])
        if candidate in ("aider", "claude-code"):
            verify_workspace_stages(folder, workspace, ["analysis", "plan"])
    if status != "completed":
        raise ValueError("generation " + status + " (see generation.log)")
    if candidate in ("aider", "claude-code"):
        generated, relative = generated_test(workspace, args, candidate, stages)
    else:
        generated, relative = validate_adapter_output(workspace, args)
    if planned_path is not None and relative != str(planned_path):
        raise ValueError("generated test path differs from the approved plan")
    if stages:
        for name, content in stages.items():
            if bounded_candidate_bytes(folder, folder / name,
                                       MAX_CANDIDATE_JSON_BYTES) != content:
                raise ValueError("candidate changed " + name + " during Code")
    write_stage_checkpoint(args, candidate, folder, "code", "completed",
                           [generated, folder / "generation.json", folder / "generation.log"])
    verify_stage_checkpoints(args, candidate, folder, generation_checkpoints(profile))
    return generated, relative


def evaluate(args: argparse.Namespace, candidate: str, folder: Path,
             test: Path, relative: str, key: str) -> str:
    profile = get_profile(getattr(args, "profile", "quarkus"))
    verify_stage_checkpoints(args, candidate, folder, generation_checkpoints(profile))
    invocation = (folder / "invocation.json" if candidate in ("aider", "claude-code")
                  else folder / "generated/invocation.json")
    bounded_candidate_bytes(folder, invocation, MAX_CANDIDATE_JSON_BYTES)
    evaluation = folder / "evaluation"
    if evaluation.exists() or evaluation.is_symlink():
        raise ValueError("candidate pre-created the evaluation output")
    command = [sys.executable, str(ROOT / "candidate_evaluation.py"),
               "--candidate-test-file", str(test), "--candidate-test-path", relative,
               "--candidate", candidate, "--tool-version", tool_version(args, candidate),
               "--model-id", MODEL, "--endpoint-id", ENDPOINT,
               "--invocation-record", str(invocation), "--fixture-root", str(args.fixture_root),
               "--profile", get_profile(getattr(args, "profile", "quarkus")).name,
               "--maven-cache", str(args.maven_cache), "--output-root", str(folder / "evaluation")]
    save_command(folder, command, [], {}, "evaluation-command.json")
    status, code, seconds = run_bounded(command, safe_environment(), None,
                                        folder / "evaluation-run.log", key,
                                        run_timeout(args, candidate, "execute",
                                                    get_profile(getattr(args, "profile", "quarkus"))))
    write_json(folder / "evaluation-run.json", {"status": status, "exit_code": code,
                                                 "elapsed_seconds": seconds, "elapsed_ms": round(seconds * 1000)})
    verify_stage_checkpoints(args, candidate, folder, generation_checkpoints(profile))
    report = folder / "evaluation/evaluation.json"
    if (status, code) not in (("completed", 0), ("failed", 2)) or not report.is_file():
        write_stage_checkpoint(args, candidate, folder, "execute", "blocked",
                               [folder / "evaluation-run.json", folder / "evaluation-run.log"])
        return "blocked_evaluation"
    require_regular_output(folder, report)
    result = json.loads(bounded_candidate_bytes(
        folder, report, 2 * MAX_CANDIDATE_JSON_BYTES))["result"]
    expected_code = {"discriminates": 0, "inconclusive": 2}.get(result)
    if expected_code != code:
        write_stage_checkpoint(args, candidate, folder, "execute", "blocked",
                               [folder / "evaluation-run.json", folder / "evaluation-run.log"])
        return "blocked_evaluation"
    write_stage_checkpoint(args, candidate, folder, "execute", "completed",
                           [report, folder / "evaluation-run.json",
                            folder / "evaluation-run.log"], result)
    return result


def parse_arguments() -> Tuple[argparse.Namespace, argparse.ArgumentParser]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="quarkus")
    parser.add_argument("--fixture-root", required=True, type=Path)
    parser.add_argument("--maven-cache", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--pydantic-venv", type=Path)
    parser.add_argument("--langgraph-venv", type=Path)
    parser.add_argument("--claude-runtime-dir", type=Path,
                        help="pinned node_modules directory, mounted read-only in Podman")
    parser.add_argument("--candidate", action="append", choices=list(VERSIONS))
    parser.add_argument("--run-spec", type=Path,
                        help="versioned, credential-free experiment specification")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--api-key-stdin", action="store_true", help="read one key from stdin")
    return parser.parse_args(), parser


def record_candidate_error(args: argparse.Namespace, folder: Path, filename: str,
                           record: Dict[str, object]) -> bool:
    """Do not overwrite a candidate-planted error file or follow its links."""
    try:
        write_json(folder / filename, record)
        return True
    except OSError:
        host_errors = args.output_root / ".candidate-errors"
        host_errors.mkdir(mode=0o700, exist_ok=True)
        write_json(host_errors / (folder.name + "-" + filename), record)
        return False


def run_one_candidate(args: argparse.Namespace, candidate: str, key: str) -> Tuple[str, bool]:
    folder = args.output_root / candidate
    folder.mkdir()
    security_blocked = False
    try:
        save_dependencies(args, candidate, folder, key)
        test, relative = generate(args, candidate, folder, key)
        result = evaluate(args, candidate, folder, test, relative, key)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        result = "blocked"
        # A candidate-controlled name already existed where the runner needed
        # to create an evidence file; do not export that packet as trusted.
        security_blocked = isinstance(error, FileExistsError)
        record = {"error_type": type(error).__name__,
                  "reason": str(error).replace(key, "[REDACTED]")[:500]}
        try:
            record_blocked_checkpoint(args, candidate, folder)
        except (ValueError, OSError) as checkpoint_error:
            record["checkpoint_error"] = type(checkpoint_error).__name__
            security_blocked = True
        if not record_candidate_error(args, folder, "runner-error.json", record):
            security_blocked = True
    try:
        scan_evidence_for_key(args.output_root, key)
    except (ValueError, OSError) as error:
        record_candidate_error(args, folder, "evidence-scan-error.json", {
            "error_type": type(error).__name__,
            "reason": str(error).replace(key, "[REDACTED]")[:500],
        })
        return "blocked", True
    return result, security_blocked


def save_summary(args: argparse.Namespace, outcomes: Dict[str, str], blocked: bool,
                 files_scanned: int, mount_capacity: int, source: Dict[str, object]) -> None:
    profile = get_profile(getattr(args, "profile", "quarkus"))
    write_json(args.output_root / "summary.json", {"candidate_results": outcomes,
               "fixture_profile": profile.name,
               "fixture_tree_sha256": profile.tree_sha256,
               "objective_sha256": profile.objective_sha256,
               "model_id": MODEL, "endpoint_id": ENDPOINT,
               "output_mount_capacity_bytes": mount_capacity,
               "evidence_scan": "blocked" if blocked else "passed",
               "evidence_files_scanned": files_scanned,
               "runner_sha256": sha256_file(Path(__file__).resolve()),
               "evaluator_sha256": sha256_file(ROOT / "candidate_evaluation.py"),
               "run_spec_sha256": (sha256_file(args.output_root / "run-spec.json")
                                   if getattr(args, "run_spec_data", None) else None),
               **getattr(args, "run_identity", {}), **source})


def main() -> int:
    args, parser = parse_arguments()
    try:
        spec_path = getattr(args, "run_spec", None)
        if spec_path and getattr(args, "candidate", None):
            parser.error("--candidate and --run-spec cannot be combined")
        args.run_spec_data = load_run_spec(spec_path) if spec_path else None
    except (ValueError, OSError, json.JSONDecodeError) as error:
        parser.error(str(error))
    if args.run_spec_data:
        args.profile = args.run_spec_data["profile"]
        candidates = args.run_spec_data["candidates"]
    else:
        candidates = args.candidate or ["aider", "pydantic-ai", "claude-code", "langgraph"]
    if len(set(candidates)) != len(candidates):
        parser.error("candidate names must be unique")
    try:
        versions = preflight(args, candidates)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    if args.check_only:
        print(json.dumps({"preflight": "passed", "versions": versions}, sort_keys=True))
        return 0
    try:
        mount_capacity = output_mount_capacity(args.output_root)
        source = checkout_identity()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    if not args.api_key_stdin or sys.stdin.isatty():
        parser.error("live run requires --api-key-stdin with a secure input stream")
    key = sys.stdin.readline().rstrip("\r\n")
    if not key:
        parser.error("empty EnMaaS key")
    args.output_root.mkdir(parents=True, mode=0o700)
    args.run_identity = {
        "run_id": str(uuid.uuid4()),
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "candidate_order": list(candidates),
    }
    if args.run_spec_data:
        write_json(args.output_root / "run-spec.json", args.run_spec_data)
    profile = get_profile(getattr(args, "profile", "quarkus"))
    write_json(args.output_root / "prepare.json", {
        "fixture_profile": profile.name,
        "fixture_repository": profile.repository,
        "fixture_revision": profile.revision,
        "fixture_tree_sha256": profile.tree_sha256,
        "objective_sha256": profile.objective_sha256,
        "maven_image": profile.maven_image,
        "maven_image_id": profile.maven_image_id,
        "maven_options": list(profile.maven_options),
        "context_sha256": {relative: sha256_file(args.fixture_root / relative)
                           for relative in profile.source_paths},
        "candidate_versions": versions,
        "model_id": MODEL,
        "endpoint_id": ENDPOINT,
        "run_spec_sha256": (sha256_file(args.output_root / "run-spec.json")
                            if args.run_spec_data else None),
        "baseline_build": "external preflight required; runner does not infer test readiness",
        **args.run_identity,
    })
    outcomes = {}
    security_blocked = False
    for candidate in candidates:
        outcomes[candidate], security_blocked = run_one_candidate(args, candidate, key)
        print(candidate + ": " + outcomes[candidate], flush=True)
        if security_blocked:
            break
    for candidate in candidates:
        outcomes.setdefault(candidate, "not_run")
    files_scanned = 0
    try:
        files_scanned = scan_evidence_for_key(args.output_root, key)
    except (ValueError, OSError) as error:
        security_blocked = True
        write_json(args.output_root / "evidence-scan-error.json", {
            "error_type": type(error).__name__,
            "reason": str(error).replace(key, "[REDACTED]")[:500],
        })
    save_summary(args, outcomes, security_blocked, files_scanned, mount_capacity, source)
    return 0 if not security_blocked and all(value == "discriminates" for value in outcomes.values()) else 2


if __name__ == "__main__":
    sys.exit(main())
