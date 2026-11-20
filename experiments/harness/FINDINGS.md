# FLPATH-5050 candidate findings

Use the saved run packets to compare candidate tools. The report reads evidence; it does not call a model or run Maven.

## Tasks and measures

Run the same candidate set, model, time limits, and source revision for each task. The tasks test different behavior gaps:

- `spring-transfer`: an exact-balance transfer in the pinned Spring app. Its 121 existing tests miss the defect. The original suite covers 31 of 31 lines and 5 of 6 branches in `TransactionService.transfer`.
- `quarkus-staged`: an empty user on the pinned Java 21 Quarkus transfer endpoint. The existing happy-path test misses the defect. This profile exercises Analyze, Plan, Code, and Execute. The evaluator has no JaCoCo method contract for this fixture, so report coverage as `not_applicable`, not zero.
- `spring-xml-deposit`: a lower-case deposit type in the same pinned Spring app. The 121 existing tests pass even with the seeded defect. A separate reference test passes on original code and fails by assertion on the defect. JaCoCo compares `StatementImportService.parseAndCreateTransactions` with and without each candidate test.

For each candidate, inspect:

1. **Analyze:** Did it identify the relevant code, existing test, missing behavior, and feasible test layer?
2. **Plan:** Did it propose a native test with setup and assertions that address the gap?
3. **Code:** Review the saved Java test for the expected behavior, meaningful assertions, maintainability, duplication, and forbidden edits. Compare bytes and lines as size measures, not quality scores.
4. **Execute:** Require the generated test to pass on original code and fail **by assertion** on the seeded defect. A build failure, setup failure, timeout, or uncovered behavior is not a pass.
5. **Coverage and time:** Compare JaCoCo method line/branch counters where available, and Analyze/Plan/Code/Execute wall time plus Maven phase time. More coverage or less code alone does not prove better defect detection.

The task objectives already state the behavior and required assertions.
These runs assess test generation from that detailed objective; they do not
establish that a candidate independently discovers coverage gaps or chooses
the behavior to test. They do not prove a CVE fix or production readiness.

## Pin and check a run

[run-spec.quarkus-staged.json](run-spec.quarkus-staged.json) and [run-spec.spring-xml-deposit.json](run-spec.spring-xml-deposit.json) select the two staged tasks and define `flpath-5050.run-spec.v1`: profile, ordered candidates, one model and endpoint, stage time limits, and optional external adapters. Use the same reviewed spec for each comparable repeat. Pass it with `--run-spec`; do not combine that option with `--candidate`. The runner saves its hash, run ID, candidate order, stage checkpoints, tool versions, runner hash, and evaluator hash.

Before a live call, run `run_comparison.py --check-only` with the same fixture, Maven cache, runtime paths, output path, and run specification that the live call will use. The key-free preflight must complete successfully. The preflight runs the pinned fixture's Maven `verify` with network disabled and the chosen cache. Use separate warmed Spring and Quarkus caches, and verify the selected fixture against its own cache; one cache may not contain the other fixture's dependencies.

Keep each run on a bounded temporary volume. Stream the model key through standard input as described in [RERUN.md](RERUN.md); do not put it in a command argument or repository file. Start every attempt with a new output directory. Treat an aborted or missing-summary run as a setup-invalid pilot, not a candidate failure or a comparable repeat. Record the cause separately and rerun only after the offline build check passes.

## Archive and report

Wait until the run has finished. Confirm `summary.json` says `"evidence_scan": "passed"`. The exporter enforces this gate and refuses an incomplete run. On the experiment host, use the variables prepared in the runbook and choose a new archive name:

```sh
: "${ATG_ROOT:?Set ATG_ROOT to the repository checkout}"
: "${WORK_ROOT:?Set WORK_ROOT to the experiment workspace}"
: "${RUN_OUTPUT:?Set RUN_OUTPUT to a completed run}"
ARCHIVE_ROOT="$WORK_ROOT/evidence"
ARCHIVE="$ARCHIVE_ROOT/run-001"
mkdir -p "$ARCHIVE_ROOT"
python3 "$ATG_ROOT/experiments/harness/archive_evidence.py" \
  --run-root "$RUN_OUTPUT" \
  --output-dir "$ARCHIVE"
```

Use a **new** archive directory for each attempt. The exporter copies selected JSON, logs, checkpoints, the evaluated Java test, and JaCoCo XML; it excludes bulk Maven workspaces. `archive-manifest.json` lists file sizes and SHA-256 hashes. Review the archive before sharing it. Do not archive a run whose scan is blocked or absent.

Build one report from any number of completed archives. The following example assumes the two named archives already exist; replace their names with your completed archives:

```sh
python3 "$ATG_ROOT/experiments/harness/build_findings.py" \
  --run-root "$ARCHIVE_ROOT/quarkus-staged-run-001" \
  --run-root "$ARCHIVE_ROOT/spring-xml-deposit-run-001" \
  --output-dir "$WORK_ROOT/findings-multitask-001"
```

The new report directory contains `findings.json` for tools and
`findings.md` for reviewers. Pass `--run-root` for every completed attempt,
including failures and timeouts. Candidate-generation attempts are grouped by
task, fixture, objective, model, endpoint, runner, evaluator, run specification,
candidate order, and stable candidate configuration. Generated Analyze and Plan
hashes are retained as provenance but do not make otherwise identical inputs
different configurations. Evaluation replays reuse tests and are not generation
attempts.

The report keeps incomplete attempts in the task cohort's attempt count. An
attempt without a verifiable configuration is counted separately as unknown,
not treated as a comparable fixed-configuration repeat. Median total stage
time appears for a configuration only when at least two independent, verified
attempts completed every stage. Do not average across tasks or changed
configurations. Historical Spring runs changed runner or prompt settings, so
they are separate cohorts. Rebuilding the report cannot invent missing JaCoCo
or Maven measurements.

### Evidence consistency

The report checks the archive manifest, retained file hashes, checkpoint
links, recorded outputs, and candidate configuration. It records:

- `verified`: complete retained evidence passes these consistency checks.
- `verified_partial`: a retained blocked-stage prefix passes the available
  checks. The failure remains in the report.
- `unverified`: the packet lacks enough records to verify consistency,
  including older packets and incomplete evidence.
- `invalid`: retained records fail a consistency check.

A missing manifest or checkpoint is unverified, not proof of failure. Hashes
and checkpoints are unsigned: these checks show internal consistency, not
authenticity against someone able to replace the evidence and its hashes.
Review source identity and authorization separately.

## Future candidates

To add a candidate, build and review an approved container with executable `/adapter`. Pin its image reference and local image ID in `external_candidates`. For example, this is only the relevant fragment, with placeholder digests:

```json
{
  "candidates": ["aider", "new-agent"],
  "external_candidates": [{
    "name": "new-agent",
    "version": "1.2.3",
    "image": "quay.io/example/new-agent@sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "image_id": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
  }]
}
```

The adapter receives the same objective and source context. It must follow
the Analyze, Plan, and Code output contract and identify the generated test
by path and hash. Later stages receive earlier artifacts read-only; the runner
checks completed-stage outputs and hash-linked checkpoints, including after a
blocked stage. Checkpoints preserve evidence; they do not resume a run.

A comparable completed attempt needs a retained sanitized configuration that
matches its invocation hash and binds the Analyze and Plan output hashes in
`stage_sha256`. An external adapter without these records remains
unverified or non-comparable; do not infer a matching configuration.

The [fake external adapter](fixtures/fake_external_adapter/) was exercised without a model using a fake key. One setup failure and one successful contract run were retained outside the repository on the original experiment host. The successful fake test passed on original Spring code and failed by assertion on the defect. This validates the extension contract, not a real candidate or a secure adapter image. A real external container receives the model key and network access; review its image before use. An image digest identifies the bits but does not establish trust.

## Record a review decision

Without a review file, the report marks each candidate and stage `insufficient_evidence`. A reviewer can add `use`, `use_with_fork`, `reject`, or `insufficient_evidence` in a separate JSON file. Do not edit raw run packets. For example:

```json
{
  "schema": "flpath-5050.decision-review.v1",
  "reviewer": "example-reviewer",
  "reviewer_role": "quality_engineer",
  "candidates": {
    "aider": {
      "decision_id": "FLPATH-5050-aider-review-001",
      "authorization_ref": "FLPATH-5050",
      "status": "insufficient_evidence",
      "reason": "Repeat with the fixed configuration and review the results.",
      "by_stage": {
        "analysis": "insufficient_evidence",
        "plan": "insufficient_evidence",
        "code": "insufficient_evidence",
        "execute": "insufficient_evidence"
      }
    }
  }
}
```

Every included candidate needs a unique decision ID, an authorization
reference, a reason, and all four stage decisions. The reviewer needs an identity
and role. `use_with_fork` also requires a nonempty `required_changes` list.

A positive decision requires at least one discriminating generation with
`verified` archive, checkpoint, and configuration records. All related
attempts must have a passed evidence scan, no integrity issues, and either
`verified` or `verified_partial` consistency status. A consistent blocked
prefix may therefore remain in the evidence; an unverified legacy or incomplete
packet cannot support a positive decision. The report records the review file
hash. Human review must still assess the test and production requirements.

```sh
: "${REVIEW_FILE:?Set REVIEW_FILE to your reviewed decision JSON}"
python3 "$ATG_ROOT/experiments/harness/build_findings.py" \
  --run-root "$ARCHIVE" \
  --decision-review "$REVIEW_FILE" \
  --output-dir "$WORK_ROOT/findings-reviewed-001"
```

Evaluate candidate use by stage and overall: semantic correctness, repeatability across behaviors, generated-code maintenance, licensing and fork rights, model endpoint support, isolation, and measured time. `Execute` is the shared evaluator today, not a fourth generation agent. A single timeout or success does not settle production suitability. A dirty checkout also needs a reviewed source snapshot or commit before another person can reproduce it.
