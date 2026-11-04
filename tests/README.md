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

## Local Contract Validation

The current service components have no third-party runtime dependencies. Contract
validation uses the optional `jsonschema` test dependency:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

The CI workflow runs the same suite by layer:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -p 'test_*.py' -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/contract -p 'test_*.py' -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/component -p 'test_*.py' -v
```

The test suite checks real JSON Schema examples and invalid fixtures, then
exercises run-state idempotency, stale-event rejection, checkpoint
supersession/expiry, retry recovery, SQLite replay, and the HTTP component
boundary.

Pull requests run the same validation through
`.github/workflows/atg-contracts-and-components.yml`.
Schema publication is intentionally separate and only deploys from `main`.
