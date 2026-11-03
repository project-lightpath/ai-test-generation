# Tests

## Test Layers

- `unit/`: fast domain and application behavior.
- `contract/`: schemas and ports using mocks and simulators.
- `component/`: adapter behavior using local services or containers.
- `replay/`: recorded model, provider, and execution behavior.
- `integration/`: real approved endpoint, execution, evidence, and SCM paths.
- `differential/`: immutable base/candidate and environment comparisons.
- `evaluation/`: golden references and failure-register regression cases.

A passing mock or replay test does not prove platform integration. A platform failure must not be converted into a behavior pass.
