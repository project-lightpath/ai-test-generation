# SSC Development Harness

The SSC demo is the first execution host for ATG, not the source of ATG test
SSC demo's `agentic-test-generation` pipeline and `ai-generate-tests` task.

## Reuse From SSC

The replacement may reuse the harness infrastructure:

- OpenShift Pipelines/Tekton and its workspace/PVC provisioning.
- Git repository access and Maven settings/artifact repository configuration.
- Approved AI endpoint configuration and credential references.
- Isolated worker images and SCM publication credentials where still approved.

These are deployment concerns. They do not define ATG request, run-state,
review, evidence, or outcome contracts.

## Replacement Flow

The replacement entrypoint must implement the ATG lifecycle rather than wrap
the old task result fields:

```text
request/admission
  -> Analyze -> durable Plan review
  -> Code -> durable generated-change review
  -> independent base/candidate Execute
  -> quality/differential gates
  -> provenance/evidence publication
```

The existing SSC slash-command trigger and pipeline name can be retained only
as a deployment migration mechanism. The resulting run must emit the ATG v1
contracts, not `TESTS_ADDED`, `ATTEMPTS`, or `PR_URL` as its public result
model.

## Migration Boundary

The first replacement implementation should provide a Tekton-facing adapter
that accepts an ATG request and writes durable contract artifacts to the
approved evidence workspace. It can initially invoke the SSC Maven/artifact
and SCM infrastructure, but it must own admission, checkpoints, run-state,
base/candidate isolation, normalized outcomes, and provenance.

## Local Component

Until the authoritative service runtime is selected, the repository provides a
dependency-free SQLite-backed development component:

```bash
PYTHONPATH=src python3 -m atg.component.run_state_server --database /tmp/atg.sqlite3
```

It exposes `POST /runs`, `POST /runs/{run_id}/events`, `GET /runs/{run_id}`,
and `GET /runs/{run_id}/events`. Component tests use this process boundary to
verify persistence and replay behavior; it is not the production deployment.
