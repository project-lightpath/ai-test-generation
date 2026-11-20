"""Derive method coverage from archived, hash-verified JaCoCo reports.

This is a read-only interpretation of saved evidence. It does not replace the
original evaluation record or imply that coverage proves test quality.
"""

import argparse
import hashlib
import json
import re
import stat
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Optional, Tuple

SCHEMA = "flpath-5050.coverage-derivation.v1"
PACKAGE = "com/redhat/lightwell/service"
CLASS = f"{PACKAGE}/StatementImportService"
METHOD = "parseAndCreateTransactions"
CASES = {"existing_suite": "coverage-control",
         "existing_plus_candidate": "coverage-candidate"}
MAX_BYTES = 16 * 1024 * 1024
JACOCO_DOCTYPE = b'<!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd">'


def read_regular(root: Path, relative: Path) -> Optional[bytes]:
    current = root
    for part in relative.parts:
        if part in ("", ".", ".."):
            return None
        current = current / part
        if not current.exists() and not current.is_symlink():
            return None
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            return None
        if current != root / relative and not stat.S_ISDIR(mode):
            return None
    if not stat.S_ISREG(current.lstat().st_mode) or current.stat().st_size > MAX_BYTES:
        return None
    return current.read_bytes()


def sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def method_counters(content: bytes) -> Tuple[Optional[Dict[str, object]], str]:
    remaining = content.replace(JACOCO_DOCTYPE, b"", 1)
    if b"<!DOCTYPE" in remaining or b"<!ENTITY" in content:
        return None, "unsupported_xml_declaration"
    try:
        report = ET.fromstring(content)
    except ET.ParseError:
        return None, "invalid_xml"
    if report.tag != "report":
        return None, "invalid_jacoco_report"
    matches = [method for package in report.findall("package") if package.get("name") == PACKAGE
               for klass in package.findall("class") if klass.get("name") == CLASS
               for method in klass.findall("method") if method.get("name") == METHOD]
    if len(matches) != 1:
        return None, "method_missing_or_ambiguous"
    values: Dict[str, Dict[str, int]] = {}
    for counter in matches[0].findall("counter"):
        kind = counter.get("type")
        if kind not in ("LINE", "BRANCH"):
            continue
        if kind in values:
            return None, "duplicate_counter"
        try:
            missed = int(counter.get("missed", ""))
            covered = int(counter.get("covered", ""))
        except ValueError:
            return None, "invalid_counter"
        if missed < 0 or covered < 0:
            return None, "invalid_counter"
        values[kind] = {"missed": missed, "covered": covered}
    if set(values) != {"LINE", "BRANCH"}:
        return None, "missing_counter"
    return {"descriptor": matches[0].get("desc"),
            "line": values["LINE"], "branch": values["BRANCH"]}, "measured"


def case_coverage(root: Path, files: Dict[str, object], name: str, case: str) -> Dict[str, object]:
    relative = Path(name, "evaluation", case, "target", "site", "jacoco", "jacoco.xml")
    key = relative.as_posix()
    entry = files.get(key)
    if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str):
        return {"status": "unknown", "reason": "xml_not_archived", "path": key}
    content = read_regular(root, relative)
    if content is None:
        return {"status": "unknown", "reason": "xml_missing_linked_or_oversized", "path": key}
    actual = sha256(content)
    if actual != entry["sha256"] or len(content) != entry.get("bytes"):
        return {"status": "unknown", "reason": "archive_hash_mismatch", "path": key}
    counters, status = method_counters(content)
    result: Dict[str, object] = {"status": "measured" if counters else "unknown",
                                 "path": key, "sha256": actual}
    if counters is None:
        result["reason"] = status
    else:
        result.update(counters)
    return result


def derive(run_root: Path) -> Dict[str, object]:
    if run_root.is_symlink() or not run_root.is_dir():
        raise ValueError("run root must be a real directory")
    root = run_root.resolve()
    summary_bytes = read_regular(root, Path("summary.json"))
    manifest_bytes = read_regular(root, Path("archive-manifest.json"))
    if summary_bytes is None or manifest_bytes is None:
        raise ValueError("missing or linked archive summary/manifest")
    summary = json.loads(summary_bytes)
    manifest = json.loads(manifest_bytes)
    if not isinstance(summary, dict) or summary.get("evidence_scan") != "passed":
        raise ValueError("source run lacks passed evidence scan")
    if not isinstance(manifest, dict) or manifest.get("schema") != "flpath-5050.evidence-archive.v1":
        raise ValueError("unsupported archive manifest")
    files = manifest.get("files")
    summary_entry = files.get("summary.json") if isinstance(files, dict) else None
    if not isinstance(summary_entry, dict) or summary_entry.get("sha256") != sha256(summary_bytes):
        raise ValueError("archive summary hash mismatch")
    candidates = summary.get("candidate_results")
    if not isinstance(candidates, dict) or not candidates:
        raise ValueError("source run lacks candidates")
    results = {}
    for name in sorted(candidates):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            raise ValueError("invalid candidate name")
        cases = {label: case_coverage(root, files, name, case)
                 for label, case in CASES.items()}
        results[name] = {"status": "measured" if all(
            value["status"] == "measured" for value in cases.values()) else "unknown",
                         "cases": cases}
    return {"schema": SCHEMA, "run_id": summary.get("run_id"),
            "source": {"summary_sha256": sha256(summary_bytes),
                       "archive_manifest_sha256": sha256(manifest_bytes),
                       "evidence_scan": "passed"},
            "target": {"class": CLASS, "method": METHOD},
            "candidates": results,
            "limitations": ["Derived from saved JaCoCo XML; original evaluation is unchanged.",
                            "Coverage measures execution, not whether a test detects the defect."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        parser.error("output must be a new file")
    report = derive(args.run_root)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
