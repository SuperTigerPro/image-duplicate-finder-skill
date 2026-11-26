"""Independent math, overlap and private-index checks for official query-only SN."""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    import numpy as np
    from PIL import Image, PngImagePlugin
    import copynce_engine as engine
    import copynce_sn as sn
except ImportError:
    sn = None


class Descriptor:
    dimension = 4
    weights_id = "c" * 64
    device = "cpu"
    fallbacks = ()

    def encode(self, images):
        return np.array([[*np.asarray(image).mean(axis=(0, 1)), 1] for image in images], dtype=np.float32)


@unittest.skipIf(sn is None, "SN needs the optional image dependencies")
class ScoreNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.images = self.root / "background"
        self.images.mkdir()
        for i in range(8):
            Image.new("RGB", (12, 10), (20 + i * 20, 15 + i * 3, 7 + i)).save(self.images / f"background-{i}.png")
        patch = mock.patch.object(engine, "_load_descriptor", return_value=Descriptor())
        patch.start()
        self.addCleanup(patch.stop)

    def build(self, **kwargs):
        return sn.build_index([self.images], Path("fixture"), self.root / "index", **kwargs)

    def index(self):
        self.build()
        return sn.load_index(self.root / "index", expected_weights_sha256=Descriptor.weights_id)

    @staticmethod
    def numeric_index():
        x = np.array([1, .9, .8, .7, .6, 0], dtype=np.float32)
        vectors = np.stack((x, np.sqrt(1 - x * x)), axis=1)
        ids = tuple(hashlib.sha256(str(i).encode()).hexdigest() for i in range(len(x)))
        return sn.AuxiliaryIndex({"images": len(x), "fingerprint": "fixture"}, vectors, ids, ids, ids)

    def test_official_half_open_slice_and_alpha_match_brute_force(self):
        index = self.numeric_index()
        query = np.array([[2, 0], [.2, 1]], dtype=np.float32)
        normalized = query / np.linalg.norm(query, axis=1, keepdims=True)
        expected = np.flip(np.sort(normalized @ index.vectors.T, axis=1), axis=1)[:, 1:5].mean(axis=1) * 1.7
        observed, info = sn.compute_bias(query, index, alpha=1.7, block_size=1, auxiliary_block=2)
        np.testing.assert_allclose(observed, expected, atol=2e-7)
        self.assertAlmostEqual(float(observed[0]), .75 * 1.7, places=6)
        self.assertEqual((info["start"], info["end"]), (1, 5))
        self.assertTrue(info["bias_includes_alpha"])

    def test_query_only_bias_preserves_ranking_but_is_directional(self):
        index = self.numeric_index()
        q = np.eye(2, dtype=np.float32)
        bias = sn.query_bias(q, index)
        raw = q @ index.vectors.T
        shifted = raw - bias[:, None]
        np.testing.assert_array_equal(np.argsort(raw, axis=1), np.argsort(shifted, axis=1))
        self.assertNotAlmostEqual(float(bias[0]), float(bias[1]))
        self.assertNotAlmostEqual(float((q @ q.T)[0, 1] - bias[0]), float((q @ q.T)[1, 0] - bias[1]))

    def test_build_load_and_manifest_tamper_protection(self):
        manifest = self.build(provenance="synthetic independent background")
        index = sn.load_index(self.root / "index", expected_weights_sha256=Descriptor.weights_id)
        self.assertEqual(index.vectors.shape, (8, 4))
        self.assertEqual(index.fingerprint, manifest["fingerprint"])
        self.assertFalse(index.vectors.flags.writeable)
        self.assertNotIn(str(self.images), json.dumps(manifest))
        self.assertEqual(sn.inspect_index(self.root / "index", expected_weights_sha256=Descriptor.weights_id), manifest)
        if os.name != "nt":
            self.assertEqual((self.root / "index/features.npz").stat().st_mode & 0o777, 0o600)
        document = self.root / "index/manifest.json"
        obj = json.loads(document.read_text())
        obj["dimension"] = 99
        document.write_text(json.dumps(obj))
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            sn.load_index(self.root / "index", expected_weights_sha256=Descriptor.weights_id)

    def test_data_checksum_and_model_mismatch_are_rejected(self):
        self.build()
        with self.assertRaisesRegex(ValueError, "model or preprocessing"):
            sn.load_index(self.root / "index", expected_weights_sha256="d" * 64)
        data = self.root / "index/features.npz"
        with data.open("ab") as stream:
            stream.write(b"unreviewed payload")
        with self.assertRaisesRegex(ValueError, "checksum"):
            sn.load_index(self.root / "index", expected_weights_sha256=Descriptor.weights_id)

    def test_lossless_reencoding_metadata_does_not_bypass_overlap_exclusion(self):
        index = self.index()
        original = self.images / "background-0.png"
        rewritten = self.root / "rewritten.png"
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("fixture", "new metadata")
        with Image.open(original) as image:
            image.save(rewritten, pnginfo=metadata)
        record = engine.core.ImageFile(rewritten, rewritten.stat().st_size, engine.core.signature(rewritten.stat()))
        raw = hashlib.sha256(rewritten.read_bytes()).hexdigest()
        self.assertNotIn(raw, index.image_sha256)
        pixel = sn.pixel_sha256(record)
        self.assertIn(pixel, index.pixel_sha256)
        _, info = sn.compute_bias(index.vectors[:1], index, excluded_sha256=[raw], excluded_pixel_sha256=[pixel])
        self.assertEqual((info["background_images_used"], info["overlap_excluded"]), (7, 1))

    def test_source_group_exclusion_and_insufficient_background(self):
        groups = {str(p): "shared" if i < 2 else f"family-{i}"
                  for i, p in enumerate(sorted(self.images.iterdir()))}
        self.build(source_groups=groups)
        index = sn.load_index(self.root / "index", expected_weights_sha256=Descriptor.weights_id)
        shared = hashlib.sha256(b"shared").hexdigest()
        _, info = sn.compute_bias(index.vectors[:1], index, excluded_source_groups=[shared])
        self.assertEqual(info["overlap_excluded"], 2)
        self.assertTrue(info["source_group_exclusion_applied"])
        with self.assertRaisesRegex(ValueError, "disjoint"):
            sn.compute_bias(index.vectors[:1], index, excluded_sha256=index.image_sha256[:4])

    def test_index_cannot_be_built_in_git_and_no_existing_index_is_overwritten(self):
        git = self.root / "repo"
        git.mkdir()
        (git / ".git").mkdir()
        with self.assertRaisesRegex(ValueError, "Git"):
            sn.build_index([self.images], Path("fixture"), git / "index")
        self.build()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.build()

    def test_zero_nonfinite_and_invalid_slice_queries_fail(self):
        index = self.numeric_index()
        for vectors, kwargs in [(np.zeros((1, 2)), {}), (np.array([[math.nan, 1]]), {}),
                                (np.ones((1, 2)), {"start": 5, "end": 5}),
                                (np.ones((1, 2)), {"alpha": math.inf})]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                sn.compute_bias(vectors, index, **kwargs)

    def test_claimed_huge_array_is_rejected_before_numpy_allocation(self):
        self.build()
        data = self.root / "index/features.npz"
        with zipfile.ZipFile(data) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        header = io.BytesIO()
        np.lib.format.write_array_header_1_0(header, dict(descr="<f4", fortran_order=False, shape=(10**12, 4)))
        contents["features.npy"] = header.getvalue()
        with zipfile.ZipFile(data, "w") as archive:
            for name, value in contents.items():
                archive.writestr(name, value)
        path = self.root / "index/manifest.json"
        manifest = json.loads(path.read_text())
        manifest["npz_sha256"] = hashlib.sha256(data.read_bytes()).hexdigest()
        manifest["fingerprint"] = sn._fingerprint(manifest)
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "header"):
            sn.load_index(self.root / "index", expected_weights_sha256=Descriptor.weights_id)


if __name__ == "__main__":
    unittest.main()
