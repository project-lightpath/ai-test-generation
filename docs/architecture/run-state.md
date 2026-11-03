# Run-State Contract

The ATG run state is an append-only event projection. A state update is valid
only when the event's `expected_revision` equals the stored revision. The event
journal and state snapshot must be committed atomically.

## State Flow

```text
requested -> admitted -> analyzing -> analysis_review
analysis_review --approved--> planning -> plan_review
plan_review --approved--> coding -> code_review
code_review --approved--> executing -> execution_review
execution_review --approved--> quality_review -> quality_review
quality_review --quality--> completed | blocked | inconclusive | stated_gap

feedback may return a run to Analyze, Plan, Code, or Execute without silently
retaining the superseded active checkpoint.
```

Review checkpoints may be explicitly superseded or expired. Expiry returns the
run to the stage that created the checkpoint. `blocked` and `inconclusive` runs
may transition through an explicit `retry_requested` event; cancellation and
stated gaps are terminal for that run.

## Invariants

- Event IDs are unique per journal and are bound to a payload digest.
- Replaying an identical event is a successful no-op.
- Reusing an event ID with a different payload is rejected.
- An event based on an old revision is rejected.
- A checkpoint decision must identify the active checkpoint, decision ID,
  authenticated reviewer identity, reviewer role, and authorization reference.
- A replacement checkpoint must explicitly name the checkpoint it supersedes.
- Generated candidates require a patch digest before they can be verified.
- Execution evidence requires subject, worker, tool, command, command digest,
  environment, and timing identities.
- Differential evidence references both executions, quality evidence, complete
  provenance, and a signed canonical payload.

## Persistence Boundary

`src/atg/ports/run_state.py` is the service persistence port.
`SQLiteRunStateStore` is the initial development adapter used for component
tests and local execution. A later Go, Java/Quarkus, or other selected runtime
must preserve the same event, revision, replay, and checkpoint semantics.
