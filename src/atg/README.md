# ATG Service

The `src/atg/` tree contains the ATG service implementation under development.
Keep domain and application behavior independent from framework, model,
execution-provider, SCM, and temporary demo implementations. If the service is
later split into multiple deployables, preserve these boundaries and contracts.

## Boundaries

- `domain/`: driver-neutral types, status taxonomy, invariants, and evidence relationships.
- `application/`: lifecycle use cases for admission, Analyze, Plan, Code, Execute, and evaluation.
- `workflow/`: checkpoints, state transitions, retries, callbacks, cancellation, and recovery.
- `gates/`: deterministic validation, quality, differential, and publication decisions.
- `provenance/`: digests, identities, manifests, limitations, and supersession.
- `ports/`: stable interfaces used by application/domain code.
- `adapters/`: translations to external systems and providers.
- `tools/`: bounded, authorized repository and analysis tools.

The dependency-free run-state implementation is in `domain/run_state.py`. It is
persistence- and framework-neutral;
workflow adapters should journal `RunEvent` values and persist the resulting
`RunState` atomically.

The SQLite persistence adapter and HTTP component are initial development
implementations used to test that boundary across transactions and process
restarts. They can evolve in place or be replaced by a Java/Quarkus runtime.

Implementations in `experiments/` or `deploy/` must not be imported directly into domain or application code.
