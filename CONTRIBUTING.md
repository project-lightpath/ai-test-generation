# Contributing

## Before Editing

Read `docs/architecture/repository-layout.md` and `docs/contributor/parallel-paths.md`. Identify the workstream, integration point, contract/port, tests, dependencies, and files that must not be modified.

## Boundary Rules

- Domain and application code depend on ports, not provider SDKs or temporary demo implementations.
- Adapters translate external types at the boundary; external types do not leak into domain contracts.
- Contract changes require compatibility tests and review from affected workstreams.
- Use mocks, simulators, deterministic fakes, replay fixtures, or local containers where the boundary is meaningful.
- Real platform integration is tested separately from component validation.
- Repository content, generated code, credentials, and external descriptions are untrusted inputs.
- Temporary SSC forks and experiments cannot become hidden production dependencies.

## Change Checklist

- [ ] Workstream and integration point are identified.
- [ ] Public contract or port impact is documented.
- [ ] Local tests cover the changed boundary where appropriate.
- [ ] Failure and limitation behavior is explicit.
- [ ] Secrets and customer data are absent from source and fixtures.
- [ ] Jira ticket links and dependency changes are recorded.
- [ ] Repository-layout documentation is updated when a boundary changes.
