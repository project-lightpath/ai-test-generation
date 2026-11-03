"""Build the static schema publication site."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

try:
    from .validate_contracts import validate_contracts
except ImportError:  # Script execution from the repository root.
    from validate_contracts import validate_contracts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--override-root", type=Path)
    args = parser.parse_args()
    root = Path(__file__).parents[1]
    schemas = validate_contracts(
        root / "contracts/v1",
        root / "contracts/examples",
        root / "contracts/compatibility",
        root / "contracts/invalid",
        args.override_root,
    )

    if args.output.exists():
        shutil.rmtree(args.output)
    output_contracts = args.output / "contracts/v1"
    output_contracts.mkdir(parents=True)
    for path in (root / "contracts/v1").glob("*.schema.json"):
        selected = args.override_root / path.name if args.override_root and (args.override_root / path.name).exists() else path
        shutil.copy2(selected, output_contracts / path.name)

    catalog = []
    for path in sorted(output_contracts.glob("*.schema.json")):
        content = path.read_bytes()
        schema = json.loads(content)
        catalog.append(
            {
                "name": path.name,
                "id": schema.get("$id"),
                "version": schema.get("properties", {}).get("version", {}).get("const"),
                "sha256": hashlib.sha256(content).hexdigest(),
                "path": f"contracts/v1/{path.name}",
            }
        )
    (args.output / "index.json").write_text(json.dumps({"schemas": catalog}, indent=2) + "\n")
    links = "\n".join(f'<li><a href="{item["path"]}">{item["name"]}</a></li>' for item in catalog)
    (args.output / "index.html").write_text(
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>ATG Schemas</title></head>"
        f"<body><h1>ATG Schemas</h1><ul>{links}</ul></body></html>\n"
    )
    (args.output / ".nojekyll").write_text("")
    print(f"Published {len(schemas)} schemas to {args.output}")


if __name__ == "__main__":
    main()
