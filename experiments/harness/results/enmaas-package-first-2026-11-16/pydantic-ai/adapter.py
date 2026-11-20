"""Single-run FLPATH-5050 Pydantic AI candidate adapter."""

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai_compatible import OpenAICompatibleProvider

SOURCE_PATHS = (
    "src/main/java/com/projectlightpath/fixture/api/TransferResource.java",
    "src/main/java/com/projectlightpath/fixture/service/DefaultTransferService.java",
    "src/main/java/com/projectlightpath/fixture/target/UserAuthPublicKey.java",
    "src/test/java/com/projectlightpath/fixture/TransferResourceTest.java",
)


class CandidateTest(BaseModel):
    file_path: str = Field(description="New Java test path under src/test/java, ending Test.java")
    content: str = Field(description="Complete Java source for that test file")


def digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def candidate_path(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.parts[:3] != ("src", "test", "java"):
        raise ValueError("candidate test must stay under src/test/java")
    if not path.name.endswith("Test.java"):
        raise ValueError("candidate test filename must end with Test.java")
    return Path(*path.parts)


def write_evidence(args: argparse.Namespace, objective: bytes,
                   context: list[tuple[str, bytes]]) -> None:
    version = importlib.metadata.version("pydantic-ai-slim")
    configuration = {
        "candidate": "pydantic-ai", "tool_version": version,
        "model_id": args.model_id, "endpoint_id": "enmaas-rhdp",
        "base_url": args.base_url, "provider": "OpenAICompatibleProvider",
        "model_class": "OpenAIChatModel", "output_type": "CandidateTest(file_path, content)",
        "credential": "[REDACTED]",
        "source_context": {name: digest(content) for name, content in context},
        "adapter_sha256": digest(Path(__file__).read_bytes()),
    }
    config_bytes = (json.dumps(configuration, sort_keys=True, indent=2) + "\n").encode()
    (args.output_dir / "sanitized-config.json").write_bytes(config_bytes)
    invocation = {
        "candidate": "pydantic-ai", "tool_version": version,
        "model_id": args.model_id, "endpoint_id": "enmaas-rhdp",
        "redacted_command": ["python3", "adapter.py", "--fixture-root", str(args.fixture_root),
                             "--objective", str(args.objective), "--output-dir", str(args.output_dir),
                             "ENMAAS_API_KEY=[REDACTED]"],
        "prompt_sha256": digest(objective), "config_sha256": digest(config_bytes),
        "tool_source_revision": configuration["adapter_sha256"],
    }
    (args.output_dir / "invocation.json").write_text(json.dumps(invocation, sort_keys=True, indent=2) + "\n")


def run_candidate(args: argparse.Namespace, objective: bytes,
                  context: list[tuple[str, bytes]], api_key: str) -> Path:
    source_context = "\n\n".join(f"File: {name}\n{content.decode()}" for name, content in context)
    model = OpenAIChatModel(args.model_id, provider=OpenAICompatibleProvider(
        "enmaas-rhdp", base_url=args.base_url, api_key=api_key))
    agent = Agent(model, output_type=CandidateTest,
                  instructions="You are a Java 21 Quarkus test engineer. Use these repository files as read-only context. "
                               "Create only one new native test. Do not claim you executed it.\n\n" + source_context)
    generated = agent.run_sync(objective.decode()).output
    target = candidate_path(generated.file_path)
    if not generated.content.strip() or (args.fixture_root / target).exists():
        raise ValueError("candidate content is empty or would replace an existing test")
    output_file = args.output_dir / target
    output_file.parent.mkdir(parents=True)
    output_file.write_text(generated.content)
    (args.output_dir / "result.json").write_text(json.dumps({
        "path": str(target), "content_sha256": digest(output_file.read_bytes())},
        sort_keys=True, indent=2) + "\n")
    return output_file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-root", required=True, type=Path)
    parser.add_argument("--objective", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--base-url", default="https://maas-rhdp.apps.maas.redhatworkshops.io/v1")
    parser.add_argument("--model-id", default="gpt-oss-120b")
    args = parser.parse_args()
    api_key = os.environ.get("ENMAAS_API_KEY")
    if not api_key:
        parser.error("ENMAAS_API_KEY must be set")
    if args.output_dir.exists():
        parser.error("output directory must be new")
    objective = args.objective.read_bytes()
    context = [(name, (args.fixture_root / name).read_bytes()) for name in SOURCE_PATHS]
    args.output_dir.mkdir(parents=True)
    write_evidence(args, objective, context)
    try:
        print(run_candidate(args, objective, context, api_key))
        return 0
    except Exception as error:
        (args.output_dir / "error.json").write_text(json.dumps({
            "error_type": type(error).__name__}, sort_keys=True, indent=2) + "\n")
        print(f"candidate_failed: {type(error).__name__}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
