# FLPATH-5050: repeat the Spring Boot comparison

This guide runs all four candidates against the pinned Spring Boot
lowercase-deposit task using the `spring-xml-deposit` profile. It does not
deploy the application or run the SSC demo pipeline. For the Quarkus task,
use [RERUN.md](RERUN.md).

Run on a Linux experiment host you choose. The current runner requires root
for Aider and the example mount below. It also requires Podman, Git, Python
3.11, npm, network access for setup and model generation, a private EnMaaS
credential, and a dedicated output mount of at most 8 GiB. Tests execute
offline with a read-only Maven cache.

The runner pins image IDs, candidate versions, the EnMaaS endpoint, and model
`rits/zai-org/glm-5-3`. Image tags may change; stop if preflight rejects an
ID. Changing pinned identities requires a reviewed configuration and a new
comparison cohort.

## 1. Prepare the pinned app and paths

Start in a reviewed checkout of this repository on the experiment host.
`WORK_ROOT` can be set to any writable location outside the checkout before
running these commands:

```sh
set -eu
ATG_ROOT="$(git rev-parse --show-toplevel)"
WORK_ROOT="${WORK_ROOT:-$HOME/flpath-5050-work}"
APP_ROOT="$WORK_ROOT/spring-boot-example-application"
RUNTIME_ROOT="$WORK_ROOT/runtimes"
MAVEN_CACHE="$WORK_ROOT/maven-cache-spring/repository"
RUN_VOLUME="$WORK_ROOT/quota-output"
RUN_OUTPUT="$RUN_VOLUME/spring-xml-deposit-run-001"
ARCHIVE_ROOT="$WORK_ROOT/evidence"
RUN_SPEC="$ATG_ROOT/experiments/harness/run-spec.spring-xml-deposit.json"
mkdir -p "$WORK_ROOT" "$RUNTIME_ROOT" "$MAVEN_CACHE" "$ARCHIVE_ROOT"
git clone https://github.com/lzspangler/spring-boot-example-application.git "$APP_ROOT"
git -C "$APP_ROOT" checkout --detach 01a18a1b40e151ffcc1d7bef86bf335f87999750
git -C "$ATG_ROOT" rev-parse HEAD
git -C "$APP_ROOT" rev-parse HEAD
```

If the app checkout already exists, verify its revision and clean source tree
instead of cloning again. The app uses Java 17 and Spring Boot 2.7.18. Record
the ATG revision and dirty state; preserve a source snapshot for any dirty
checkout. Preflight checks the fixture tree hash and objective hash.

## 2. Prepare images, runtimes, and the offline cache

```sh
podman pull docker.io/library/python:3.11-slim
podman pull docker.io/library/maven:3.9.11-eclipse-temurin-17
podman pull quay.io/sscdemo/ai-agent-maven-aider:v1.0.1
podman run --rm --pull=never --network=host \
  -v "$ATG_ROOT:/repo:ro,Z" \
  -v "$RUNTIME_ROOT:/runtimes:Z" \
  docker.io/library/python:3.11-slim \
  sh -c 'python -m venv /runtimes/pydantic-venv &&
    /runtimes/pydantic-venv/bin/pip install -r /repo/experiments/harness/candidates/pydantic_ai/requirements.txt &&
    python -m venv /runtimes/langgraph-venv &&
    /runtimes/langgraph-venv/bin/pip install -r /repo/experiments/harness/candidates/langgraph/requirements.txt'
npm install --prefix "$RUNTIME_ROOT/claude" --no-audit --no-fund \
  node@22.23.5 @anthropic-ai/claude-code@2.1.296
```

Use a **separate Spring cache**; the Quarkus and Spring dependencies differ.
Warm the pinned app online. The runner later verifies the app offline with
the cache read-only, before it reads the model key:

```sh
podman run --rm --pull=never --network=host \
  -v "$APP_ROOT:/workspace:Z" \
  -v "$MAVEN_CACHE:/root/.m2/repository:Z" \
  -w /workspace docker.io/library/maven:3.9.11-eclipse-temurin-17 \
  mvn -B -ntp verify
```

The original experiment's baseline had 121 passing tests. Treat your own
offline preflight result as the evidence for your setup; do not assume a
historical result proves the current cache works.

## 3. Set up bounded output and run preflight

Mount a dedicated 4 GiB temporary filesystem on the experiment host. Obtain
any authorization your environment requires. The parent of `RUN_OUTPUT`
must be a mount no larger than 8 GiB:

```sh
free -h
mkdir -p "$RUN_VOLUME"
mount -t tmpfs -o size=4G,mode=0700 tmpfs "$RUN_VOLUME"
df -h "$RUN_VOLUME"
python3 "$ATG_ROOT/experiments/harness/run_comparison.py" \
  --run-spec "$RUN_SPEC" \
  --fixture-root "$APP_ROOT" \
  --maven-cache "$MAVEN_CACHE" \
  --output-root "$RUN_OUTPUT" \
  --pydantic-venv "$RUNTIME_ROOT/pydantic-venv" \
  --langgraph-venv "$RUNTIME_ROOT/langgraph-venv" \
  --claude-runtime-dir "$RUNTIME_ROOT/claude/node_modules" \
  --check-only
```

The key-free preflight checks the spec, pinned content, image IDs, runtimes,
and unused output path. It also runs the app's existing tests with offline Maven
and the cache read-only. Resolve any failure before a model call.

The versioned spec sets `spring-xml-deposit`, the same four candidates, and
600/600/600/3900-second Analyze/Plan/Code/Execute limits. Keep the spec,
candidate order, model, runner, evaluator, source and runtime identities fixed
for comparable repeats. Keep each completed failure and timeout.

## 4. Run all candidates

Set `ENMAAS_KEY_FILE` in the host shell to your approved private credential
file. It is read through standard input; do not place the key in the repository
or a command argument. If it resides elsewhere, stream it securely to the
experiment host's standard input.

```sh
: "${ENMAAS_KEY_FILE:?Set ENMAAS_KEY_FILE to your private key file}"
set +e
python3 "$ATG_ROOT/experiments/harness/run_comparison.py" \
  --run-spec "$RUN_SPEC" \
  --fixture-root "$APP_ROOT" \
  --maven-cache "$MAVEN_CACHE" \
  --output-root "$RUN_OUTPUT" \
  --pydantic-venv "$RUNTIME_ROOT/pydantic-venv" \
  --langgraph-venv "$RUNTIME_ROOT/langgraph-venv" \
  --claude-runtime-dir "$RUNTIME_ROOT/claude/node_modules" \
  --api-key-stdin < "$ENMAAS_KEY_FILE"
RUN_EXIT=$?
set -e
printf 'Runner exit: %s\n' "$RUN_EXIT"
```

Analyze and Plan produce checkpoints before Code creates a native JUnit test.
The shared evaluator installs that test in two fresh copies: original code
must pass and a copy with the seeded case-sensitive deposit defect must fail
by assertion. A build failure or timeout is blocked or inconclusive, not a
passing test. A nonzero runner exit still needs evidence review.

For a repeat, choose a fresh `RUN_OUTPUT` directory beneath the same mounted
`RUN_VOLUME`. Never overwrite an attempt.

## 5. Archive and review

After completion, check `summary.json` contains
`"evidence_scan": "passed"`. The scan checks the exact key; it cannot detect
partial or transformed secrets. Review selected evidence before sharing it.
The exporter refuses missing or blocked scans and writes outside the mount:

```sh
ARCHIVE="$ARCHIVE_ROOT/spring-xml-deposit-run-001"
python3 "$ATG_ROOT/experiments/harness/archive_evidence.py" \
  --run-root "$RUN_OUTPUT" --output-dir "$ARCHIVE"
python3 "$ATG_ROOT/experiments/harness/build_findings.py" \
  --run-root "$ARCHIVE" --output-dir "$WORK_ROOT/findings-spring-001"
```

Use [FINDINGS.md](FINDINGS.md) to compare archives and record a reviewed
decision. Preserve all completed attempts before unmounting; tmpfs contents
are lost on unmount:

```sh
umount "$RUN_VOLUME"
```

The Spring evaluator measures JaCoCo line and branch coverage for
`StatementImportService.parseAndCreateTransactions` before and after adding
the generated test. More coverage does not guarantee a useful assertion. The
controlled defect does not prove a CVE fix.

For the separate exact-balance task, use the existing
[run-spec.example.json](run-spec.example.json), which selects
`spring-transfer`. Keep its results in a different cohort from the XML task.
