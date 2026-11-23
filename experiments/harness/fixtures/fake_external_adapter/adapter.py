#!/usr/bin/env python3
"""Offline fake adapter for the FLPATH-5050 external-candidate contract.

It replays one saved Spring test. It makes no model or network calls and only
accepts the literal fake key, so a real credential cannot be used by accident.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil

FIXTURE = Path("/opt/fake-fixture")
TEST_PATH = Path(
    "src/test/java/com/redhat/lightwell/service/"
    "TransactionServiceFullBalanceTransferTest.java"
)
FAKE_KEY = "fake-key-for-contract-test"


def digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--fixture-root", required=True, type=Path)
    cli.add_argument("--objective", required=True, type=Path)
    cli.add_argument("--output-dir", required=True, type=Path)
    cli.add_argument("--base-url", required=True)
    cli.add_argument("--model-id", required=True)
    cli.add_argument("--profile", required=True)
    cli.add_argument("--stage", required=True, choices=("analysis", "plan", "code"))
    cli.add_argument("--framework")
    cli.add_argument("--instruction")
    cli.add_argument("--analysis-file", required=True, type=Path)
    cli.add_argument("--plan-file", required=True, type=Path)
    cli.add_argument("--source-path", action="append", default=[])
    return cli


def main() -> int:
    args = parser().parse_args()
    if os.environ.get("ENMAAS_API_KEY") != FAKE_KEY:
        raise ValueError("fake adapter accepts only the fixed fake key")
    if args.profile != "spring-transfer" or not args.output_dir.is_relative_to("/output"):
        raise ValueError("fake adapter supports only the pinned Spring output")
    if not args.objective.is_file() or not args.fixture_root.is_dir():
        raise ValueError("fixture context is missing")
    args.output_dir.mkdir(parents=True)
    if args.stage == "analysis":
        shutil.copyfile(FIXTURE / "analysis.json", args.analysis_file)
        return 0
    if not args.analysis_file.is_file():
        raise ValueError("Plan and Code require saved Analyze evidence")
    if args.stage == "plan":
        shutil.copyfile(FIXTURE / "plan.json", args.plan_file)
        return 0
    if not args.plan_file.is_file():
        raise ValueError("Code requires saved Plan evidence")
    source = FIXTURE / TEST_PATH.name
    target = args.output_dir / TEST_PATH
    target.parent.mkdir(parents=True)
    shutil.copyfile(source, target)
    write_json(args.output_dir / "result.json", {
        "path": str(TEST_PATH), "content_sha256": digest(target.read_bytes()),
    })
    config = {
        "fake_model": True,
        "fixture_test_sha256": digest(source.read_bytes()),
        "source_paths": args.source_path,
    }
    config_bytes = (json.dumps(config, indent=2, sort_keys=True) + "\n").encode()
    (args.output_dir / "sanitized-config.json").write_bytes(config_bytes)
    write_json(args.output_dir / "invocation.json", {
        "candidate": "fake-external-no-model",
        "tool_version": "1.0-fixture",
        "model_id": args.model_id,
        "endpoint_id": "enmaas-devshift",
        "prompt_sha256": digest(args.objective.read_bytes()),
        "config_sha256": digest(config_bytes),
        "tool_source_revision": digest(Path("/adapter").read_bytes()),
        "redacted_command": [
            "/adapter", "--stage", "code", "ENMAAS_API_KEY=[REDACTED]"
        ],
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
