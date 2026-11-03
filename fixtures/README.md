# Fixtures

Fixtures are versioned, minimal, and classified as authoritative-shape, synthetic, replay, golden-reference, or failure-register data.

- `vex/` and `policy/`: input and decision fixtures; synthetic data must be clearly marked.
- `repositories/`: immutable target references and metadata, not uncontrolled source copies.
- `execution/`: normalized replay artifacts and environment manifests.
- `golden/`: expected behavior/evidence references.
- `failure-register/`: known failure and regression cases.

Never commit credentials, customer data, model secrets, or unapproved authoritative vulnerability records.
