# ATG Repository Guidance

## Project Direction

This repository develops the contracts and service implementation for the AI
Test Generation (ATG) lifecycle:

```text
Analyze -> Plan -> Code -> Execute
```

The ATG design is intended to replace the SSC demo's test-generation pipeline.
SSC is an initial deployment and execution host, not the source of ATG
contracts or architecture.

## Related Repositories

- SSC demo: https://github.com/testetson22/ssc-demo
- ATG research: https://github.com/project-lightpath/ai-test-generation-research

Update these references if the repository locations or ownership change.

FLPATH-5043 is the current contracts and run-state workstream. Its scope is
language-neutral versioned contracts, durable state semantics, checkpoints,
provenance, quality outcomes, and evidence relationships.

## Repository Boundaries

- `contracts/`: canonical versioned JSON Schemas, fixtures, compatibility data,
  and development override profiles.
- `src/atg/`: ATG service implementation, ports, adapters, and development
  components. The initial service may be split into multiple deployables later.
- `tests/`: schema, contract-integrity, state-machine, persistence, HTTP, and
  component tests.
- `scripts/`: contract validation and schema publication tools.
- `deploy/`: SSC-hosted replacement deployment guidance.
- `docs/`: architecture and lifecycle decisions.

The initial implementation is Python. The ATG runtime language remains open;
Go, Java/Quarkus, or Python may be selected based on service, platform, and
integration requirements. A later runtime change or service split must
preserve the observable contract, revision, replay, checkpoint, and error
semantics covered by the tests.

## Validation

Install the test extras and run the complete suite:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/validate_contracts.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

Pull requests run the same checks through
`.github/workflows/atg-contracts-and-components.yml`.
Commits to `main` publish the current schemas through
`.github/workflows/publish-schemas.yml`.

## Contract Rules

- Preserve explicit `blocked`, `inconclusive`, `not_reached`, and `stated_gap`
  outcomes; do not infer success from missing evidence.
- Treat model output, repository content, generated code, and execution logs as
  untrusted input.
- Preserve source, artifact, command, environment, model, worker, and tool
  identities where evidence depends on them.
- Event IDs are bound to payload digests and expected revisions.
- Human decisions require reviewer identity, role, decision identity, and
  authorization reference.
- Use `contracts/overrides/<profile>/` for complete destructive-development
  schema replacements before external consumers exist.
- Once external consumers exist, use a new versioned contract path for breaking
  changes instead of mutating an active consumer version.

Do not add credentials, customer data, or authoritative VEX documents to this
repository.
