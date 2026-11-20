"""Build a reviewable FLPATH-5050 findings report from saved run evidence.

The report never executes candidates or reads credentials. Distinct provenance
cohorts stay separate so changed prompts, fixtures, models, or runners cannot
be mistaken for repeated trials.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from collections import Counter
from statistics import median
from typing import Dict, List, Optional, Tuple

SCHEMA = "flpath-5050.findings.v1"
MAX_EVIDENCE_JSON_BYTES = 16 * 1024 * 1024
MAX_GENERATED_TEST_BYTES = 16 * 1024 * 1024
STAGES = ("analysis", "plan", "code", "execute")
OUTCOMES = ("use", "use_with_fork", "reject", "insufficient_evidence")


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return "sha256:" + checksum.hexdigest()


def read_json(path: Path) -> Optional[Dict[str, object]]:
    if not path.is_file() or path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        return None
    if path.stat().st_size > MAX_EVIDENCE_JSON_BYTES:
        raise ValueError(f"evidence JSON exceeds size limit: {path}")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def evidence(root: Path, path: Path) -> Dict[str, str]:
    if path.is_symlink() or not path.is_file() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError(f"missing or linked evidence: {path}")
    relative = path.relative_to(root).as_posix()
    return {"path": relative, "sha256": digest(path)}


def cohort_identity(summary: Dict[str, object]) -> Dict[str, object]:
    fields = ("fixture_profile", "fixture_tree_sha256", "objective_sha256",
              "model_id", "endpoint_id", "runner_sha256", "evaluator_sha256",
              "run_spec_sha256")
    return {field: summary.get(field) for field in fields}


def timing(folder: Path, filename: str) -> Optional[int]:
    record = read_json(folder / filename)
    if not record:
        return None
    milliseconds = record.get("elapsed_ms")
    if isinstance(milliseconds, int) and milliseconds >= 0:
        return milliseconds
    seconds = record.get("elapsed_seconds")
    return round(seconds * 1000) if isinstance(seconds, (int, float)) and seconds >= 0 else None


def generated_test(root: Path, folder: Path, evaluation: Optional[Dict[str, object]]) -> Optional[Dict[str, object]]:
    if not evaluation or not isinstance(evaluation.get("test"), dict):
        return None
    relative = evaluation["test"].get("path")
    if not isinstance(relative, str):
        return None
    target = PurePosixPath(relative)
    if target.is_absolute() or ".." in target.parts or target.parts[:3] != ("src", "test", "java"):
        return None
    for workspace in ("generated", "workspace"):
        path = folder / workspace / relative
        if not path.is_file() or path.is_symlink():
            continue
        if any(parent.is_symlink() for parent in path.parents if parent != folder.parent):
            continue
        if path.stat().st_size > MAX_GENERATED_TEST_BYTES:
            return None
        content = path.read_bytes()
        lines = content.decode("utf-8", errors="replace").splitlines()
        actual_hash = "sha256:" + hashlib.sha256(content).hexdigest()
        return {"bytes": len(content), "physical_lines": len(lines),
                "nonblank_lines": sum(bool(line.strip()) for line in lines),
                "hash_matches": actual_hash == evaluation["test"].get("sha256"),
                "evidence": evidence(root, path)}
    return None


def phase_timings(evaluation: Optional[Dict[str, object]]) -> Dict[str, Optional[int]]:
    result: Dict[str, Optional[int]] = {}
    if not evaluation or not isinstance(evaluation.get("cases"), dict):
        return result
    for case in ("baseline", "mutant"):
        record = evaluation["cases"].get(case)
        if not isinstance(record, dict):
            continue
        for phase in ("package", "test"):
            value = record.get(phase)
            if isinstance(value, dict):
                result[f"{case}_{phase}"] = value.get("elapsed_ms")
    coverage = evaluation.get("coverage", {})
    if isinstance(coverage, dict):
        for name, value in coverage.get("runs", {}).items():
            if isinstance(value, dict):
                result[name] = value.get("elapsed_ms")
    return result


def archive_integrity(root: Path) -> Dict[str, object]:
    """Check an unsigned archive manifest for internal consistency, not authenticity."""
    manifest_path = root / "archive-manifest.json"
    if not manifest_path.exists() and not manifest_path.is_symlink():
        return {"status": "unverified", "issues": []}
    try:
        manifest = read_json(manifest_path)
        if not manifest or manifest.get("schema") != "flpath-5050.evidence-archive.v1":
            raise ValueError("archive_manifest_invalid")
        files = manifest.get("files")
        if not isinstance(files, dict) or len(files) > 300 or manifest.get("file_count") != len(files):
            raise ValueError("archive_manifest_invalid")
        total = 0
        for name, record in files.items():
            if not isinstance(name, str) or not isinstance(record, dict) or "\\" in name:
                raise ValueError("archive_manifest_path_invalid")
            relative = PurePosixPath(name)
            if (relative.is_absolute() or not relative.parts or ".." in relative.parts
                    or relative.as_posix() != name or name == "archive-manifest.json"):
                raise ValueError("archive_manifest_path_invalid")
            path = root.joinpath(*relative.parts)
            if (not path.is_file() or path.is_symlink()
                    or any(parent.is_symlink() for parent in path.parents)):
                raise ValueError("archive_file_missing_or_linked:" + name)
            size = path.stat().st_size
            if size > MAX_EVIDENCE_JSON_BYTES or record.get("bytes") != size:
                raise ValueError("archive_file_size_mismatch:" + name)
            total += size
            if total > 64 * 1024 * 1024 or record.get("sha256") != digest(path):
                raise ValueError("archive_file_hash_mismatch:" + name)
        actual = set()
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError("archive_contains_link")
            if path.is_file() and path != manifest_path:
                actual.add(path.relative_to(root).as_posix())
            elif not path.is_dir() and not path.is_file():
                raise ValueError("archive_contains_special_file")
        if actual != set(files) or manifest.get("total_bytes") != total:
            raise ValueError("archive_manifest_file_set_mismatch")
        summary = read_json(root / "summary.json")
        if (not summary or manifest.get("run_id") != summary.get("run_id")
                or manifest.get("evidence_scan") != "passed"
                or summary.get("evidence_scan") != "passed"):
            raise ValueError("archive_summary_mismatch")
        return {"status": "verified", "issues": []}
    except (ValueError, OSError, json.JSONDecodeError) as error:
        return {"status": "invalid", "issues": [str(error)[:300]]}


def checkpoint_integrity(root: Path, name: str, summary: Dict[str, object],
                         outcome: str) -> Dict[str, object]:
    """Verify the recorded checkpoint chain and retained candidate artifacts."""
    directory = root / "checkpoints" / name
    checkpoints = [directory / (stage + ".json") for stage in STAGES]
    if not any(path.exists() or path.is_symlink() for path in checkpoints):
        return {"status": "unverified", "issues": []}
    issues = []
    complete = True
    outputs_present = True
    seen = []
    previous = None
    for stage, path in zip(STAGES, checkpoints):
        if not path.exists() and not path.is_symlink():
            complete = False
            previous = None
            continue
        try:
            record = read_json(path)
            if (not record or record.get("schema") != "flpath-5050.stage-checkpoint.v1"
                    or record.get("candidate") != name or record.get("stage") != stage):
                raise ValueError("checkpoint_identity_invalid:" + stage)
            for field in ("fixture_tree_sha256", "objective_sha256", "model_id",
                          "endpoint_id", "run_spec_sha256"):
                if record.get(field) != summary.get(field):
                    raise ValueError("checkpoint_binding_mismatch:" + stage + ":" + field)
            if record.get("previous_checkpoint_sha256") != previous:
                raise ValueError("checkpoint_chain_mismatch:" + stage)
            outputs = record.get("outputs")
            if not isinstance(outputs, dict):
                raise ValueError("checkpoint_outputs_invalid:" + stage)
            seen.append((stage, record.get("status")))
            if record.get("status") != "completed":
                complete = False
                if outcome == "discriminates":
                    issues.append("checkpoint_status_mismatch:" + stage)
            for relative, expected in outputs.items():
                if not isinstance(relative, str) or "\\" in relative:
                    raise ValueError("checkpoint_output_path_invalid:" + stage)
                target = PurePosixPath(relative)
                if target.is_absolute() or not target.parts or ".." in target.parts or target.as_posix() != relative:
                    raise ValueError("checkpoint_output_path_invalid:" + stage)
                output = root.joinpath(name, *target.parts)
                if (not output.is_file() or output.is_symlink()
                        or any(parent.is_symlink() for parent in output.parents)):
                    complete = False
                    outputs_present = False
                    continue
                if output.stat().st_size > MAX_GENERATED_TEST_BYTES or digest(output) != expected:
                    raise ValueError("checkpoint_output_hash_mismatch:" + stage + ":" + relative)
            required = {"analysis": {"analysis-run.json", "analysis.json"},
                        "plan": {"plan-run.json", "plan.json"},
                        "code": {"generation.json"},
                        "execute": {"evaluation-run.json", "evaluation/evaluation.json"}}[stage]
            if record.get("status") == "completed" and not required.issubset(outputs):
                issues.append("checkpoint_required_outputs_missing:" + stage)
            if stage == "execute" and record.get("status") == "completed" and record.get("result") != outcome:
                issues.append("checkpoint_result_mismatch")
            previous = digest(path)
        except (ValueError, OSError, json.JSONDecodeError) as error:
            issues.append(str(error)[:300])
            complete = False
    # A blocked stage can end a valid prefix; the archive still binds its logs.
    prefix = [stage for stage, _ in seen] == list(STAGES[:len(seen)])
    blocked_prefix = (bool(seen) and prefix and outputs_present and seen[-1][1] == "blocked"
                      and all(status == "completed" for _, status in seen[:-1])
                      and outcome in ("blocked", "inconclusive"))
    if blocked_prefix:
        marker_name = {"analysis": "analysis-run.json", "plan": "plan-run.json",
                       "code": "generation.json", "execute": "evaluation-run.json"}[seen[-1][0]]
        try:
            marker = read_json(root / name / marker_name)
            blocked_prefix = bool(marker and isinstance(marker.get("status"), str))
        except (ValueError, OSError, json.JSONDecodeError):
            blocked_prefix = False
    return {"status": "invalid" if issues else "verified" if complete
            else "verified_partial" if blocked_prefix else "unverified",
            "issues": issues}


def stable_invocation_identity(folder: Path, invocation: Optional[Dict[str, object]]) -> Dict[str, object]:
    """Separate input configuration from recorded generated-stage hashes."""
    identity = {key: invocation.get(key) if invocation else None
                for key in ("candidate", "model_id", "endpoint_id", "prompt_sha256",
                            "tool_source_revision", "tool_version")}
    identity["stable_config_sha256"] = None
    identity["config_status"] = "unverified"
    if not invocation:
        return identity
    for path in (folder / "sanitized-config.json", folder / "generated/sanitized-config.json"):
        if not path.exists() and not path.is_symlink():
            continue
        config = read_json(path)
        if not config or invocation.get("config_sha256") != digest(path):
            identity["config_status"] = "invalid"
            return identity
        stage_hashes = config.get("stage_sha256")
        if not isinstance(stage_hashes, dict) or set(stage_hashes) != {"analysis.json", "plan.json"}:
            return identity
        for filename, expected in stage_hashes.items():
            stage_file = folder / filename
            if (not stage_file.is_file() or stage_file.is_symlink()
                    or any(parent.is_symlink() for parent in stage_file.parents)):
                return identity
            if stage_file.stat().st_size > MAX_EVIDENCE_JSON_BYTES or digest(stage_file) != expected:
                identity["config_status"] = "invalid"
                return identity
        if invocation.get("candidate") not in (None, folder.name):
            identity["config_status"] = "invalid"
            return identity
        if config.get("candidate") not in (None, folder.name):
            identity["config_status"] = "invalid"
            return identity
        for field in ("model_id", "endpoint_id", "tool_version"):
            if field in config and config[field] != invocation.get(field):
                identity["config_status"] = "invalid"
                return identity
        if folder.name in ("pydantic-ai", "langgraph"):
            dependencies = folder / "pip-freeze.log"
            if (not dependencies.is_file() or dependencies.is_symlink()
                    or any(parent.is_symlink() for parent in dependencies.parents)
                    or dependencies.stat().st_size > MAX_EVIDENCE_JSON_BYTES):
                return identity
            identity["dependencies_sha256"] = digest(dependencies)
        stable = {key: value for key, value in config.items() if key != "stage_sha256"}
        encoded = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
        identity["stable_config_sha256"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
        identity["config_status"] = "verified"
        return identity
    return identity


def candidate_attempt(root: Path, name: str, outcome: str) -> Dict[str, object]:
    folder = root / name
    evaluation_path = folder / "evaluation/evaluation.json"
    evaluation = read_json(evaluation_path)
    stage_validation = read_json(folder / "stage-validation.json")
    stage_files = {"analysis": "analysis-run.json", "plan": "plan-run.json",
                   "code": "generation.json", "execute": "evaluation-run.json"}
    stages = {stage: timing(folder, filename) for stage, filename in stage_files.items()}
    stage_statuses = {stage: (read_json(folder / filename) or {}).get("status")
                      for stage, filename in stage_files.items()}
    invocation_path = folder / "invocation.json"
    if not invocation_path.is_file():
        invocation_path = folder / "generated/invocation.json"
    invocation = read_json(invocation_path)
    invocation_identity = stable_invocation_identity(folder, invocation)
    artifacts = [root / "summary.json", invocation_path]
    artifacts.extend(folder / filename for filename in stage_files.values())
    artifacts.extend([folder / "stage-validation.json", folder / "runner-error.json", evaluation_path])
    artifacts.extend(root / "checkpoints" / name / (stage + ".json") for stage in STAGES)
    refs = [evidence(root, path) for path in artifacts if path.is_file() and not path.is_symlink()]
    cases = evaluation.get("cases", {}) if evaluation else {}
    statuses = {case: cases.get(case, {}).get("status") if isinstance(cases.get(case), dict) else None
                for case in ("baseline", "mutant")}
    code = generated_test(root, folder, evaluation)
    issues = []
    if invocation_identity["config_status"] == "invalid":
        issues.append("invocation_config_hash_mismatch")
    if outcome == "discriminates":
        if not evaluation or evaluation.get("result") != "discriminates":
            issues.append("summary_success_without_matching_evaluation")
        if statuses != {"baseline": "passed", "mutant": "failed_assertion"}:
            issues.append("differential_cases_do_not_support_success")
        if not code:
            issues.append("generated_test_missing")
    if code and not code["hash_matches"]:
        issues.append("generated_test_hash_mismatch")
    verified_outcome = "invalid_evidence" if issues else outcome
    return {"candidate": name, "outcome": verified_outcome,
            "reported_outcome": outcome, "integrity_issues": issues, "stages_ms": stages,
            "stage_statuses": stage_statuses,
            "stage_validation": stage_validation,
            "invocation_identity": invocation_identity,
            "maven_phases_ms": phase_timings(evaluation),
            "generated_test": code,
            "coverage": evaluation.get("coverage", {"status": "unknown"}) if evaluation else {"status": "unknown"},
            "case_statuses": statuses, "evidence": refs}


def load_run(root: Path) -> Dict[str, object]:
    if root.is_symlink():
        raise ValueError(f"linked run root: {root}")
    root = root.resolve()
    summary = read_json(root / "summary.json")
    if not summary or not isinstance(summary.get("candidate_results"), dict):
        raise ValueError(f"run lacks candidate results: {root}")
    kind = summary.get("run_kind", "candidate_generation")
    if kind not in ("candidate_generation", "evaluation_replay"):
        raise ValueError(f"unknown run kind: {root}")
    if kind == "evaluation_replay":
        manifest = read_json(root / "replay-manifest.json")
        if (not manifest or manifest.get("record_type") != "flpath-5050.evaluation-replay.v1"
                or manifest.get("source_summary_sha256") != summary.get("source_run_summary_sha256")):
            raise ValueError(f"replay packet lacks source linkage: {root}")
    candidates = summary["candidate_results"]
    if not all(isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name)
               and isinstance(value, str) for name, value in candidates.items()):
        raise ValueError(f"invalid candidate results: {root}")
    identity = cohort_identity(summary)
    spec_path = root / "run-spec.json"
    if spec_path.is_symlink():
        raise ValueError(f"run spec is linked: {root}")
    if spec_path.is_file():
        if spec_path.stat().st_size > MAX_EVIDENCE_JSON_BYTES:
            raise ValueError(f"run spec exceeds size limit: {root}")
        spec_hash = digest(spec_path)
        if identity["run_spec_sha256"] not in (None, spec_hash):
            raise ValueError(f"run spec hash mismatch: {root}")
        identity["run_spec_sha256"] = spec_hash
    archive = archive_integrity(root)
    binding_summary = {**summary, "run_spec_sha256": identity["run_spec_sha256"]}
    attempts = []
    for name, outcome in sorted(candidates.items()):
        attempt = candidate_attempt(root, name, outcome)
        checkpoints = checkpoint_integrity(root, name, binding_summary, outcome)
        issues = attempt["integrity_issues"] + archive["issues"] + checkpoints["issues"]
        invocation = attempt["invocation_identity"]
        for field, expected in (("candidate", name), ("model_id", summary.get("model_id")),
                                ("endpoint_id", summary.get("endpoint_id")),
                                ("prompt_sha256", summary.get("objective_sha256"))):
            if (invocation.get("config_status") == "verified" and invocation.get(field) is not None
                    and invocation[field] != expected):
                issues.append("invocation_binding_mismatch:" + field)
        attempt["integrity_issues"] = issues
        attempt["integrity_status"] = (
            "invalid" if issues else
            "verified" if archive["status"] == "verified" and checkpoints["status"] == "verified"
            and attempt["invocation_identity"]["config_status"] == "verified" else
            "verified_partial" if archive["status"] == "verified"
            and checkpoints["status"] == "verified_partial" else "unverified")
        if issues:
            attempt["outcome"] = "invalid_evidence"
        attempts.append(attempt)
    return {"root": str(root), "kind": kind,
            "run_id": summary.get("run_id"),
            "candidate_order": summary.get("candidate_order"),
            "source_run_summary_sha256": summary.get("source_run_summary_sha256"),
            "identity": identity,
            "identity_hash": "sha256:" + hashlib.sha256(
                json.dumps(identity, sort_keys=True).encode()).hexdigest(),
            "evidence_scan": summary.get("evidence_scan"),
            "archive_integrity": archive,
            "candidates": attempts}


def reviewed_decisions(path: Optional[Path], names: List[str]) -> Dict[str, object]:
    if path is None:
        return {}
    review = read_json(path)
    if not review or review.get("schema") != "flpath-5050.decision-review.v1":
        raise ValueError("decision review needs versioned schema")
    for field in ("reviewer", "reviewer_role"):
        if not isinstance(review.get(field), str) or not review[field].strip():
            raise ValueError(f"decision review needs {field}")
    entries = review.get("candidates")
    if not isinstance(entries, dict) or set(entries) - set(names):
        raise ValueError("decision review contains unknown candidates")
    decision_ids = set()
    for name, entry in entries.items():
        if not isinstance(entry, dict) or entry.get("status") not in OUTCOMES:
            raise ValueError(f"invalid decision for {name}")
        for field in ("decision_id", "authorization_ref"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ValueError(f"decision for {name} needs {field}")
        if entry["decision_id"] in decision_ids:
            raise ValueError("decision IDs must be unique")
        decision_ids.add(entry["decision_id"])
        if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            raise ValueError(f"decision for {name} needs a reason")
        stages = entry.get("by_stage")
        if not isinstance(stages, dict) or set(stages) != set(STAGES):
            raise ValueError(f"decision for {name} needs all stage decisions")
        if any(value not in OUTCOMES for value in stages.values()):
            raise ValueError(f"invalid stage decision for {name}")
        status = entry["status"]
        if status == "use" and any(value != "use" for value in stages.values()):
            raise ValueError(f"use decision for {name} conflicts with stage decisions")
        if status == "use_with_fork":
            if not entry.get("required_changes"):
                raise ValueError(f"fork decision for {name} needs required changes")
            if "use_with_fork" not in stages.values() or any(
                    value in ("reject", "insufficient_evidence") for value in stages.values()):
                raise ValueError(f"fork decision for {name} conflicts with stage decisions")
        if status == "reject" and "reject" not in stages.values():
            raise ValueError(f"reject decision for {name} needs a rejected stage")
        if status == "insufficient_evidence" and "insufficient_evidence" not in stages.values():
            raise ValueError(f"insufficient decision for {name} needs an unresolved stage")
    return {"reviewer": review["reviewer"], "reviewer_role": review["reviewer_role"],
            "source_sha256": digest(path),
            "candidates": entries}


def candidate_cohorts(runs: List[Dict[str, object]], name: str) -> List[Dict[str, object]]:
    """Summarize attempts per run identity and ordered candidate configuration."""
    groups: Dict[str, List[Tuple[Dict[str, object], Dict[str, object]]]] = {}
    run_id_counts = Counter(run.get("run_id") for run in runs
                            if run["kind"] == "candidate_generation" and run.get("run_id"))
    for run in runs:
        if run["kind"] != "candidate_generation":
            continue
        for attempt in run["candidates"]:
            if attempt["candidate"] != name:
                continue
            key = json.dumps({"run": run["identity_hash"],
                              "candidate_order": run["candidate_order"]}, sort_keys=True)
            groups.setdefault(key, []).append((run, attempt))
    result = []
    for entries in groups.values():
        config_groups: Dict[str, List[Tuple[Dict[str, object], Dict[str, object]]]] = {}
        unknown = 0
        for run, attempt in entries:
            identity = attempt["invocation_identity"]
            if (identity.get("config_status") != "verified"
                    or not all(identity.get(key) for key in
                               ("stable_config_sha256", "candidate", "model_id", "endpoint_id", "prompt_sha256",
                                "tool_source_revision", "tool_version"))):
                unknown += 1
                continue
            config_groups.setdefault(json.dumps(identity, sort_keys=True), []).append((run, attempt))
        configs = []
        required_identity = ("fixture_profile", "fixture_tree_sha256", "objective_sha256",
                             "model_id", "endpoint_id", "runner_sha256", "evaluator_sha256",
                             "run_spec_sha256")
        known_cohort = (all(entries[0][0]["identity"].get(field) for field in required_identity)
                        and isinstance(entries[0][0]["candidate_order"], list)
                        and bool(entries[0][0]["candidate_order"]))
        for identity, attempts in sorted(config_groups.items()):
            run_ids = [run["run_id"] for run, _ in attempts]
            independent = (bool(run_ids) and all(run_ids) and len(set(run_ids)) == len(run_ids)
                           and all(run_id_counts[run_id] == 1 for run_id in run_ids))
            verified = all(attempt.get("integrity_status") == "verified" for _, attempt in attempts)
            durations = [sum(attempt["stages_ms"].values()) for _, attempt in attempts
                         if all(isinstance(v, int) and v >= 0 for v in attempt["stages_ms"].values())
                         and all(attempt["stage_statuses"].get(stage) == "completed" for stage in STAGES)]
            configs.append({"invocation_identity": json.loads(identity),
                            "attempts": len(attempts),
                            "discriminating_attempts": sum(attempt["outcome"] == "discriminates"
                                                           for _, attempt in attempts),
                            "independent_run_ids": independent, "integrity_verified": verified,
                            "configuration_complete": bool(known_cohort),
                            "complete_wall_ms_median": median(durations)
                            if len(durations) >= 2 and independent and verified and known_cohort else None})
        run_ids = [run["run_id"] for run, _ in entries]
        result.append({"identity_hash": entries[0][0]["identity_hash"],
                       "identity": entries[0][0]["identity"],
                       "candidate_order": entries[0][0]["candidate_order"],
                       "attempts": len(entries),
                       "discriminating_attempts": sum(attempt["outcome"] == "discriminates"
                                                      for _, attempt in entries),
                       "independent_run_ids": bool(run_ids) and all(run_ids)
                       and len(set(run_ids)) == len(run_ids)
                       and all(run_id_counts[run_id] == 1 for run_id in run_ids),
                       "unknown_configuration_attempts": unknown,
                       "configuration_groups": configs})
    return result


def findings(runs: List[Dict[str, object]],
             review: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    cohorts = {run["identity_hash"] for run in runs}
    candidate_names = sorted({attempt["candidate"] for run in runs
                              for attempt in run["candidates"]})
    decisions = {}
    for name in candidate_names:
        attempts = [attempt for run in runs if run["kind"] == "candidate_generation"
                    for attempt in run["candidates"] if attempt["candidate"] == name]
        replays = [attempt for run in runs if run["kind"] == "evaluation_replay"
                   for attempt in run["candidates"] if attempt["candidate"] == name]
        grouped = candidate_cohorts(runs, name)
        run_ids = [run["run_id"] for run in runs if run["kind"] == "candidate_generation"
                   and any(a["candidate"] == name for a in run["candidates"])]
        independent = len(run_ids) == len(attempts) and bool(run_ids) and all(run_ids) and len(set(run_ids)) == len(run_ids)
        orders = [tuple(run["candidate_order"]) if isinstance(run["candidate_order"], list) else None
                  for run in runs if run["kind"] == "candidate_generation"
                  and any(a["candidate"] == name for a in run["candidates"])]
        same_order = bool(orders) and all(orders) and len(set(orders)) == 1
        tasks = {(run["identity"]["fixture_tree_sha256"], run["identity"]["objective_sha256"])
                 for run in runs if run["kind"] == "candidate_generation"
                 for attempt in run["candidates"] if attempt["candidate"] == name}
        config_results = [item for group in grouped for item in group["configuration_groups"]]
        repeated = any(group["independent_run_ids"] and config["independent_run_ids"]
                       and config["integrity_verified"] and config["configuration_complete"]
                       and config["attempts"] >= 3
                       for group in grouped for config in group["configuration_groups"])
        reasons = []
        if len(tasks) < 2:
            reasons.append("one behavior")
        if not repeated:
            reasons.append("no fixed-configuration repeated trials")
        reasons.append("semantic and production constraints remain unreviewed")
        decisions[name] = {"status": "insufficient_evidence",
                           "decision_origin": "automated_pending",
                           "by_stage": {stage: "insufficient_evidence" for stage in STAGES},
                           "reason": "; ".join(reasons) + ".",
                           "task_count": len(tasks), "attempts": len(attempts),
                           "evaluation_replays": len(replays),
                           "comparable_identity_count": len(grouped) + sum(
                               max(0, len(group["configuration_groups"]) - 1) for group in grouped),
                           "independent_run_ids": bool(independent),
                           "same_candidate_order": bool(same_order),
                           "discriminating_attempts": sum(a["outcome"] == "discriminates" for a in attempts),
                           "complete_wall_ms_median": (config_results[0]["complete_wall_ms_median"]
                               if len(grouped) == 1 and len(config_results) == 1
                               and not grouped[0]["unknown_configuration_attempts"] else None),
                           "cohorts": grouped}
    if review:
        for name, entry in review["candidates"].items():
            related = [(run, attempt) for run in runs for attempt in run["candidates"]
                       if attempt["candidate"] == name]
            if entry["status"] in ("use", "use_with_fork"):
                if not any(run["kind"] == "candidate_generation"
                           and attempt["outcome"] == "discriminates"
                           for run, attempt in related):
                    raise ValueError(f"positive decision for {name} needs a discriminating generation attempt")
                if not any(run["kind"] == "candidate_generation"
                           and attempt["outcome"] == "discriminates"
                           and attempt.get("integrity_status") == "verified"
                           for run, attempt in related):
                    raise ValueError(f"positive decision for {name} needs verified evidence")
                if any(run["evidence_scan"] != "passed" or attempt["integrity_issues"]
                       or attempt.get("integrity_status") not in ("verified", "verified_partial")
                       for run, attempt in related):
                    raise ValueError(f"positive decision for {name} needs verified evidence")
            decisions[name].update({
                "status": entry["status"], "decision_origin": "reviewer",
                "by_stage": entry["by_stage"],
                "reason": entry["reason"],
                "required_changes": entry.get("required_changes", []),
                "reviewer": review["reviewer"],
                "reviewer_role": review["reviewer_role"],
                "decision_id": entry["decision_id"],
                "authorization_ref": entry["authorization_ref"],
                "review_source_sha256": review["source_sha256"],
            })
    return {"schema": SCHEMA, "cohort_count": len(cohorts), "runs": runs,
            "decisions": decisions,
            "limitations": ["Archive and checkpoint hashes verify internal consistency, not authenticity.",
                            "Different identity hashes are not repeated trials.",
                            "Generated lines and coverage describe output, not test quality.",
                            "Use/fork/reject requires a reviewed rubric and production constraints."]}


def coverage_text(value: object) -> str:
    if not isinstance(value, dict) or value.get("status") != "measured":
        return value.get("status", "unknown") if isinstance(value, dict) else "unknown"
    measurements = value.get("measurements", {})
    before = measurements.get("existing_suite", {})
    after = measurements.get("existing_plus_candidate", {})
    parts = []
    for name in ("line", "branch"):
        old = before.get(name, {})
        new = after.get(name, {})
        if not isinstance(old, dict) or not isinstance(new, dict):
            return "coverage_unknown"
        old_total = old.get("covered", 0) + old.get("missed", 0)
        new_total = new.get("covered", 0) + new.get("missed", 0)
        parts.append(f"{name} {old.get('covered', 0)}/{old_total} -> "
                     f"{new.get('covered', 0)}/{new_total}")
    return ", ".join(parts)


def markdown(report: Dict[str, object]) -> str:
    lines = ["# FLPATH-5050 candidate findings", "",
             f"Runs: {len(report['runs'])}; comparable provenance cohorts: {report['cohort_count']}.",
             "", "## Candidate findings", ""]
    if any(run["kind"] == "evaluation_replay" for run in report["runs"]):
        lines.extend(["Evaluation replay packets reuse saved generated tests and their original stage timings.",
                      "Their Maven evidence was collected later with a named evaluator revision.", ""])
    for name, decision in report["decisions"].items():
        attempts = [(run["kind"], a) for run in report["runs"]
                    for a in run["candidates"] if a["candidate"] == name]
        lines.extend([f"### {name}", "",
                      f"{'Reviewer decision' if decision['decision_origin'] == 'reviewer' else 'Decision'}: "
                      f"**{decision['status']}**. {decision['reason']}",
                      f"Discriminating: {decision['discriminating_attempts']}/{decision['attempts']} generation attempt(s); "
                      f"evaluation replays: {decision['evaluation_replays']}.", ""])
        for cohort in decision["cohorts"]:
            fixture = cohort["identity"].get("fixture_profile") or "unknown fixture"
            lines.append(f"- Cohort {fixture}: {cohort['discriminating_attempts']}/{cohort['attempts']} "
                         f"discriminating; {cohort['unknown_configuration_attempts']} configuration(s) unknown.")
            for group in cohort["configuration_groups"]:
                value = group["complete_wall_ms_median"]
                rendered = f"{value / 1000:.1f}s" if value is not None else "unavailable"
                lines.append(f"  Fixed-config attempts: {group['attempts']}; median complete time: {rendered}; "
                             f"integrity: {'verified' if group['integrity_verified'] else 'unverified'}.")
        if decision["cohorts"]:
            lines.append("")
        for kind, attempt in attempts:
            times = ", ".join(
                f"{stage}={attempt['stage_statuses'].get(stage) or 'unknown'} ({ms / 1000:.1f}s)"
                for stage, ms in attempt["stages_ms"].items() if ms is not None)
            code = attempt["generated_test"]
            size = (f"{code['physical_lines']} lines, {code['bytes']} bytes"
                    if code else "no saved generated test")
            coverage = attempt["coverage"]
            coverage_status = coverage_text(coverage)
            phases = ", ".join(f"{phase}={ms / 1000:.1f}s"
                               for phase, ms in attempt["maven_phases_ms"].items()
                               if isinstance(ms, (int, float)))
            direct = next((item for item in attempt["evidence"]
                           if item["path"].endswith("evaluation/evaluation.json")),
                          next((item for item in attempt["evidence"]
                                if item["path"].endswith("generation.json")),
                               attempt["evidence"][0]))
            validation = attempt.get("stage_validation") or {}
            semantic = validation.get("semantic_review", "unrecorded")
            lines.extend([f"- {kind}: {attempt['outcome']}; {times}; {size}; coverage: {coverage_status}.",
                          f"  Maven phases: {phases or 'unmeasured'}; semantic review: {semantic}; "
                          f"integrity: {attempt.get('integrity_status', 'unverified')}."])
            if attempt["integrity_issues"]:
                lines.append(f"  Evidence integrity: {', '.join(attempt['integrity_issues'])}.")
            lines.append(f"  Evidence: {direct['path']} ({direct['sha256']}).")
        lines.append("")
    lines.extend(["## Interpretation", "",
                  "Line coverage alone cannot prove that a test detects the target defect.",
                  "Compare branch coverage and baseline-pass/mutant-assertion-fail evidence.",
                  "Treat setup failures and timeouts separately from test failures."])
    if all(value["status"] == "insufficient_evidence" for value in report["decisions"].values()):
        lines.append("Decisions remain insufficient until repeated runs and human stage review are complete.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", action="append", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--decision-review", type=Path)
    args = parser.parse_args()
    if len({root.resolve() for root in args.run_root}) != len(args.run_root):
        parser.error("run roots must be unique")
    if args.output_dir.exists():
        parser.error("output directory must be new")
    runs = [load_run(root) for root in args.run_root]
    names = sorted({a["candidate"] for run in runs for a in run["candidates"]})
    review = reviewed_decisions(args.decision_review, names)
    report = findings(runs, review)
    args.output_dir.mkdir(parents=True)
    (args.output_dir / "findings.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (args.output_dir / "findings.md").write_text(markdown(report))
    print(args.output_dir / "findings.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
