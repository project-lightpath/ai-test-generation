# Contracts

Own versioned schemas and compatibility rules for requests, VPE/VEX input, policy decisions, objectives, plans, checkpoints, generation, execution, quality, provenance, and signed evidence.

## Rules

- Keep contracts provider- and framework-neutral.
- Preserve source identity and normalization provenance.
- Add compatibility tests for every schema change.
- Use `examples/` for small valid documents and `compatibility/` for migration tests.
- Do not place credentials, customer data, or unapproved authoritative VEX documents in fixtures.

## Integration

Domain and adapter code consume contracts through `src/atg/ports/`. Contract changes require review from every affected workstream.
