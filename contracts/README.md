# Contracts

Own versioned schemas and compatibility rules for requests, VPE/VEX input, policy decisions, objectives, plans, checkpoints, generation, execution, quality, provenance, and signed evidence.

## Version 1

The canonical, framework-neutral JSON Schemas live in `v1/`. Every document has a
stable `contract` discriminator and an explicit `version` so a stored artifact can
be interpreted without relying on the producer's deployment. Shared identities,
digests, source references, artifacts, limitations, and outcome taxonomy are defined
in `v1/common.schema.json`.

The lifecycle is represented by:

- `request`, `preflight`, `objective`, `plan`, and `checkpoint` for admission and review.
- `feedback`, `generation`, `execution`, and `quality` for Analyze -> Plan -> Code -> Execute.
- `provenance` and `signed-differential` for reproducible, owner-reviewable evidence.
- `run-state` and `run-event` for durable, idempotent workflow state transitions.
- `compatibility` for recording schema migration results instead of guessing compatibility.

The state-machine invariants and persistence boundary are documented in
`docs/architecture/run-state.md`. The reference examples in `examples/` and
negative fixtures in `invalid/` are validated with the optional JSON Schema
test dependency.

Individual schema validation is supplemented by cross-document integrity
checks in `src/atg/domain/contract_integrity.py`. These checks ensure that
plans, generations, executions, quality results, provenance, and signed
differentials belong to the same run and resolve their referenced IDs.

The HTTP boundary, schema publication, and signed-differential options are
documented in `docs/architecture/contract-distribution-and-api.md`.

## Rules

- Keep contracts provider- and framework-neutral.
- Preserve source identity and normalization provenance.
- Add compatibility tests for every schema change.
- Use `examples/` for small valid documents and `compatibility/` for migration tests.
- Do not place credentials, customer data, or unapproved authoritative VEX documents in fixtures.
- Treat `blocked`, `inconclusive`, `not_reached`, and `stated_gap` as explicit outcomes; never infer a pass from missing evidence.
- Treat model output, repository content, generated code, and execution logs as untrusted data.

## Integration

Service domain and adapter code consume contracts through `src/atg/ports/`. Contract changes require review from every affected workstream.
