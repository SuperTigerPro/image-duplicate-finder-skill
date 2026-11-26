"""Exercise file identity, crop matching and cache semantics without downloading weights."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    import numpy as np
    from PIL import Image
    import copynce_engine as engine
except ImportError:
    engine = None


class ColorDescriptor:
    dimension = 4
    device = "cpu"
    weights_id = "test-descriptor"
    fallbacks = ()

    def encode(self, images):
        return np.array([[*np.asarray(image).mean(axis=(0, 1)), 1] for image in images], dtype=np.float32)


@unittest.skipIf(engine is None, "Model engine tests require Pillow and NumPy")
class ModelScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / "images"
        self.directory.mkdir()
        self.cache = self.root / "features.sqlite3"
        patch = mock.patch.object(engine, "_load_descriptor", return_value=ColorDescriptor())
        patch.start()
        self.addCleanup(patch.stop)

    def image(self, name, color="red"):
        path = self.directory / name
        Image.new("RGB", (24, 17), color).save(path)
        return path

    def run_scan(self, paths, **kwargs):
        return engine.scan(paths, weights=Path("test-weights"), cache=self.cache, **kwargs)

    def test_compares_multiple_roots_selects_best_boxes_and_caps_results(self):
        a = self.image("a.png")
        self.image("b.png")
        c = self.image("c.png", "blue")
        result = self.run_scan([a, self.directory, c], threshold=-1, top_k=1)
        self.assertEqual(result["candidate_file_count"], 3)
        self.assertEqual(result["view_count"], 66)
        self.assertEqual(result["pairs_assessed"], 3)
        self.assertTrue(result["truncated"])
        match = result["matches"][0]
        self.assertEqual({Path(match[k]).name for k in ("path_a", "path_b")}, {"a.png", "b.png"})
        self.assertGreater(match["score"], .99)
        self.assertEqual(len(match["box_b"]), 4)

    def test_cache_checks_signature_and_new_safety_limit(self):
        a = self.image("a.png")
        self.assertEqual(self.run_scan([a])["cache_misses"], 1)
        self.assertEqual(self.run_scan([a])["cache_hits"], 1)
        self.assertEqual(self.run_scan([a], max_pixels=2)["error_count"], 1)
        self.image("a.png", "green")
        self.assertEqual(self.run_scan([a])["cache_misses"], 1)

    def test_selection_manifest_prevents_directory_expansion(self):
        a = self.image("a.png")
        b = self.image("b.png")
        stamps = {str(p): list(engine.core.signature(p.stat())) for p in (a, b)}
        a.unlink()
        a.mkdir()
        Image.new("RGB", (2, 2)).save(a / "unexpected.png")
        result = self.run_scan([a, b], file_stamps=stamps)
        self.assertEqual(result["candidate_file_count"], 2)
        self.assertEqual(result["image_count"], 1)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["errors"][0]["path"], str(a))

    def test_selection_manifest_reports_missing_or_changed_regular_images(self):
        a = self.image("a.png")
        b = self.image("b.png")
        stamps = {str(p): list(engine.core.signature(p.stat())) for p in (a, b)}
        a.unlink()
        Image.new("RGB", (3, 4)).save(b)
        result = self.run_scan([a, b], file_stamps=stamps)
        self.assertEqual(result["error_count"], 2)
        self.assertEqual(result["image_count"], 0)
        self.assertEqual(result["pairs_assessed"], 0)

    def test_partial_scan_marks_multiframe_and_corrupt_inputs(self):
        a = self.image("a.png")
        gif = a.with_suffix(".gif")
        frame = Image.new("RGB", (3, 3), "red")
        frame.save(gif, save_all=True, append_images=[Image.new("RGB", (3, 3), "green")])
        (self.directory / "bad.png").write_bytes(b"invalid image")
        self.image(".hidden.png")
        result = self.run_scan([self.directory], tiles=False)
        self.assertEqual(result["image_count"], 1)
        self.assertEqual(result["error_count"], 2)
        self.assertEqual(result["skipped"]["hidden_entries"], 1)

    def test_directory_symlink_entries_are_excluded(self):
        a = self.image("a.png")
        link = a.with_name("link.png")
        try:
            os.symlink(a.name, link)
        except OSError:
            self.skipTest("symlink creation unavailable")
        self.assertEqual(self.run_scan([self.directory])["image_count"], 1)
        with self.assertRaises(ValueError):
            self.run_scan([link])

    def test_unsupported_explicit_index_does_not_silently_change_modes(self):
        self.image("a.png")
        with mock.patch.dict(sys.modules, {"faiss": None}):
            with self.assertRaisesRegex(RuntimeError, "faiss"):
                self.run_scan([self.directory], mode="indexed")

    def test_resource_limits_preserve_partial_results_and_mark_errors(self):
        for name in ("a.png", "b.png", "c.png"):
            self.image(name)
        with mock.patch.object(engine, "MAX_IMAGES", 2):
            raw = self.run_scan([self.directory], tiles=False)
        self.assertEqual((raw["candidate_file_count"], raw["image_count"], raw["error_count"]), (3, 2, 1))
        self.assertEqual(raw["errors"][0]["operation"], "limit")
        with mock.patch.object(engine, "MAX_VIEWS", 22):
            raw = self.run_scan([self.directory])
        self.assertEqual((raw["image_count"], raw["error_count"]), (1, 1))
        self.assertIn("ModelLimitError", raw["errors"][0]["message"])

    def test_files_changed_during_retrieval_are_not_delivered(self):
        a = self.image("a.png")
        self.image("b.png")
        original = engine._match
        def change_after_match(*args):
            found = original(*args)
            a.unlink()
            return found
        with mock.patch.object(engine, "_match", side_effect=change_after_match):
            raw = self.run_scan([self.directory], tiles=False)
        self.assertEqual(raw["matches"], [])
        self.assertEqual(raw["error_count"], 1)
        self.assertEqual(raw["errors"][0]["operation"], "verify")

@unittest.skipIf(engine is None, "The model requires Pillow and NumPy")
class DescriptorFallbackTests(unittest.TestCase):
    def test_gpu_oom_reduces_batch_before_reverting_to_cpu(self):
        try:
            import copynce_model as model
        except ImportError:
            self.skipTest("torch is optional")
        descriptor = object.__new__(model.Descriptor)
        descriptor.device = "cuda"
        descriptor.fallbacks = []
        descriptor.max_batch = None
        observed = []

        def forward(batch):
            observed.append((descriptor.device, len(batch)))
            if descriptor.device == "cuda":
                raise RuntimeError("CUDA out of memory")
            return np.ones((len(batch), 512), dtype=np.float32)

        def move(target):
            descriptor.device = str(target)

        with mock.patch.object(descriptor, "_forward", side_effect=forward), mock.patch.object(descriptor, "_move", side_effect=move):
            encoded = descriptor.encode([None] * 4)
        self.assertEqual(observed[:4], [("cuda", 4), ("cuda", 2), ("cuda", 1), ("cpu", 1)])
        self.assertEqual(encoded.shape, (4, 512))
        self.assertEqual(descriptor.device, "cpu")
