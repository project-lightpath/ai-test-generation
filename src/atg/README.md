# ATG Service

The `src/atg/` tree contains the service implementation. Keep domain and application behavior independent from framework, model, execution-provider, SCM, and temporary demo implementations.

## Boundaries

- `domain/`: driver-neutral types, status taxonomy, invariants, and evidence relationships.
- `application/`: lifecycle use cases for admission, Analyze, Plan, Code, Execute, and evaluation.
- `workflow/`: checkpoints, state transitions, retries, callbacks, cancellation, and recovery.
- `gates/`: deterministic validation, quality, differential, and publication decisions.
- `provenance/`: digests, identities, manifests, limitations, and supersession.
- `ports/`: stable interfaces used by application/domain code.
- `adapters/`: translations to external systems and providers.
- `tools/`: bounded, authorized repository and analysis tools.

Implementations in `experiments/` or `deploy/` must not be imported directly into domain or application code.
