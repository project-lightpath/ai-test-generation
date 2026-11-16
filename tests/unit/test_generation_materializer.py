import pathlib
import subprocess
import sys
import unittest
from tempfile import TemporaryDirectory

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.adapters.generation.deterministic import FilesystemPatchMaterializer
from atg.adapters.generation.compatibility import to_ssc_tekton_compatibility
from atg.domain.generation import canonical_patch, compute_materialized_digest, compute_patch_digest
from atg.ports.generation import MaterializationResult, PatchCandidate


class GenerationMaterializerTests(unittest.TestCase):
    def test_canonical_patch_preserves_final_newline_and_applies_exactly(self):
        cases = (
            ("tests/NewTest.java", None, "class NewTest {}"),
            ("tests/ExistingTest.java", "class ExistingTest {}\n", "class ExistingTest {}"),
            ("tests/RestoreNewline.java", "class RestoreNewline {}", "class RestoreNewline {}\n"),
        )
        for path, before, after in cases:
            with self.subTest(path=path), TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
                target = root / path
                target.parent.mkdir(parents=True)
                if before is not None:
                    target.write_bytes(before.encode())
                patch = canonical_patch(
                    {} if before is None else {path: before},
                    {path: after},
                )
                parsed = subprocess.run(
                    ["git", "apply", "--numstat", "-"],
                    cwd=root,
                    input=patch,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                self.assertTrue(parsed.stdout.strip())
                subprocess.run(
                    ["git", "apply", "-"],
                    cwd=root,
                    input=patch,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                self.assertEqual(target.read_bytes(), after.encode())

    def test_materializes_exact_patch_within_allowed_workspace(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "workspace"
            root.mkdir()
            path = "tests/test_generated.py"
            content = "def test_example():\n    assert subject_value == 1\n"
            candidate = PatchCandidate(canonical_patch({}, {path: content}), {path: content})
            result = FilesystemPatchMaterializer().materialize(
                candidate,
                str(root),
                {"kind": "git-tree", "id": "candidate", "version": "abc123"},
                ("tests/",),
                compute_patch_digest(candidate.patch_text),
                compute_materialized_digest(candidate.materialized_files),
            )
            self.assertEqual((root / path).read_text(), content)
            self.assertEqual(result.changed_files, (path,))
            self.assertEqual(result.to_manifest()["allowed_paths"], ["tests/"])
            self.assertEqual(result.materialized_test_digest, compute_materialized_digest({path: content}))

    def test_modification_requires_exact_base_content(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = "tests/test_existing.py"
            before = "def test_existing():\n    assert value == 0\n"
            after = "def test_existing():\n    assert value == 1\n"
            (root / "tests").mkdir()
            (root / path).write_text(before)
            candidate = PatchCandidate(
                canonical_patch({path: before}, {path: after}),
                {path: after},
                {path: before},
            )
            result = FilesystemPatchMaterializer().materialize(
                candidate, str(root), {"kind": "git-tree", "id": "candidate"},
                ("tests/",), compute_patch_digest(candidate.patch_text),
                compute_materialized_digest(candidate.materialized_files),
            )
            self.assertEqual((root / path).read_text(), after)
            self.assertEqual(result.changed_files, (path,))

    def test_rejects_workspace_base_mismatch_without_overwrite(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = "tests/test_existing.py"
            (root / "tests").mkdir()
            (root / path).write_text("unexpected base\n")
            expected = "expected base\n"
            content = "new content\n"
            candidate = PatchCandidate(
                canonical_patch({path: expected}, {path: content}),
                {path: content},
                {path: expected},
            )
            with self.assertRaisesRegex(ValueError, "base content mismatch"):
                FilesystemPatchMaterializer().materialize(
                    candidate, str(root), {"kind": "git-tree", "id": "candidate"},
                    ("tests/",), compute_patch_digest(candidate.patch_text),
                    compute_materialized_digest(candidate.materialized_files),
                )
            self.assertEqual((root / path).read_text(), "unexpected base\n")

    def test_rejects_symlinked_allowed_directory(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as outside:
            root = pathlib.Path(directory) / "workspace"
            root.mkdir()
            (root / "tests").symlink_to(outside, target_is_directory=True)
            path = "tests/test_escape.py"
            content = "def test_escape():\n    assert True\n"
            candidate = PatchCandidate(canonical_patch({}, {path: content}), {path: content})
            with self.assertRaisesRegex(ValueError, "symlink"):
                FilesystemPatchMaterializer().materialize(
                    candidate, str(root), {"kind": "git-tree", "id": "candidate"},
                    ("tests/",), compute_patch_digest(candidate.patch_text),
                    compute_materialized_digest(candidate.materialized_files),
                )
            self.assertEqual(list(pathlib.Path(outside).iterdir()), [])

    def test_rejects_patch_or_materialized_digest_mismatch(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = "tests/test_generated.py"
            content = "def test_example():\n    assert value == 1\n"
            candidate = PatchCandidate(canonical_patch({}, {path: content}), {path: content})
            with self.assertRaisesRegex(ValueError, "patch digest mismatch"):
                FilesystemPatchMaterializer().materialize(
                    candidate, str(root), {"kind": "git-tree", "id": "candidate"},
                    ("tests/",), "sha256:" + "0" * 64,
                    compute_materialized_digest(candidate.materialized_files),
                )

    def test_rejects_patch_that_does_not_reproduce_materialized_content(self):
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = "tests/test_generated.py"
            actual = "def test_example():\n    assert value == 1\n"
            different = "def test_example():\n    assert value == 2\n"
            candidate = PatchCandidate(canonical_patch({}, {path: different}), {path: actual})
            with self.assertRaisesRegex(ValueError, "patch content differs"):
                FilesystemPatchMaterializer().materialize(
                    candidate, str(root), {"kind": "git-tree", "id": "candidate"},
                    ("tests/",), compute_patch_digest(candidate.patch_text),
                    compute_materialized_digest(candidate.materialized_files),
                )

    def test_compatibility_projection_is_optional_and_digest_bound(self):
        result = MaterializationResult(
            workspace_identity={"kind": "git-tree", "id": "candidate"},
            changed_files=("tests/test_generated.py",),
            allowed_paths=("tests/",),
            patch_digest="sha256:" + "a" * 64,
            materialized_test_digest="sha256:" + "b" * 64,
        )
        record = type("Record", (), {"manifest": {
            "status": "verified",
            "base": dict(result.workspace_identity),
            "candidate": {"kind": "git-tree", "id": "base"},
            "patch_digest": result.patch_digest,
            "materialized_test_digest": result.materialized_test_digest,
        }})()
        projection = to_ssc_tekton_compatibility(
            record, result, run_ref="taskrun-1", evidence_ref="artifact-generation-1"
        ).to_document()
        self.assertEqual(projection["TESTS_ADDED"], ["tests/test_generated.py"])
        self.assertEqual(projection["RUN_REF"], "taskrun-1")
        self.assertIsNone(projection["PR_URL"])

    def test_compatibility_projection_rejects_wrong_subject(self):
        result = MaterializationResult(
            workspace_identity={"kind": "git-tree", "id": "wrong-subject"},
            changed_files=("tests/test_generated.py",),
            allowed_paths=("tests/",),
            patch_digest="sha256:" + "a" * 64,
            materialized_test_digest="sha256:" + "b" * 64,
        )
        record = type("Record", (), {"manifest": {
            "status": "verified",
            "base": {"kind": "git-tree", "id": "base"},
            "candidate": {"kind": "git-tree", "id": "candidate"},
            "patch_digest": result.patch_digest,
            "materialized_test_digest": result.materialized_test_digest,
        }})()
        with self.assertRaisesRegex(ValueError, "workspace does not match"):
            to_ssc_tekton_compatibility(record, result)

    def test_compatibility_projection_rejects_digest_mismatch(self):
        result = MaterializationResult(
            workspace_identity={"kind": "git-tree", "id": "candidate"},
            changed_files=("tests/test_generated.py",),
            allowed_paths=("tests/",),
            patch_digest="sha256:" + "a" * 64,
            materialized_test_digest="sha256:" + "b" * 64,
        )
        record = type("Record", (), {"manifest": {
            "status": "verified",
            "base": {"kind": "git-tree", "id": "base"},
            "candidate": dict(result.workspace_identity),
            "patch_digest": "sha256:" + "c" * 64,
            "materialized_test_digest": result.materialized_test_digest,
        }})()
        with self.assertRaisesRegex(ValueError, "differs from canonical generation"):
            to_ssc_tekton_compatibility(record, result)


if __name__ == "__main__":
    unittest.main()
