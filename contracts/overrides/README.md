# Development Overrides

The base schemas under `v1/` are intentionally mutable during early
development because there are no external consumers yet. When experimenting
with a destructive contract change, create a complete replacement schema under
an explicit profile directory, for example:

```text
contracts/overrides/experiment-2026-11/
  generation.schema.json
  common.schema.json       # include when shared definitions change
```

Override files replace the base file with the same name. They are not JSON
patches or partial fragments. Validate an override profile with:

```bash
python scripts/validate_contracts.py \
  --override-root contracts/overrides/experiment-2026-11
```

The GitHub Pages workflow can publish a selected override profile through a
manual workflow dispatch. Once a real consumer exists, destructive changes
must use a new versioned contract path instead of an override.
