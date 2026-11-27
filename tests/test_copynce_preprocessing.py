"""Check selected-window pixel parity, memory bounds and input invalidation."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    import numpy as np
    from PIL import Image
    import copynce_engine as engine
    import copynce_pipeline as pipeline
except ImportError:
    engine = None


@unittest.skipIf(engine is None, "Preprocessing needs optional image dependencies")
class SelectedWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def record(self, path):
        info = path.stat()
        return engine.core.ImageFile(path, info.st_size, engine.core.signature(info))

    def make_image(self, name="image.png", size=(113, 79), exif=False):
        rng = np.random.default_rng(123)
        pixels = rng.integers(0, 256, (*reversed(size), 3), dtype=np.uint8)
        path = self.root / name
        image = Image.fromarray(pixels)
        try:
            if exif:
                metadata = Image.Exif()
                metadata[274] = 6
                image.save(path, exif=metadata)
            else:
                image.save(path)
        finally:
            image.close()
        return self.record(path)

    def test_selected_views_equal_full_decode_including_exif_and_tiny_images(self):
        for name, size, exif in (("regular.png", (113, 79), False),
                                 ("oriented.jpg", (91, 73), True),
                                 ("tiny.png", (2, 3), False)):
            with self.subTest(name=name):
                record = self.make_image(name, size, exif)
                shape, boxes, full = engine._decode(record, True, 40000000)
                indices = [len(boxes) - 1, 0, len(boxes) // 2, 0]
                other_shape, other_boxes, selected = engine._decode(record, True, 40000000,
                                                                    view_indices=indices)
                try:
                    self.assertEqual((shape, boxes), (other_shape, other_boxes))
                    self.assertEqual(len(selected), len(indices))
                    for image, index in zip(selected, indices):
                        np.testing.assert_array_equal(np.asarray(image), np.asarray(full[index]))
                finally:
                    for image in full + selected:
                        image.close()

    def test_unrequested_windows_are_not_resized(self):
        record = self.make_image()
        calls = []
        original = Image.Image.resize

        def counted(image, *args, **kwargs):
            calls.append(image.size)
            return original(image, *args, **kwargs)

        with mock.patch.object(Image.Image, "resize", counted):
            _, boxes, selected = engine._decode(record, True, 40000000, view_indices=[0, 7, 21])
        try:
            self.assertEqual(len(boxes), 22)
            self.assertEqual(len(calls), 3)
        finally:
            for image in selected:
                image.close()

    def test_invalid_indices_keep_decode_validation(self):
        record = self.make_image()
        for indices in ([22], [-1], [True]):
            with self.subTest(indices=indices), self.assertRaises(ValueError):
                engine._decode(record, True, 40000000, view_indices=indices)
        with self.assertRaisesRegex(ValueError, "pixel limit"):
            engine._decode(record, True, 2, view_indices=[0])

    def test_cache_reuses_only_required_immutable_pixels_and_obeys_byte_bound(self):
        record = self.make_image()
        size, boxes, full = engine._decode(record, True, 40000000)
        try:
            item = (str(record.path), size, boxes, None)
            view_bytes = 224 * 224 * 3
            cache = pipeline._ImageViewCache(2 * view_bytes)
            with mock.patch.object(engine, "_decode", wraps=engine._decode) as decoded:
                first = cache.get(item, record, [0, 1], 40000000)
                again = cache.get(item, record, [1, 0, 1], 40000000)
                self.assertEqual(decoded.call_count, 1)
                self.assertIs(first[0], again[0])
                self.assertFalse(first[0].flags.writeable)
                cache.get(item, record, [2], 40000000)
                self.assertLessEqual(cache.bytes, 2 * view_bytes)
                self.assertEqual(len(cache.views), 2)
                # Eviction cannot invalidate arrays already held by the active pair.
                np.testing.assert_array_equal(first[0], np.asarray(full[0]))
        finally:
            for image in full:
                image.close()

    def test_cache_hit_still_rejects_changed_source(self):
        record = self.make_image()
        size, boxes, full = engine._decode(record, True, 40000000)
        for image in full:
            image.close()
        cache = pipeline._ImageViewCache()
        item = (str(record.path), size, boxes, None)
        cache.get(item, record, [0], 40000000)
        os.utime(record.path, ns=(record.stamp[3], record.stamp[3] + 1000000))
        with self.assertRaises(OSError):
            cache.get(item, record, [0], 40000000)

    def test_enhanced_directions_and_windows_match_full_decode(self):
        rng = np.random.default_rng(19)
        records, items = {}, []
        full_images = []
        try:
            for i in range(2):
                record = self.make_image(f"source-{i}.png", (113 + i * 2, 79 + i * 2))
                size, boxes, images = engine._decode(record, True, 40000000)
                full_images.append(images)
                vectors = rng.normal(size=(len(boxes), 12)).astype(np.float32)
                vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
                items.append((str(record.path), size, boxes, vectors))
                records[str(record.path)] = record

            class PixelMatcher:
                def score_pairs(self, queries, references):
                    return np.array([float(np.asarray(q).mean()) / 1024 +
                                     float(np.asarray(r)[:, :, 0].mean()) / 512
                                     for q, r in zip(queries, references)], dtype=np.float32)

            matcher = PixelMatcher()
            bias = np.zeros(2, dtype=np.float32)
            _, chosen = pipeline._evidence(items, 0, 1, bias, 2)
            views = [(0, 0), *chosen]
            ab = matcher.score_pairs([full_images[0][x] for x, _ in views],
                                     [full_images[1][y] for _, y in views])
            ba = matcher.score_pairs([full_images[1][y] for _, y in views],
                                     [full_images[0][x] for x, _ in views])
            best = int(np.maximum(ab, ba).argmax())
            row, count = pipeline._verify_pair(items, records, 0, 1, matcher, "enhanced", 2, bias,
                                                40000000, view_cache=pipeline._ImageViewCache())
            self.assertEqual(count, 6)
            self.assertEqual(row["matcher_a_to_b"], float(ab.max()))
            self.assertEqual(row["matcher_b_to_a"], float(ba.max()))
            self.assertEqual(row["matcher_score"], float(max(ab.max(), ba.max())))
            self.assertEqual(row["box_a"], list(items[0][2][views[best][0]]))
            self.assertEqual(row["box_b"], list(items[1][2][views[best][1]]))
        finally:
            for images in full_images:
                for image in images:
                    image.close()


if __name__ == "__main__":
    unittest.main()
