"""Evaluate one candidate-generated Java test against a fixed behavior mutation.

FLPATH-5050 experiment: accepts test output from any candidate. This script
does not invoke a model or decide which agent should be adopted.
"""

import argparse
import hashlib
import json
import re
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional
from xml.etree import ElementTree

from fixture_profiles import FixtureProfile, QUARKUS, PROFILES, get_profile

FIXTURE_REVISION = "2aac181ae63d938c118bac133d33fb8ae19eeec9"
FIXTURE_DIGEST = "sha256:5e19aa18e2019ecf9fe2559648002bac97467da685f5ef3fb4eefa0251080efe"
MUTATION_PATH = Path("src/main/java/com/projectlightpath/fixture/target/UserAuthPublicKey.java")
ORIGINAL_LINE = "return strict && user != null && !user.isBlank();"
MUTATED_LINE = "return true;"
DEFAULT_IMAGE = "docker.io/library/maven:3.9.11-eclipse-temurin-21"
SHARED_OBJECTIVE = Path(__file__).resolve().parent / "fixtures/blank-user-objective.md"
MAX_LOG_BYTES = 16 * 1024 * 1024
SPRING_COVERAGE_TARGETS = {
    "spring-transfer": ("com/redhat/lightwell/service/TransactionService", "transfer"),
    "spring-xml-deposit": ("com/redhat/lightwell/service/StatementImportService", "parseAndCreateTransactions"),
}


def sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        if relative.parts[0] == "target" or ".git" in relative.parts:
            continue
        digest.update(relative.as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return "sha256:" + digest.hexdigest()


def test_target(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.parts[:3] != ("src", "test", "java"):
        raise ValueError("test path must stay under src/test/java")
    if not path.name.endswith("Test.java"):
        raise ValueError("test file must end with Test.java")
    return Path(*path.parts)



def load_invocation(path: Path, args: argparse.Namespace) -> Dict[str, object]:
    record = json.loads(path.read_text())
    identities = {"candidate": args.candidate, "tool_version": args.tool_version,
                  "model_id": args.model_id, "endpoint_id": args.endpoint_id}
    if not isinstance(record, dict) or any(record.get(k) != v for k, v in identities.items()):
        raise ValueError("invocation identities must match evaluator arguments")
    command = record.get("redacted_command")
    if not isinstance(command, list) or any(not isinstance(part, str) for part in command):
        raise ValueError("invocation needs a redacted command argument list")
    secret_field = re.compile(r"(?i)(api[_-]?key|token|password|secret|authorization|bearer)")
    placeholders = {"[REDACTED]", "<redacted>"}
    for index, part in enumerate(command):
        if not secret_field.search(part):
            continue
        inline_value = part.split("=", 1)[1] if "=" in part else None
        following = command[index + 1] if index + 1 < len(command) else None
        if inline_value is not None and inline_value not in placeholders:
            raise ValueError("invocation command may contain a secret; use [REDACTED]")
        if inline_value is None and following not in placeholders:
            raise ValueError("invocation command may contain a secret; use [REDACTED]")
    for field in ("prompt_sha256", "config_sha256"):
        value = record.get(field)
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value)):
            raise ValueError("invocation " + field + " must be a SHA-256 digest")
    if args.model_id != "none" and (not command or not record.get("prompt_sha256")
                                     or not record.get("config_sha256")):
        raise ValueError("real-model invocation needs command, prompt and config digests")
    profile = get_profile(getattr(args, "profile", "quarkus"))
    if args.model_id != "none" and record.get("prompt_sha256") != sha256(profile.objective.read_bytes()):
        raise ValueError("candidate prompt digest does not match shared objective")
    return {**identities, "redacted_command": command,
            "prompt_sha256": record.get("prompt_sha256"),
            "config_sha256": record.get("config_sha256"),
            "tool_source_revision": record.get("tool_source_revision"),
            "record_sha256": sha256(path.read_bytes())}


def copy_fixture(source: Path, destination: Path) -> None:
    def ignore_build(directory: str, names: List[str]) -> List[str]:
        ignored = [".git"] if ".git" in names else []
        if Path(directory) == source and "target" in names:
            ignored.append("target")
        return ignored

    shutil.copytree(str(source), str(destination), ignore=ignore_build)


def mutate_fixture(root: Path, profile: FixtureProfile = QUARKUS) -> str:
    path = root / profile.mutation_path
    source = path.read_text()
    if source.count(profile.original_line) != 1:
        raise ValueError("fixture mutation anchor must occur exactly once")
    path.write_text(source.replace(profile.original_line, profile.mutated_line))
    return sha256(path.read_bytes())


def surefire_result(root: Path, class_name: str) -> Dict[str, object]:
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0,
              "assertion_failures": 0}
    directory = root / "target/surefire-reports"
    if workspace_path_has_link(root, Path("target/surefire-reports")):
        return {**counts, "report_status": "linked"}
    if not directory.is_dir():
        return {**counts, "report_status": "missing"}
    matching = []
    for report in directory.glob("TEST-*.xml"):
        if report.is_symlink() or not report.is_file() or report.stat().st_size > MAX_LOG_BYTES:
            return {**counts, "report_status": "invalid"}
        try:
            suite = ElementTree.parse(str(report)).getroot()
        except (ElementTree.ParseError, OSError):
            return {**counts, "report_status": "invalid"}
        name = suite.attrib.get("name", "")
        if name == class_name or name.endswith("." + class_name):
            matching.append(suite)
    if len(matching) != 1:
        return {**counts, "report_status": "missing"}
    suite = matching[0]
    try:
        counts.update({field: int(suite.attrib.get(field, 0))
                       for field in ("tests", "failures", "errors", "skipped")})
    except ValueError:
        return {**counts, "report_status": "invalid"}
    counts["assertion_failures"] = sum(
        1 for test in suite.findall("testcase") for failure in test.findall("failure")
        if any(kind in failure.attrib.get("type", "")
               for kind in ("AssertionError", "AssertionFailedError")))
    return {**counts, "report_status": "valid"}


def _limit_log_file() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_LOG_BYTES, MAX_LOG_BYTES))


def _remove_container(cid_file: Path) -> None:
    if not cid_file.is_file():
        return
    cid = cid_file.read_text().strip()
    if cid:
        subprocess.run(["podman", "rm", "-f", cid], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)


def _run_maven_phase(root: Path, label: str, phase: str, cache: Path,
                     image: str, class_name: str,
                     profile: FixtureProfile = QUARKUS) -> Dict[str, object]:
    cid_file = root.parent / (label + "-" + phase + ".cid")
    goal = (["-DskipTests", "package"] if phase == "package" else
            ["verify"] if phase == "verify" else
            ["-Dtest=" + class_name, "-Dsurefire.failIfNoSpecifiedTests=true", "test"])
    command = [
        "podman", "run", "--rm", "--cidfile", str(cid_file), "--pull=never",
        "--network=none", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
        "--cap-drop=all", "--security-opt=no-new-privileges",
        "--pids-limit=256", "--memory=2g", "--cpus=2",
        "--ulimit", "fsize=134217728:134217728",
        "-e", "JAVA_TOOL_OPTIONS=-XX:ActiveProcessorCount=2",
        "-v", str(root) + ":/workspace:Z",
        "-v", str(cache) + ":/root/.m2/repository:ro,Z",
        "-w", "/workspace", image, "mvn", "-o", "-B", "-ntp",
    ] + list(profile.maven_options) + goal
    log = root.parent / (label + "-" + phase + ".log")
    started_at = time.monotonic()
    try:
        with log.open("wb") as output:
            result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT,
                                    timeout=900, preexec_fn=_limit_log_file,
                                    start_new_session=True)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "exit_code": None, "log": str(log),
                "command": command, "elapsed_ms": int((time.monotonic() - started_at) * 1000)}
    finally:
        _remove_container(cid_file)
    status = ("output_limit" if log.stat().st_size >= MAX_LOG_BYTES else
              "passed" if result.returncode == 0 else "failed")
    return {"status": status, "exit_code": result.returncode,
            "log": str(log), "command": command,
            "elapsed_ms": int((time.monotonic() - started_at) * 1000)}


def run_case(root: Path, label: str, class_name: str, cache: Path, image: str,
             profile: FixtureProfile = QUARKUS) -> Dict[str, object]:
    package = _run_maven_phase(root, label, "package", cache, image, class_name, profile)
    if package["status"] != "passed":
        reason = "failure" if package["status"] == "failed" else package["status"]
        return {"status": "blocked_package_" + reason, "exit_code": package["exit_code"],
                "tests": {}, "log": package["log"], "command": package["command"],
                "package": package, "test": None}
    test = _run_maven_phase(root, label, "test", cache, image, class_name, profile)
    if test["status"] in ("timeout", "output_limit"):
        return {"status": "blocked_" + test["status"], "exit_code": test["exit_code"],
                "tests": {}, "log": test["log"], "command": test["command"],
                "package": package, "test": test}
    counts = surefire_result(root, class_name)
    if counts["report_status"] in ("linked", "invalid"):
        status = "blocked_report_invalid"
    elif counts["tests"] == 0:
        status = "blocked_no_tests"
    elif counts["tests"] <= counts["skipped"]:
        status = "blocked_no_executed_tests"
    elif counts["errors"] > 0:
        status = "blocked_test_error"
    elif counts["failures"] > 0:
        status = ("failed_assertion" if test["exit_code"] != 0
                  and counts["assertion_failures"] == counts["failures"]
                  else "blocked_test_failure")
    elif test["exit_code"] != 0:
        status = "blocked_runner_failure"
    else:
        status = "passed"
    return {"status": status, "exit_code": test["exit_code"], "tests": counts,
            "log": test["log"], "command": test["command"],
            "package": package, "test": test}


def verify_inputs(args: argparse.Namespace, target: Path,
                  profile: FixtureProfile = QUARKUS) -> str:
    if not args.candidate_test_file.is_file() or not args.maven_cache.is_dir():
        raise ValueError("candidate test file or Maven cache is missing")
    if args.output_root.exists():
        raise ValueError("output directory must be new")
    digest = tree_digest(args.fixture_root.resolve())
    if digest != profile.tree_sha256:
        raise ValueError("fixture content differs from pinned " + profile.name + " revision")
    if (args.fixture_root / target).exists():
        raise ValueError("candidate test would replace an existing fixture test")
    for value in (args.candidate, args.tool_version, args.model_id, args.endpoint_id):
        if not value.strip():
            raise ValueError("candidate, tool version, model, and endpoint identity are required")
    return digest



def generated_test_metrics(path: Path) -> Dict[str, object]:
    content = path.read_bytes()
    lines = content.decode("utf-8", errors="replace").splitlines()
    return {"bytes": len(content), "physical_lines": len(lines),
            "nonblank_lines": sum(bool(line.strip()) for line in lines),
            "sha256": sha256(content)}


def spring_method_coverage(root: Path, class_name: str, method_name: str) -> Dict[str, object]:
    report = root / "target/site/jacoco/jacoco.xml"
    if any(path.is_symlink() for path in (report, report.parent, report.parent.parent,
                                          report.parent.parent.parent)):
        return {"status": "coverage_unknown", "reason": "jacoco_report_linked"}
    if not report.is_file():
        return {"status": "coverage_unknown", "reason": "jacoco_report_missing"}
    try:
        if report.stat().st_size > MAX_LOG_BYTES:
            return {"status": "coverage_unknown", "reason": "jacoco_report_oversized"}
        document = ElementTree.parse(str(report)).getroot()
        methods = [method for package in document.findall("package")
                   for source_class in package.findall("class")
                   if source_class.attrib.get("name") == class_name
                   for method in source_class.findall("method")
                   if method.attrib.get("name") == method_name]
        if len(methods) != 1:
            return {"status": "coverage_unknown", "reason": "target_method_not_unique"}
        counters = {item.attrib["type"]: {"covered": int(item.attrib["covered"]),
                                                 "missed": int(item.attrib["missed"])}
                    for item in methods[0].findall("counter")}
        if not all(kind in counters for kind in ("LINE", "BRANCH")):
            return {"status": "coverage_unknown", "reason": "target_counters_missing"}
    except (ElementTree.ParseError, OSError, KeyError, ValueError):
        return {"status": "coverage_unknown", "reason": "jacoco_report_invalid"}
    return {"status": "measured", "method": methods[0].attrib,
            "line": counters["LINE"], "branch": counters["BRANCH"],
            "report_sha256": sha256(report.read_bytes())}



def spring_transfer_coverage(root: Path) -> Dict[str, object]:
    class_name, method_name = SPRING_COVERAGE_TARGETS["spring-transfer"]
    return spring_method_coverage(root, class_name, method_name)


def workspace_path_has_link(root: Path, relative: Path) -> bool:
    current = root
    if current.is_symlink():
        return True
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def spring_coverage_comparison(fixture: Path, candidate_file: Path, target: Path,
                               output: Path, cache: Path, image: str,
                               profile: FixtureProfile) -> Dict[str, object]:
    target_method = SPRING_COVERAGE_TARGETS.get(profile.name)
    if target_method is None:
        return {"status": "not_applicable", "reason": "fixture_has_no_coverage_contract"}
    control = output / "coverage-control"
    with_candidate = output / "coverage-candidate"
    copy_fixture(fixture, control)
    copy_fixture(fixture, with_candidate)
    installed = with_candidate / target
    installed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(candidate_file), str(installed))
    runs = {"existing_suite": _run_maven_phase(control, "coverage-control", "verify",
                                               cache, image, "", profile),
            "existing_plus_candidate": _run_maven_phase(with_candidate, "coverage-candidate",
                                                        "verify", cache, image, "", profile)}
    if any(run["status"] != "passed" for run in runs.values()):
        return {"status": "coverage_unknown", "reason": "coverage_build_failed", "runs": runs}
    measured = {"existing_suite": spring_method_coverage(control, *target_method),
                "existing_plus_candidate": spring_method_coverage(with_candidate, *target_method)}
    if any(value["status"] != "measured" for value in measured.values()):
        return {"status": "coverage_unknown", "reason": "coverage_report_unavailable",
                "runs": runs, "measurements": measured}
    delta = {kind: measured["existing_plus_candidate"][kind]["covered"] -
            measured["existing_suite"][kind]["covered"] for kind in ("line", "branch")}
    return {"status": "measured", "runs": runs, "measurements": measured,
            "covered_delta": delta}


def evaluate(args: argparse.Namespace) -> Dict[str, object]:
    profile = get_profile(getattr(args, "profile", "quarkus"))
    fixture = args.fixture_root.resolve()
    candidate_file = args.candidate_test_file.resolve()
    cache = args.maven_cache.resolve()
    output = args.output_root.resolve()
    target = test_target(args.candidate_test_path)
    invocation = load_invocation(args.invocation_record, args)
    actual_fixture_digest = verify_inputs(args, target, profile)
    image_name = args.image or profile.maven_image
    image = subprocess.check_output(
        ["podman", "image", "inspect", "--format", "{{.Id}}", image_name],
        universal_newlines=True).strip()
    output.mkdir(parents=True)
    results = {}
    for label in ("baseline", "mutant"):
        root = output / label
        copy_fixture(fixture, root)
        installed = root / target
        installed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(candidate_file), str(installed))
        if label == "mutant":
            mutation_digest = mutate_fixture(root, profile)
        results[label] = run_case(root, label, target.stem, cache, image_name, profile)
    status = ("discriminates" if results["baseline"]["status"] == "passed"
              and results["mutant"]["status"] == "failed_assertion" else "inconclusive")
    coverage = spring_coverage_comparison(fixture, candidate_file, target, output, cache,
                                          image_name, profile)
    report = {
        "record_type": "flpath-5050.candidate-evaluation",
        "shared_objective_sha256": sha256(profile.objective.read_bytes()),
        "candidate": {"name": args.candidate, "tool_version": args.tool_version,
                      "model_id": args.model_id, "endpoint_id": args.endpoint_id},
        "invocation": invocation,
        "test": {"path": str(target), **generated_test_metrics(candidate_file)},
        "coverage": coverage,
        "fixture": {"repository": profile.repository,
                    "revision": profile.revision, "verified_tree_sha256": actual_fixture_digest},
        "mutation": {"path": str(profile.mutation_path), "old": profile.original_line,
                     "new": profile.mutated_line, "mutated_source_sha256": mutation_digest},
        "environment": {"image": image_name, "image_id": image,
                        "execution_contract": "offline-package-then-candidate-test",
                        "network": "disabled", "maven_cache": "offline-read-only",
                        "memory_limit": "2g", "cpu_limit": 2, "file_size_limit_bytes": 134217728},
        "result": status, "cases": results,
        "limitations": [
            "This checks sensitivity to one seeded application behavior change.",
            "It does not prove vulnerability remediation or model quality across tasks.",
            "Coverage measures the target method, not whether the test catches the seeded defect.",
            "A generated test that duplicates an existing test can still discriminate.",
            "Candidate invocation is external; its redacted command and digests are supplied by the caller.",
            "The writable workspace is not disk-quota isolated; run untrusted tests only on a disposable, quota-limited host.",
            "Surefire and JaCoCo XML are in candidate-writable workspaces; results are review signals, not tamper-proof proof.",
        ],
    }
    (output / "evaluation.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-test-file", required=True, type=Path)
    parser.add_argument("--candidate-test-path", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--tool-version", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--endpoint-id", required=True)
    parser.add_argument("--invocation-record", required=True, type=Path)
    parser.add_argument("--fixture-root", required=True, type=Path)
    parser.add_argument("--maven-cache", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="quarkus")
    parser.add_argument("--image", help="override the profile Maven image")
    args = parser.parse_args()
    try:
        report = evaluate(args)
    except (ValueError, OSError, json.JSONDecodeError, subprocess.CalledProcessError, ElementTree.ParseError) as error:
        parser.error(str(error))
    print(json.dumps({"result": report["result"],
                      "baseline": report["cases"]["baseline"]["status"],
                      "mutant": report["cases"]["mutant"]["status"]}))
    return 0 if report["result"] == "discriminates" else 2


if __name__ == "__main__":
    sys.exit(main())
