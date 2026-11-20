"""FLPATH-5050 LangGraph candidate adapter with optional staged analysis."""

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
STAGE_FIELDS = {
    "analysis": {"relevant_files": "array", "existing_coverage": "string",
                 "gap": "string", "feasibility": "string"},
    "plan": {"test_path": "string", "setup": "string", "assertions": "array",
             "baseline_expectation": "string", "mutant_expectation": "string"},
    "code": {"file_path": "string", "content": "string"},
}


REASON_CODES = frozenset(("missing_tool_call", "invalid_tool_arguments",
                          "missing_stage_fields", "invalid_test_path"))


class CandidateStageError(ValueError):
    def __init__(self, reason_code: str) -> None:
        fixed_code = reason_code if reason_code in REASON_CODES else "other"
        super().__init__(fixed_code)
        self.reason_code = fixed_code


def error_reason_code(error: Exception) -> str:
    return error.reason_code if isinstance(error, CandidateStageError) else "other"


class CandidateState(TypedDict, total=False):
    objective: str
    source_context: str
    payload: dict


def digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def candidate_path(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.parts[:3] != ("src", "test", "java"):
        raise ValueError("candidate test must stay under src/test/java")
    if not path.name.endswith("Test.java"):
        raise ValueError("candidate test filename must end with Test.java")
    return Path(*path.parts)


def stage_tool(stage: str) -> dict:
    fields = STAGE_FIELDS[stage]
    properties = {name: {"type": kind} for name, kind in fields.items()}
    if "relevant_files" in properties:
        properties["relevant_files"]["items"] = {"type": "string"}
    if "assertions" in properties:
        properties["assertions"]["items"] = {"type": "string"}
    return {"type": "function", "function": {
        "name": "create_test" if stage == "code" else "submit_" + stage,
        "description": "Submit one new Java test file" if stage == "code"
                       else "Submit the " + stage + " artifact",
        "parameters": {"type": "object", "properties": properties,
                       "required": list(fields), "additionalProperties": False}}}


def write_response_diagnostics(response: ChatCompletion, output_dir: Path) -> None:
    choice = response.choices[0]
    message = choice.message
    extra = message.model_extra or {}
    reasoning = getattr(message, "reasoning_content", None) or extra.get("reasoning_content")
    usage = response.usage
    details = usage.completion_tokens_details if usage else None
    diagnostics = {
        "finish_reason": choice.finish_reason if choice.finish_reason in
                         ("stop", "length", "tool_calls", "content_filter", "function_call")
                         else "other",
        "content_length": len(message.content or ""),
        "tool_call_count": len(message.tool_calls or []),
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


def parse_candidate_response(response: ChatCompletion, output_dir: Path,
                             tool_name: str = "create_test") -> dict:
    calls = response.choices[0].message.tool_calls or []
    if len(calls) != 1 or calls[0].type != "function" or calls[0].function.name != tool_name:
        raise CandidateStageError("missing_tool_call")
    try:
        payload = json.loads(calls[0].function.arguments)
    except json.JSONDecodeError as error:
        raise CandidateStageError("invalid_tool_arguments") from error
    if not isinstance(payload, dict):
        raise CandidateStageError("invalid_tool_arguments")
    return payload


def write_evidence(args: argparse.Namespace, objective: bytes,
                   context: list[tuple[str, bytes]]) -> None:
    version = importlib.metadata.version("langgraph")
    configuration = {
        "candidate": "langgraph", "tool_version": version,
        "model_id": args.model_id, "endpoint_id": args.endpoint_id,
        "base_url": args.base_url, "provider": "openai-compatible",
        "model_client": "openai.OpenAI", "model_client_version": importlib.metadata.version("openai"),
        "graph_nodes": ["generate", "validate"],
        "response_mode": "required create_test function call" if args.stage == "code"
                         else "required submit_" + args.stage + " function call",
        "request_limits": {"timeout_seconds": REQUEST_TIMEOUT_SECONDS,
                           "max_retries": MAX_RETRIES, "max_tokens": MAX_TOKENS},
        "stage": args.stage, "profile": args.profile,
        "credential": "[REDACTED]",
        "source_context": {name: digest(content) for name, content in context},
        "adapter_sha256": digest(Path(__file__).read_bytes()),
    }
    if args.stage == "code" and args.analysis_file and args.plan_file:
        configuration["stage_sha256"] = {
            "analysis.json": digest(args.analysis_file.read_bytes()),
            "plan.json": digest(args.plan_file.read_bytes()),
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
                             "--endpoint-id", args.endpoint_id, "--profile", args.profile,
                             "--stage", args.stage, "ENMAAS_API_KEY=[REDACTED]"],
        "prompt_sha256": digest(objective), "config_sha256": digest(config_bytes),
        "tool_source_revision": configuration["adapter_sha256"],
    }
    (args.output_dir / "invocation.json").write_text(json.dumps(invocation, sort_keys=True, indent=2) + "\n")


def run_candidate(args: argparse.Namespace, objective: bytes,
                  context: list[tuple[str, bytes]], api_key: str) -> Path:
    source_context = "\n\n".join(f"File: {name}\n{content.decode()}" for name, content in context)
    client = OpenAI(base_url=args.base_url, api_key=api_key,
                    timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_RETRIES)
    prior = ""
    if args.analysis_file and args.stage in ("plan", "code"):
        prior += "\nCandidate analysis:\n" + args.analysis_file.read_text()
    if args.plan_file and args.stage == "code":
        prior += "\nCandidate plan:\n" + args.plan_file.read_text()
    prompt = (args.instruction or objective.decode()) + prior
    tool = stage_tool(args.stage)
    tool_name = tool["function"]["name"]

    def generate(state: CandidateState) -> CandidateState:
        if args.profile == "quarkus" and args.stage == "code":
            system_prompt = ("You are a Java 21 Quarkus test engineer. "
                             "Use these repository files as read-only context. Create only one new native test. "
                             "Call create_test once with the new test path and complete Java source. "
                             "Do not claim you executed the test.\n\n" + state["source_context"])
        else:
            system_prompt = (f"You are a {args.framework} test engineer. "
                             "Use these repository files as read-only context. "
                             "Do not claim you executed tests.\n\n" + state["source_context"])
        response = client.chat.completions.create(
            model=args.model_id,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": state["objective"]},
            ],
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": tool_name}},
            max_tokens=MAX_TOKENS,
        )
        write_response_diagnostics(response, args.output_dir)
        return {"payload": parse_candidate_response(response, args.output_dir, tool_name)}

    def validate(state: CandidateState) -> CandidateState:
        payload = state["payload"]
        for name, kind in STAGE_FIELDS[args.stage].items():
            value = payload.get(name)
            if not value:
                raise CandidateStageError("missing_stage_fields")
            if (kind == "string" and not isinstance(value, str)) or (kind == "array" and not isinstance(value, list)):
                raise CandidateStageError("invalid_tool_arguments")
        if args.stage == "code":
            try:
                target = candidate_path(payload["file_path"])
            except (TypeError, ValueError) as error:
                raise CandidateStageError("invalid_test_path") from error
            if (args.fixture_root / target).exists():
                raise CandidateStageError("invalid_test_path")
        return {"payload": payload}

    graph = StateGraph(CandidateState)
    graph.add_node("generate", generate)
    graph.add_node("validate", validate)
    graph.add_edge(START, "generate")
    graph.add_edge("generate", "validate")
    graph.add_edge("validate", END)
    generated = graph.compile().invoke({"objective": prompt, "source_context": source_context})["payload"]
    if args.stage != "code":
        target = args.analysis_file if args.stage == "analysis" else args.plan_file
        target.write_text(json.dumps(generated, sort_keys=True, indent=2) + "\n")
        return target
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
    parser.add_argument("--profile", default="quarkus")
    parser.add_argument("--stage", choices=("analysis", "plan", "code"), default="code")
    parser.add_argument("--framework", default="Java 21 Quarkus")
    parser.add_argument("--source-path", action="append")
    parser.add_argument("--instruction")
    parser.add_argument("--analysis-file", type=Path)
    parser.add_argument("--plan-file", type=Path)
    args = parser.parse_args()
    api_key = os.environ.get("ENMAAS_API_KEY")
    if not api_key:
        parser.error("ENMAAS_API_KEY must be set")
    if args.output_dir.exists():
        parser.error("output directory must be new")
    objective = args.objective.read_bytes()
    paths = args.source_path or SOURCE_PATHS
    context = [(name, (args.fixture_root / name).read_bytes()) for name in paths]
    args.output_dir.mkdir(parents=True)
    write_evidence(args, objective, context)
    try:
        print(run_candidate(args, objective, context, api_key))
        return 0
    except Exception as error:
        (args.output_dir / "error.json").write_text(json.dumps({
            "error_type": type(error).__name__,
            "reason_code": error_reason_code(error)}, sort_keys=True, indent=2) + "\n")
        print(f"candidate_failed: {type(error).__name__}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
