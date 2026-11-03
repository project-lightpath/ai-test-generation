import json
import pathlib
import shutil
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
        self.assertEqual(len(schemas), 16)

    def test_complete_override_profile_replaces_only_selected_schema(self):
        with TemporaryDirectory() as directory:
            override = pathlib.Path(directory)
            shutil.copy2(ROOT / "contracts/v1/generation.schema.json", override / "generation.schema.json")
            schemas = load_schema_documents(ROOT / "contracts/v1", override)
            base = json.loads((ROOT / "contracts/v1/generation.schema.json").read_text())
            self.assertEqual(schemas["generation"], base)
            self.assertEqual(schemas["request"]["$id"], "https://project-lightpath.github.io/ai-test-generation/contracts/v1/request.schema.json")


if __name__ == "__main__":
    unittest.main()
