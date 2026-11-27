"""Bundled SN must work offline without exposing a writable repository index."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import copynce_default_sn as default


class DefaultBackgroundTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def test_real_bundled_index_is_materialized_and_loads_offline(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("SN must not download")):
            path = default.prepare_default_sn(self.root)
        self.assertEqual(path.parent, self.root)
        self.assertNotEqual(path, default.BUNDLED_INDEX)
        for name, (size, checksum) in default.DEFAULT_FILES.items():
            self.assertEqual((path / name).stat().st_size, size)
            self.assertEqual(hashlib.sha256((path / name).read_bytes()).hexdigest(), checksum)
            if os.name != "nt":
                self.assertEqual((path / name).stat().st_mode & 0o777, 0o600)
        try:
            import copynce_sn
        except ImportError:
            self.skipTest("NumPy or Pillow unavailable for vector inspection")
        index = copynce_sn.load_index(path, expected_weights_sha256=default.setup.WEIGHT_SHA256)
        self.assertEqual(index.vectors.shape, (1000, 512))
        self.assertEqual(index.fingerprint, default.DEFAULT_FINGERPRINT)

    def test_valid_cache_can_be_reused_without_bundle_or_network(self):
        path = default.prepare_default_sn(self.root)
        before = {n: (path / n).stat().st_mtime_ns for n in default.DEFAULT_FILES}
        with mock.patch.object(default, "BUNDLED_INDEX", self.root / "missing-bundle"), \
                mock.patch.object(default.shutil, "copyfile", side_effect=AssertionError("warm cache")):
            self.assertEqual(default.prepare_default_sn(self.root), path)
        self.assertEqual({n: (path / n).stat().st_mtime_ns for n in default.DEFAULT_FILES}, before)

    def test_corrupt_default_cache_is_repaired_from_pinned_bundle(self):
        path = default.prepare_default_sn(self.root)
        (path / "features.npz").write_bytes(b"damaged cached resource")
        self.assertEqual(default.prepare_default_sn(self.root), path)
        self.assertEqual(hashlib.sha256((path / "features.npz").read_bytes()).hexdigest(),
                         default.DEFAULT_FILES["features.npz"][1])

    def test_tampered_bundle_is_rejected_without_partial_index(self):
        bundle = self.root / "bad-bundle"
        shutil.copytree(default.BUNDLED_INDEX, bundle)
        payload = bytearray((bundle / "features.npz").read_bytes())
        payload[-1] ^= 1
        (bundle / "features.npz").write_bytes(payload)
        cache = self.root / "cache"
        with mock.patch.object(default, "BUNDLED_INDEX", bundle), self.assertRaisesRegex(ValueError, "校验"):
            default.prepare_default_sn(cache)
        self.assertFalse((cache / default.DEFAULT_NAME).exists())
        self.assertEqual(list(cache.glob(".default-sn-*")), [])

    def test_does_not_write_inside_any_git_worktree(self):
        project = self.root / "project"
        project.mkdir()
        (project / ".git").write_text("gitdir: fixture")
        with self.assertRaisesRegex(ValueError, "Git"):
            default.prepare_default_sn(project / "cache")
        self.assertFalse((project / "cache").exists())

    @unittest.skipIf(os.name == "nt", "POSIX directory ownership and write bits")
    def test_shared_writable_cache_root_is_rejected(self):
        self.root.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "其他用户"):
            default.prepare_default_sn(self.root)
        self.root.chmod(0o700)

    def test_symlink_lock_is_rejected_without_changing_destination(self):
        sentinel = self.root / "untouched"
        sentinel.write_text("preserve")
        try:
            (self.root / ".default-sn.lock").symlink_to(sentinel)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable")
        with self.assertRaisesRegex(ValueError, "普通文件"):
            default.prepare_default_sn(self.root)
        self.assertEqual(sentinel.read_text(), "preserve")

    def test_symlink_target_does_not_redirect_repairs(self):
        other = self.root / "untouched"
        other.mkdir()
        sentinel = other / "sentinel"
        sentinel.write_text("preserve")
        target = self.root / default.DEFAULT_NAME
        try:
            target.symlink_to(other, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            default.prepare_default_sn(self.root)
        self.assertEqual(sentinel.read_text(), "preserve")

    def test_repair_never_removes_unexpected_user_files(self):
        path = default.prepare_default_sn(self.root)
        (path / "features.npz").write_bytes(b"damaged")
        sentinel = path / "user-notes.txt"
        sentinel.write_text("preserve")
        with self.assertRaisesRegex(ValueError, "意外文件"):
            default.prepare_default_sn(self.root)
        self.assertEqual(sentinel.read_text(), "preserve")

    def test_concurrent_preparation_returns_one_complete_index(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: default.prepare_default_sn(self.root), range(3)))
        self.assertEqual(len(set(results)), 1)
        default._private_index(results[0])
        self.assertEqual(list(self.root.glob(".default-sn-*")), [])


if __name__ == "__main__":
    unittest.main()
