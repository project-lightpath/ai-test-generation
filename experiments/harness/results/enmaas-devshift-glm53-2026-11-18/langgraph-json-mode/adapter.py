"""Single-run FLPATH-5050 LangGraph candidate adapter."""

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from openai import OpenAI
from openai.types.chat import ChatCompletion

SOURCE_PATHS = (
    "src/main/java/com/projectlightpath/fixture/api/TransferResource.java",
    "src/main/java/com/projectlightpath/fixture/service/DefaultTransferService.java",
    "src/main/java/com/projectlightpath/fixture/target/UserAuthPublicKey.java",
    "src/test/java/com/projectlightpath/fixture/TransferResourceTest.java",
)
REQUEST_TIMEOUT_SECONDS = 300
MAX_RETRIES = 0
MAX_TOKENS = 16384


class CandidateState(TypedDict, total=False):
    objective: str
    source_context: str
    file_path: str
    content: str


def digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def candidate_path(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.parts[:3] != ("src", "test", "java"):
        raise ValueError("candidate test must stay under src/test/java")
    if not path.name.endswith("Test.java"):
        raise ValueError("candidate test filename must end with Test.java")
    return Path(*path.parts)


def write_response_diagnostics(response: ChatCompletion, output_dir: Path) -> None:
    choice = response.choices[0]
    message = choice.message
    extra = message.model_extra or {}
    reasoning = getattr(message, "reasoning_content", None) or extra.get("reasoning_content")
    usage = response.usage
    details = usage.completion_tokens_details if usage else None
    diagnostics = {
        "finish_reason": choice.finish_reason,
        "content_length": len(message.content or ""),
        "reasoning_content_length": len(reasoning) if isinstance(reasoning, str) else 0,
        "usage": {
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "completion_tokens": usage.completion_tokens if usage else None,
            "total_tokens": usage.total_tokens if usage else None,
            "reasoning_tokens": details.reasoning_tokens if details else None,
        },
    }
    (output_dir / "response-diagnostics.json").write_text(
        json.dumps(diagnostics, sort_keys=True, indent=2) + "\n")


def parse_candidate_response(response: ChatCompletion, output_dir: Path) -> CandidateState:
    try:
        payload = json.loads(response.choices[0].message.content or "")
    except json.JSONDecodeError:
        write_response_diagnostics(response, output_dir)
        raise
    if not isinstance(payload, dict):
        write_response_diagnostics(response, output_dir)
        raise ValueError("model output must be a JSON object")
    return {"file_path": payload.get("file_path"), "content": payload.get("content")}


def write_evidence(args: argparse.Namespace, objective: bytes,
                   context: list[tuple[str, bytes]]) -> None:
    version = importlib.metadata.version("langgraph")
    configuration = {
        "candidate": "langgraph", "tool_version": version,
        "model_id": args.model_id, "endpoint_id": args.endpoint_id,
        "base_url": args.base_url, "provider": "openai-compatible",
        "model_client": "openai.OpenAI", "model_client_version": importlib.metadata.version("openai"),
        "graph_nodes": ["generate", "validate"],
        "request_limits": {"timeout_seconds": REQUEST_TIMEOUT_SECONDS,
                           "max_retries": MAX_RETRIES, "max_tokens": MAX_TOKENS},
        "credential": "[REDACTED]",
        "source_context": {name: digest(content) for name, content in context},
        "adapter_sha256": digest(Path(__file__).read_bytes()),
    }
    config_bytes = (json.dumps(configuration, sort_keys=True, indent=2) + "\n").encode()
    (args.output_dir / "sanitized-config.json").write_bytes(config_bytes)
    invocation = {
        "candidate": "langgraph", "tool_version": version,
        "model_id": args.model_id, "endpoint_id": args.endpoint_id,
        "base_url": args.base_url,
        "redacted_command": ["python3", "adapter.py", "--fixture-root", str(args.fixture_root),
                             "--objective", str(args.objective), "--output-dir", str(args.output_dir),
                             "--base-url", args.base_url, "--model-id", args.model_id,
                             "--endpoint-id", args.endpoint_id,
                             "ENMAAS_API_KEY=[REDACTED]"],
        "prompt_sha256": digest(objective), "config_sha256": digest(config_bytes),
        "tool_source_revision": configuration["adapter_sha256"],
    }
    (args.output_dir / "invocation.json").write_text(json.dumps(invocation, sort_keys=True, indent=2) + "\n")


def run_candidate(args: argparse.Namespace, objective: bytes,
                  context: list[tuple[str, bytes]], api_key: str) -> Path:
    source_context = "\n\n".join(f"File: {name}\n{content.decode()}" for name, content in context)
    client = OpenAI(base_url=args.base_url, api_key=api_key,
                    timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_RETRIES)

    def generate(state: CandidateState) -> CandidateState:
        response = client.chat.completions.create(
            model=args.model_id,
            messages=[
                {"role": "system", "content": "You are a Java 21 Quarkus test engineer. "
                 "Use these repository files as read-only context. Create only one new native test. "
                 "Return one JSON object with string fields file_path and content. "
                 "Do not claim you executed the test.\n\n" + state["source_context"]},
                {"role": "user", "content": state["objective"]},
            ],
            response_format={"type": "json_object"},
            max_tokens=MAX_TOKENS,
        )
        return parse_candidate_response(response, args.output_dir)

    def validate(state: CandidateState) -> CandidateState:
        if not isinstance(state.get("file_path"), str) or not isinstance(state.get("content"), str):
            raise ValueError("model output must contain file_path and content strings")
        target = candidate_path(state["file_path"])
        if not state["content"].strip() or (args.fixture_root / target).exists():
            raise ValueError("candidate content is empty or would replace an existing test")
        return {"file_path": state["file_path"], "content": state["content"]}

    graph = StateGraph(CandidateState)
    graph.add_node("generate", generate)
    graph.add_node("validate", validate)
    graph.add_edge(START, "generate")
    graph.add_edge("generate", "validate")
    graph.add_edge("validate", END)
    generated = graph.compile().invoke({"objective": objective.decode(), "source_context": source_context})
    target = candidate_path(generated["file_path"])
    output_file = args.output_dir / target
    output_file.parent.mkdir(parents=True)
    output_file.write_text(generated["content"])
    (args.output_dir / "result.json").write_text(json.dumps({
        "path": str(target), "content_sha256": digest(output_file.read_bytes())},
        sort_keys=True, indent=2) + "\n")
    return output_file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-root", required=True, type=Path)
    parser.add_argument("--objective", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--base-url", default="https://api.enmaas.devshift.net/v1")
    parser.add_argument("--model-id", default="rits/zai-org/glm-5-3")
    parser.add_argument("--endpoint-id", default="enmaas-devshift")
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
