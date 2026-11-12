import json
import pathlib
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2]))

from scripts.validate_contracts import load_schema_documents, validate_contracts  # noqa: E402


ROOT = pathlib.Path(__file__).parents[2]


class SchemaToolTests(unittest.TestCase):
    def test_repository_contracts_validate_through_public_script(self):
        schemas = validate_contracts(
            ROOT / "contracts/v1",
            ROOT / "contracts/examples",
            ROOT / "contracts/compatibility",
            ROOT / "contracts/invalid",
        )
        self.assertEqual(len(schemas), 20)

    def test_complete_override_profile_replaces_only_selected_schema(self):
        with TemporaryDirectory() as directory:
            override = pathlib.Path(directory)
            base = json.loads((ROOT / "contracts/v1/generation.schema.json").read_text())
            base["title"] = "ATG generation development override"
            (override / "generation.schema.json").write_text(json.dumps(base))
            schemas = load_schema_documents(ROOT / "contracts/v1", override)
            self.assertEqual(schemas["generation"]["title"], "ATG generation development override")
            self.assertNotEqual(schemas["generation"]["title"], "ATG generated test change")
            self.assertEqual(
                schemas["request"]["$id"],
                "https://project-lightpath.github.io/ai-test-generation/contracts/v1/request.schema.json",
            )

    def test_missing_override_profile_fails_loudly(self):
        with TemporaryDirectory() as directory:
            missing = pathlib.Path(directory) / "does-not-exist"
            with self.assertRaises(FileNotFoundError):
                validate_contracts(
                    ROOT / "contracts/v1",
                    ROOT / "contracts/examples",
                    ROOT / "contracts/compatibility",
                    ROOT / "contracts/invalid",
                    missing,
                )


if __name__ == "__main__":
    unittest.main()
