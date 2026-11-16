import pathlib
import sys
import unittest
from tempfile import TemporaryDirectory

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.adapters.persistence.sqlite_run_state import SQLiteRunStateStore
from atg.application.generation import GenerationService
from tests.unit.test_analysis import create_repository
from tests.unit.test_generation import candidate, prepare_coding_store, request


class GenerationReplayTests(unittest.TestCase):
    def test_repair_artifacts_and_lifecycle_replay_after_restart(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "repository"
            root.mkdir()
            create_repository(root)
            database = pathlib.Path(directory) / "atg.sqlite3"
            store, _ = prepare_coding_store(root, database)
            bad = candidate("def test_target_handler():\n    # target-handler\n    value = 1\n")
            created = GenerationService(store).generate(request(
                store.get_run("run-1"), first=bad, repairs=(candidate(),)
            ))
            store.close()

            reopened = SQLiteRunStateStore(database)
            persisted = reopened.get_generation("generation-1")
            replayed = reopened.replay("run-1")
            self.assertEqual(persisted.manifest, created.manifest)
            self.assertEqual(len(persisted.attempts), 2)
            self.assertEqual(replayed.completed_generation_id, "generation-1")
            reopened.close()


if __name__ == "__main__":
    unittest.main()
