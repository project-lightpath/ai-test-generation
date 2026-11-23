"""Generate a deterministic, test-only Java test from a pinned replay case."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CASE_PATH = REPOSITORY_ROOT / "experiments/harness/fixtures/transfer-objective.json"
MODEL_SOURCE_ROOT = REPOSITORY_ROOT / "experiments/harness/fixtures"
EXPECTED_JAVA_REVISION = "2aac181ae63d938c118bac133d33fb8ae19eeec9"
EXPECTED_QUARKUS_VERSION = "3.40.1"


class ReplayInputError(ValueError):
    """Raised when a replay case is incomplete or unsafe."""


class ReplayState:
    """Enforce the experiment's Analyze -> Plan -> Code -> Execute order."""

    _allowed_statuses = {
        "analyze": {"replayed"},
        "plan": {"synthetic_replay_input"},
        "code": {"generated"},
        "execute": {"not_run", "passed", "failed", "blocked"},
    }

    def __init__(self) -> None:
        self._events: list[dict[str, str]] = []

    def advance(self, stage: str, status: str) -> None:
        expected_stages = tuple(self._allowed_statuses)
        expected_index = len(self._events)
        if expected_index >= len(expected_stages) or stage != expected_stages[expected_index]:
            raise ReplayInputError("replay stages must follow Analyze -> Plan -> Code -> Execute")
        if status not in self._allowed_statuses[stage]:
            raise ReplayInputError(f"invalid status for replay stage {stage}")
        self._events.append({"stage": stage, "status": status})

    @property
    def events(self) -> list[dict[str, str]]:
        return [dict(event) for event in self._events]


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return _sha256(encoded)


def _harness_digest() -> str:
    digest = hashlib.sha256()
    files = (Path(__file__), CASE_PATH, MODEL_SOURCE_ROOT / "TransferGeneratedReplayTest.java")
    for path in sorted(files):
        relative = path.relative_to(REPOSITORY_ROOT).as_posix().encode()
        digest.update(relative + b"\0" + path.read_bytes() + b"\0")
    return "sha256:" + digest.hexdigest()


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative_path = path.relative_to(root)
        if relative_path.parts[0] == "target" or ".git" in relative_path.parts:
            continue
        relative = relative_path.as_posix().encode()
        digest.update(relative + b"\0" + path.read_bytes() + b"\0")
    return "sha256:" + digest.hexdigest()


def _safe_test_path(value: object) -> Path:
    if not isinstance(value, str):
        raise ReplayInputError("generated test path must be a string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.parts[:3] != ("src", "test", "java"):
        raise ReplayInputError("generated test must stay under src/test/java")
    if not path.name.endswith("Test.java"):
        raise ReplayInputError("generated test must use the Java *Test.java naming pattern")
    return Path(*path.parts)


def _validate_generated_test(path: Path, source: str, objective: dict[str, Any]) -> None:
    expected = objective["expected"]
    required = (
        "@QuarkusTest",
        "@Test",
        '.post("/transfers")',
        f".statusCode({expected['status_code']})",
        f'.body("requestReference", is("{objective["request_reference"]}"))',
        f'.body("accepted", is({str(expected["accepted"]).lower()}))',
        f'.body("path", is("{expected["path"]}"))',
    )
    if not source.strip() or any(token not in source for token in required):
        raise ReplayInputError("generated test does not assert the replay objective")
    unsafe_markers = ("ProcessBuilder(", "Runtime.getRuntime(", "java.net.", "assertTrue(true)")
    if path.suffix != ".java" or any(marker in source for marker in unsafe_markers):
        raise ReplayInputError("generated test failed the basic quality check")


def _validate_fixture(case: dict[str, Any], fixture_root: Path) -> None:
    fixture = case.get("fixture")
    if not isinstance(fixture, dict):
        raise ReplayInputError("fixture identity is missing")
    if fixture.get("revision") != EXPECTED_JAVA_REVISION:
        raise ReplayInputError("fixture revision does not match the pinned replay source")
    if fixture.get("java_release") != 21 or fixture.get("quarkus_version") != EXPECTED_QUARKUS_VERSION:
        raise ReplayInputError("fixture Java or Quarkus version does not match the pinned profile")
    pom = fixture_root / "pom.xml"
    if not pom.is_file():
        raise ReplayInputError("fixture pom.xml is missing")
    pom_text = pom.read_text()
    if "<java.release>21</java.release>" not in pom_text or EXPECTED_QUARKUS_VERSION not in pom_text:
        raise ReplayInputError("fixture pom.xml does not match the expected Java 21/Quarkus profile")
    required_sources = (
        "src/main/java/com/projectlightpath/fixture/api/TransferResource.java",
        "src/main/java/com/projectlightpath/fixture/target/ServerUserAuthService.java",
    )
    if any(not (fixture_root / source).is_file() for source in required_sources):
        raise ReplayInputError("pinned transfer fixture source is incomplete")


def replay(case_path: Path, fixture_root: Path, output_root: Path) -> dict[str, Any]:
    case = json.loads(case_path.read_text())
    if not isinstance(case, dict) or case.get("kind") != "atg.fake-model-replay" or case.get("version") != "1":
        raise ReplayInputError("unsupported replay case")
    _validate_fixture(case, fixture_root)
    generated = case.get("generated_test")
    if not isinstance(generated, dict):
        raise ReplayInputError("generated test identity is missing")
    test_path = _safe_test_path(generated.get("path"))
    source_name = generated.get("source_file")
    if source_name != "TransferGeneratedReplayTest.java":
        raise ReplayInputError("fake model response file is not the pinned response")
    source = (MODEL_SOURCE_ROOT / source_name).read_text()
    objective = case.get("objective")
    if not isinstance(objective, dict) or not isinstance(objective.get("expected"), dict):
        raise ReplayInputError("objective or expected behavior is missing")
    _validate_generated_test(test_path, source, objective)
    if output_root.exists():
        raise ReplayInputError("output directory must be new to prevent stale replay files")

    def ignore_build_outputs(directory: str, names: list[str]) -> set[str]:
        current = Path(directory)
        ignored = {".git"} if ".git" in names else set()
        if current == fixture_root and "target" in names:
            ignored.add("target")
        return ignored

    shutil.copytree(fixture_root, output_root, ignore=ignore_build_outputs)
    generated_path = output_root / test_path
    generated_path.parent.mkdir(parents=True, exist_ok=True)
    generated_path.write_text(source)

    state = ReplayState()
    state.advance("analyze", "replayed")
    state.advance("plan", "synthetic_replay_input")
    state.advance("code", "generated")
    state.advance("execute", "not_run")
    return {
        "record_type": "atg.fake-model-replay",
        "version": "1",
        "run_id": case["run_id"],
        "input_digest": _canonical_digest(case),
        "harness_digest": _harness_digest(),
        "fixture": {
            "repository": case["fixture"]["repository"],
            "revision": case["fixture"]["revision"],
            "tree_digest": _tree_digest(fixture_root),
            "java_release": 21,
            "quarkus_version": EXPECTED_QUARKUS_VERSION,
        },
        "objective": objective,
        "plan": case["plan"],
        "model": {
            **case["model"],
            "config_digest": _canonical_digest(case["model"]),
            "response_digest": _sha256(source.encode()),
        },
        "generated_test": {
            "path": test_path.as_posix(),
            "digest": _sha256(source.encode()),
        },
        "stages": state.events,
        "limitations": [
            "The analysis and plan are fixed synthetic inputs, not live ATG stages or human approval.",
            "The fake model is a recorded deterministic response; no real model endpoint was called.",
            "Execution evidence must be recorded separately; generation alone is not a test pass or vulnerability proof.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    try:
        record = replay(CASE_PATH, args.fixture_root, args.output_root)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, AttributeError, ReplayInputError) as error:
        parser.error(str(error))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
