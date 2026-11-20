# FLPATH-5050: repeat the Quarkus comparison

This guide runs Aider, Pydantic AI, Claude Code, and LangGraph against the
same pinned Java 21/Quarkus blank-user task. It exercises Analyze → Plan →
Code → Execute. The application runs in containers on a Linux host you choose;
it does not require a particular hypervisor.

## Prerequisites

- A reviewed checkout of this repository, Git, Python 3.11, Podman, npm, and
  network access to obtain dependencies and call the configured model.
- Root privileges on the experiment host: the current Aider preflight checks
  for root, and the example temporary filesystem mount requires it. The runner
  does not require a particular hostname.
- The exact pinned images and versions listed below. The runner checks local
  image IDs, not just tags. A different ID or architecture requires a new
  reviewed configuration and a separate results cohort; do not bypass the
  identity check and call it the same experiment.
- An approved EnMaaS credential supplied through standard input. The runner
  currently pins the EnMaaS endpoint and `rits/zai-org/glm-5-3` model; it
  does not yet accept an arbitrary model endpoint in its run specification.
- A dedicated, size-limited output mount. The live runner requires the
  output directory's **parent** to be a mount of no more than 8 GiB. The
  generated test executes offline, but model-candidate containers need
  network access. Review any external adapter image before providing a key.

All commands below run on the **experiment host**. Start in the root of the
reviewed repository checkout. Set `WORK_ROOT` to a writable location outside
the checkout if the default is unsuitable. The run ID and outputs must be
new for every attempt.

## 1. Prepare the fixture and paths

```sh
set -eu
ATG_ROOT="$(git rev-parse --show-toplevel)"
WORK_ROOT="${WORK_ROOT:-$HOME/flpath-5050-work}"
FIXTURE_REPO="$WORK_ROOT/java-analysis-adapter"
FIXTURE_ROOT="$FIXTURE_REPO/spike/java21-quarkus"
RUNTIME_ROOT="$WORK_ROOT/runtimes"
MAVEN_CACHE="$WORK_ROOT/maven-cache-quarkus/repository"
RUN_VOLUME="$WORK_ROOT/quota-output"
RUN_OUTPUT="$RUN_VOLUME/quarkus-staged-run-001"
ARCHIVE_ROOT="$WORK_ROOT/evidence"
RUN_SPEC="$ATG_ROOT/experiments/harness/run-spec.quarkus-staged.json"
mkdir -p "$WORK_ROOT" "$RUNTIME_ROOT" "$MAVEN_CACHE" "$ARCHIVE_ROOT"
git clone https://github.com/project-lightpath/java-analysis-adapter.git "$FIXTURE_REPO"
git -C "$FIXTURE_REPO" checkout --detach 2aac181ae63d938c118bac133d33fb8ae19eeec9
git -C "$ATG_ROOT" rev-parse HEAD
git -C "$FIXTURE_REPO" rev-parse HEAD
```

For an existing fixture checkout, verify the revision and clean source tree
instead of cloning into an existing directory. Record the ATG revision and dirty
state; preserve a source snapshot if the checkout is dirty. The runner checks
the fixture tree hash as well as the pinned revision's expected contents.

## 2. Prepare images, runtimes, and Maven cache

```sh
podman pull docker.io/library/python:3.11-slim
podman pull docker.io/library/maven:3.9.11-eclipse-temurin-21
podman pull quay.io/sscdemo/ai-agent-maven-aider:v1.0.1
```

The expected image IDs are recorded in `run_comparison.py` and
`fixture_profiles.py`; the key-free preflight checks them. A tag can change,
so stop if the IDs differ. Prepare the pinned Python and Claude runtimes:

```sh
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

Warm a **separate Quarkus** cache online, including the test and package
lifecycles. The model run uses this cache read-only and cannot download missing
dependencies:

```sh
podman run --rm --pull=never --network=host \
  -v "$FIXTURE_ROOT:/workspace:Z" \
  -v "$MAVEN_CACHE:/root/.m2/repository:Z" \
  -w /workspace docker.io/library/maven:3.9.11-eclipse-temurin-21 \
  mvn -B -ntp -Ptest verify
```

## 3. Set up bounded output and run preflight

Mount a dedicated 4 GiB temporary filesystem at `RUN_VOLUME` on the
experiment host. Check available memory and obtain any authorization your
environment requires. Keep the mount until evidence is reviewed and archived.

```sh
free -h
mkdir -p "$RUN_VOLUME"
mount -t tmpfs -o size=4G,mode=0700 tmpfs "$RUN_VOLUME"
df -h "$RUN_VOLUME"
python3 "$ATG_ROOT/experiments/harness/run_comparison.py" \
  --run-spec "$RUN_SPEC" \
  --fixture-root "$FIXTURE_ROOT" \
  --maven-cache "$MAVEN_CACHE" \
  --output-root "$RUN_OUTPUT" \
  --pydantic-venv "$RUNTIME_ROOT/pydantic-venv" \
  --langgraph-venv "$RUNTIME_ROOT/langgraph-venv" \
  --claude-runtime-dir "$RUNTIME_ROOT/claude/node_modules" \
  --check-only
```

Preflight checks the spec, fixture and objective digests, image IDs, candidate
versions, runtime paths, and that the output does not exist. It also runs the
existing fixture tests with offline Maven and the cache read-only. It makes no
model call. Stop on any failure and correct the setup before starting a run.

The versioned Quarkus spec selects `quarkus-staged`, all four candidates, and
600/600/600/3900-second Analyze/Plan/Code/Execute limits. Use the same spec,
runner, evaluator, fixture, runtime and candidate order for comparable repeats.
The runner records hashes and identities, but model output can vary.

## 4. Run the comparison

Set `ENMAAS_KEY_FILE` in the host shell to an existing private file approved
for this experiment; do not commit it, paste its contents, or place the key in
an argument. If it resides on another machine, use an approved secure stdin
stream to the host. The following reads the key from stdin:

```sh
: "${ENMAAS_KEY_FILE:?Set ENMAAS_KEY_FILE to your private key file}"
set +e
python3 "$ATG_ROOT/experiments/harness/run_comparison.py" \
  --run-spec "$RUN_SPEC" \
  --fixture-root "$FIXTURE_ROOT" \
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

A nonzero exit can mean a timeout, blocked stage, or inconclusive test. Keep
the completed attempt, including failures; inspect `summary.json` rather
than treating the exit code alone as a candidate verdict. The runner will not
overwrite output. For each repeat, set `RUN_OUTPUT` to a new directory under
the same mounted `RUN_VOLUME` and use the same spec and arguments.

## 5. Archive and report

When the run finishes, check `summary.json` contains
`"evidence_scan": "passed"`. Never publish an unscanned or blocked packet.
The scan checks the exact key, not partial or transformed secrets. Review the
selected evidence before sharing it. Archive outside the mount and unmount
only after the archive has been verified:

```sh
ARCHIVE="$ARCHIVE_ROOT/quarkus-staged-run-001"
python3 "$ATG_ROOT/experiments/harness/archive_evidence.py" \
  --run-root "$RUN_OUTPUT" --output-dir "$ARCHIVE"
python3 "$ATG_ROOT/experiments/harness/build_findings.py" \
  --run-root "$ARCHIVE" --output-dir "$WORK_ROOT/findings-quarkus-001"
```

Use a new archive and report directory for each attempt. See
[FINDINGS.md](FINDINGS.md) for multi-run reports and reviewed decisions. Retain
or archive every completed attempt before unmounting; tmpfs contents disappear
on unmount:

```sh
umount "$RUN_VOLUME"
```

The Quarkus fixture has no configured JaCoCo coverage contract. The common
evaluator checks one seeded behavior: the generated test must pass on original
code and fail by assertion on the defective copy. This alone does not prove a
CVE fix or select a production tool.
