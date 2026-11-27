"""Offline Matcher correctness, safety, fallback and official-code LCE checks."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    import numpy as np
    from PIL import Image
    import torch
    import copynce_matcher as matcher
except ImportError:
    matcher = torch = np = Image = None


@unittest.skipUnless(matcher is not None, "optional PyTorch Matcher runtime is unavailable")
class MatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous_threads = torch.get_num_threads()
        torch.set_num_threads(2)

    @classmethod
    def tearDownClass(cls):
        torch.set_num_threads(cls.previous_threads)

    def bare(self, device="cpu"):
        model = matcher.Matcher.__new__(matcher.Matcher)
        model.device = device
        model.max_batch = 8
        model.fallbacks = []
        model._release_accelerator = mock.Mock()
        model._move = lambda value: setattr(model, "device", str(value))
        return model

    def checkpoint(self, record):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "synthetic.pth"
        payload = b"synthetic trusted checkpoint fixture"
        path.write_bytes(payload)
        context = mock.patch.multiple(matcher, OFFICIAL_BYTES=len(payload),
                                      OFFICIAL_SHA256=hashlib.sha256(payload).hexdigest())
        context.start()
        self.addCleanup(context.stop)
        load = mock.patch.object(matcher.torch, "load", return_value=record)
        mocked = load.start()
        self.addCleanup(load.stop)
        return path, mocked

    def test_checkpoint_hash_verified_before_deserialization(self):
        path, load = self.checkpoint({"model": {}})
        path.write_bytes(b"x" * path.stat().st_size)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            matcher._checkpoint_state(path)
        load.assert_not_called()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs are a POSIX-only input boundary")
    def test_nonregular_checkpoint_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-fifo"
            os.mkfifo(path)
            with mock.patch.object(matcher.torch, "load", side_effect=AssertionError):
                with self.assertRaisesRegex(ValueError, "size mismatch"):
                    matcher._checkpoint_state(path)

    def test_safe_load_strips_only_module_prefix_and_known_metadata(self):
        tensor = torch.tensor([1.0])
        path, load = self.checkpoint({"model": {"module.encoder.example": tensor}, "epoch": 30})
        state, fingerprint = matcher._checkpoint_state(path)
        self.assertEqual(list(state), ["encoder.example"])
        self.assertIs(state["encoder.example"], tensor)
        self.assertEqual(fingerprint, matcher.OFFICIAL_SHA256)
        self.assertTrue(load.call_args.kwargs["weights_only"])
        self.assertEqual(load.call_args.kwargs["map_location"], "cpu")

    def test_unknown_checkpoint_metadata_is_rejected(self):
        path, _ = self.checkpoint({"model": {}, "optimizer": {}})
        with self.assertRaisesRegex(ValueError, "metadata"):
            matcher._checkpoint_state(path)

    def test_duplicate_normalized_state_keys_are_rejected(self):
        path, _ = self.checkpoint({"model": {"module.a": torch.ones(1), "a": torch.zeros(1)}})
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            matcher._checkpoint_state(path)

    def test_missing_and_unknown_architecture_weights_fail_strictly(self):
        for state in ({}, {"unexpected_training_tensor": torch.zeros(1)}):
            with self.subTest(keys=list(state)), mock.patch.object(matcher, "_checkpoint_state", return_value=(state, "fixture")):
                with self.assertRaises(RuntimeError):
                    matcher.Matcher("synthetic.pth", device="cpu")

    def test_classification_head_normalizes_then_applies_one_sigmoid(self):
        head = matcher._MatcherClassificationHead().eval()
        with torch.no_grad():
            head.classifier.weight.fill_(1)
            head.classifier.bias.fill_(float(np.log(3.0)))
        with torch.inference_mode():
            output = head(torch.ones((2, 384)))
        np.testing.assert_allclose(output.numpy(), [.75, .75], rtol=0, atol=1e-6)

    def test_base_matcher_is_symmetric_for_swapped_inputs(self):
        with torch.random.fork_rng(devices=[]):
            torch.manual_seed(7103)
            network = matcher._CopyMatcher().eval()
            left, right = torch.rand((1, 3, 224, 224)), torch.rand((1, 3, 224, 224))
            with torch.inference_mode():
                direct, reverse = network(left, right), network(right, left)
        torch.testing.assert_close(direct, reverse, rtol=1e-5, atol=1e-6)
        self.assertEqual(tuple(direct.shape), (1,))
        self.assertTrue(bool(((direct >= 0) & (direct <= 1)).all()))

    def test_preprocessing_matches_bicubic_rgb_imagenet_pipeline(self):
        model = self.bare()
        model._mean = torch.tensor([.485, .456, .406]).view(1, 3, 1, 1)
        model._std = torch.tensor([.229, .224, .225]).view(1, 3, 1, 1)
        array = np.arange(41 * 63 * 3, dtype=np.uint32).reshape(41, 63, 3).astype(np.uint8)
        with Image.fromarray(array) as original:
            before = original.tobytes()
            actual = model._batch([original]).numpy()
            with original.resize((224, 224), Image.Resampling.BICUBIC) as resized:
                expected = np.asarray(resized).astype(np.float32).transpose(2, 0, 1) / 255.0
            expected = (expected - np.array([.485, .456, .406], dtype=np.float32)[:, None, None]) / np.array([.229, .224, .225], dtype=np.float32)[:, None, None]
            self.assertEqual(before, original.tobytes())
        np.testing.assert_allclose(actual[0], expected, rtol=0, atol=3e-7)
        self.assertEqual(actual.dtype, np.float32)

    def test_bad_inputs_and_mismatched_lengths_are_rejected(self):
        model = self.bare()
        with self.assertRaises(ValueError):
            model.score_pairs([None], [])
        for array in (np.zeros((224, 224, 4), dtype=np.uint8), np.zeros((224, 224, 3), dtype=np.float32)):
            with self.assertRaises(ValueError):
                matcher._rgb_array(array)
        empty = model.score_pairs([], [])
        self.assertEqual(empty.shape, (0,))
        self.assertEqual(empty.dtype, np.float32)

    def test_oom_batch_reduction_keeps_every_pair_in_order(self):
        model = self.bare("cuda")
        attempted = []
        def forward(left, right):
            attempted.append(len(left))
            if len(left) > 2:
                raise RuntimeError("CUDA out of memory")
            return (np.asarray(left, dtype=np.float32) + np.asarray(right, dtype=np.float32)) / 20
        model._forward = forward
        output = model.score_pairs(list(range(8)), list(range(8)))
        np.testing.assert_allclose(output, np.arange(8, dtype=np.float32) / 10)
        self.assertEqual(attempted[:3], [8, 4, 2])
        self.assertEqual(model.device, "cuda")
        self.assertEqual(model.max_batch, 2)

    def test_gpu_single_pair_failure_uses_cpu_without_losing_pairs(self):
        model = self.bare("mps")
        def forward(left, right):
            if model.device != "cpu":
                raise RuntimeError("MPS backend out of memory")
            return np.asarray(left, dtype=np.float32) / 10
        model._forward = forward
        np.testing.assert_allclose(model.score_pairs([1, 2, 3], [4, 5, 6]), [.1, .2, .3])
        self.assertEqual(model.device, "cpu")
        self.assertIn("using CPU", model.fallbacks[-1])

    def test_cpu_error_and_invalid_score_do_not_become_negative_matches(self):
        model = self.bare()
        model._forward = mock.Mock(side_effect=RuntimeError("fixture inference failure"))
        with self.assertRaisesRegex(RuntimeError, "fixture inference"):
            model.score_pairs([1], [2])
        for value in (np.nan, -0.1, 1.1):
            model._forward = lambda left, right: np.full((len(left),), value, dtype=np.float32)
            with self.assertRaisesRegex(ValueError, "invalid classification"):
                model.score_pairs([1], [2])

    def test_fp32_is_preserved_inside_ambient_cpu_autocast(self):
        model = self.bare()
        model._mean = torch.zeros((1, 3, 1, 1))
        model._std = torch.ones((1, 3, 1, 1))
        class TinyMatcher(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(3, 1)
            def forward(self, left, right):
                value = self.linear((left + right).mean(dim=(2, 3)))
                if value.dtype != torch.float32:
                    raise AssertionError("FP32 pipeline changed precision")
                return value.sigmoid().squeeze(-1)
        model._net = TinyMatcher().eval()
        image = np.zeros((224, 224, 3), dtype=np.uint8)
        with torch.autocast(device_type="cpu", dtype=torch.bfloat16):
            result = model.score_pairs([image], [image])
        self.assertEqual(result.dtype, np.float32)

    def test_official_lce_q26_r10_crops_rotations_and_owned_images(self):
        array = np.arange(39 * 61 * 3, dtype=np.uint32).reshape(39, 61, 3).astype(np.uint8)
        with Image.fromarray(array) as image:
            queries = matcher.official_lce_views(image, "query")
            references = matcher.official_lce_views(image, "reference")
            try:
                self.assertEqual((len(queries), len(references)), (26, 10))
                self.assertEqual([v.rotation for v in queries[19:]], list(range(45, 360, 45)))
                self.assertEqual(queries[1].box, queries[14].box)
                for view in queries[:19] + references:
                    with image.crop(view.box) as expected:
                        np.testing.assert_array_equal(np.asarray(view.image), np.asarray(expected))
                with image.rotate(45, expand=True, resample=Image.Resampling.BICUBIC) as expected:
                    np.testing.assert_array_equal(np.asarray(queries[19].image), np.asarray(expected))
            finally:
                for view in (*queries, *references):
                    view.image.close()
            np.testing.assert_array_equal(np.asarray(image), array)

    def test_official_lce_uses_35_star_pairs_and_maximum_score(self):
        pairs = matcher.official_lce_pair_indices()
        self.assertEqual(len(pairs), 35)
        self.assertEqual(len(set(pairs)), 35)
        self.assertTrue(all(i == 0 or j == 0 for i, j in pairs))
        self.assertEqual(pairs[:26], tuple((i, 0) for i in range(26)))
        self.assertEqual(pairs[26:], tuple((0, j) for j in range(1, 10)))
        model = self.bare()
        with Image.new("RGB", (30, 40), "red") as left, Image.new("RGB", (50, 60), "blue") as right:
            model.score_pairs = mock.Mock(return_value=np.linspace(.1, .9, 35, dtype=np.float32))
            result = model.score_pairs_lce([left], [right])
            self.assertEqual(len(model.score_pairs.call_args.args[0]), 35)
            self.assertEqual(len(model.score_pairs.call_args.args[1]), 35)
            np.testing.assert_allclose(result, [.9])
            self.assertEqual(left.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(right.getpixel((0, 0)), (0, 0, 255))

    def test_lce_resized_views_match_native_then_official_preprocessing(self):
        array = np.arange(39 * 61 * 3, dtype=np.uint32).reshape(39, 61, 3).astype(np.uint8)
        with Image.fromarray(array) as image:
            native = matcher.official_lce_views(image)
            compact = matcher.official_lce_views(image, resize_to_224=True)
            try:
                self.assertEqual(len(native), len(compact))
                for a, b in zip(native, compact):
                    self.assertEqual(b.image.size, (224, 224))
                    self.assertEqual((a.box, a.rotation), (b.box, b.rotation))
                    with a.image.resize((224, 224), Image.Resampling.BICUBIC) as expected:
                        np.testing.assert_array_equal(np.asarray(b.image), np.asarray(expected))
            finally:
                for view in (*native, *compact):
                    view.image.close()

    def test_lce_failure_closes_partial_views_and_keeps_original_open(self):
        with Image.new("RGB", (30, 40), "red") as original:
            made = []
            crop = original.crop
            def fail_after_two(box):
                if len(made) == 2:
                    raise RuntimeError("fixture crop failure")
                value = crop(box)
                made.append(value)
                return value
            with mock.patch.object(original, "crop", side_effect=fail_after_two):
                with self.assertRaisesRegex(RuntimeError, "fixture crop failure"):
                    matcher.official_lce_views(original)
            for image in made:
                with self.assertRaises(ValueError):
                    image.getpixel((0, 0))
            self.assertEqual(original.getpixel((0, 0)), (255, 0, 0))


@unittest.skipUnless(matcher is not None, "optional PyTorch Matcher runtime is unavailable")
class TokenCacheTests(unittest.TestCase):
    """Tiny structural modules only: no official model/weights or accelerators."""
    @classmethod
    def setUpClass(cls):
        cls.previous_threads = torch.get_num_threads()
        torch.set_num_threads(2)

    @classmethod
    def tearDownClass(cls):
        torch.set_num_threads(cls.previous_threads)

    def model(self):
        class Encoder(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.register_buffer("scale", torch.tensor(1.))
                self.widths = []
            def forward(self, value):
                self.widths.append(len(value))
                return (value.mean(dim=(2, 3)) * self.scale).unsqueeze(1).expand(-1, 2, -1)
        class Fusion(torch.nn.Module):
            def forward(self, left, right):
                return torch.cat((left, right), dim=1).mean(dim=1)
        class Head(torch.nn.Module):
            def forward(self, value):
                return torch.sigmoid(value.sum(dim=1))
        class Net(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.encoder, self.fusion = Encoder(), Fusion()
                self.heads = torch.nn.ModuleDict({"cls_head": Head()})
            def forward(self, left, right):
                return self.heads["cls_head"](self.fusion(self.encoder(left), self.encoder(right)))
        model = matcher.Matcher.__new__(matcher.Matcher)
        model._net = Net().eval()
        model._mean = torch.tensor([.485, .456, .406]).view(1, 3, 1, 1)
        model._std = torch.tensor([.229, .224, .225]).view(1, 3, 1, 1)
        model.device, model.weights_id, model.max_batch = "cpu", "fixture", 8
        model.fallbacks, model._token_cache = [], None
        model._release_accelerator = mock.Mock()
        return model

    def images(self):
        return ([np.full((224, 224, 3), value, np.uint8) for value in (11, 53, 89)],
                [np.full((224, 224, 3), value, np.uint8) for value in (127, 173, 251)])

    def test_ab_ba_reuses_two_full_batches_and_preserves_bits_and_state(self):
        model = self.model()
        left, right = self.images()
        state_keys = list(model._net.state_dict())
        baseline = (model.score_pairs(left, right), model.score_pairs(right, left))
        self.assertEqual(model._net.encoder.widths, [3, 3, 3, 3])
        model._net.encoder.widths.clear()
        model.enable_token_cache()
        optimized = (model.score_pairs(left, right), model.score_pairs(right, left))
        for a, b in zip(baseline, optimized):
            np.testing.assert_array_equal(a, b)
        self.assertEqual(model._net.encoder.widths, [3, 3])
        self.assertEqual(model.token_cache_info["batch_hits"], 2)
        self.assertEqual(model.token_cache_info["rows_reused"], 6)
        self.assertEqual(list(model._net.state_dict()), state_keys)

    def test_partial_miss_reencodes_original_width_and_mutable_rgb_is_hashed(self):
        model = self.model()
        model.enable_token_cache()
        left, right = self.images()
        model.score_pairs(left, right)
        left[1][0, 0, 0] ^= 1  # Same Python object, different pixels.
        result = model.score_pairs(left, right)
        self.assertEqual(model._net.encoder.widths, [3, 3, 3])
        model.disable_token_cache()
        expected = model.score_pairs(left, right)
        np.testing.assert_array_equal(result, expected)

    def test_slot_and_batch_context_changes_do_not_reuse_wrong_tokens(self):
        model = self.model()
        model.enable_token_cache()
        left, right = self.images()
        model.score_pairs(left, right)
        model.score_pairs(list(reversed(left)), right)
        self.assertEqual(model._net.encoder.widths, [3, 3, 3])
        model.score_pairs(left[:2], right[:2])
        self.assertEqual(model._net.encoder.widths[-2:], [2, 2])
        self.assertEqual(model.token_cache_info["entries"], 4)
        model.max_batch = 4
        model.score_pairs(left[:2], right[:2])
        self.assertEqual(model._net.encoder.widths[-2:], [2, 2])
        self.assertGreaterEqual(model.token_cache_info["clears"], 3)

    def test_byte_bound_uses_independent_row_storage(self):
        model = self.model()
        model.enable_token_cache(max_bytes=48)
        model.score_pairs(*self.images())
        self.assertLessEqual(model.token_cache_info["current_bytes"], 48)
        self.assertGreater(model.token_cache_info["evictions"], 0)
        for value in model._token_cache.values():
            self.assertEqual(value.untyped_storage().nbytes(), value.numel() * value.element_size())

    def test_cpu_storage_preserves_bits(self):
        model = self.model()
        left, right = self.images()
        expected = model.score_pairs(right, left)
        model.enable_token_cache(storage="cpu")
        model.score_pairs(left, right)
        np.testing.assert_array_equal(model.score_pairs(right, left), expected)
        self.assertTrue(all(value.device.type == "cpu" for value in model._token_cache.values()))

    def test_move_disable_and_weight_identity_invalidate(self):
        model = self.model()
        model.enable_token_cache()
        left, right = self.images()
        model.score_pairs(left, right)
        model.weights_id = "another-fixture"
        model.score_pairs(left, right)
        self.assertEqual(model._net.encoder.widths, [3, 3, 3, 3])
        model._move("cpu")
        self.assertEqual(model.token_cache_info["entries"], 0)
        self.assertTrue(model.token_cache_info["enabled"])
        model.disable_token_cache()
        self.assertFalse(model.token_cache_info["enabled"])
        self.assertEqual(model.token_cache_info["max_bytes"], 128 * 1024 * 1024)
        self.assertEqual(model.token_cache_info["storage"], "device")

    def test_inference_error_clears_cache_before_cpu_error_escapes(self):
        model = self.model()
        model.enable_token_cache()
        model.score_pairs(*self.images())
        model._forward = mock.Mock(side_effect=RuntimeError("synthetic failure"))
        with self.assertRaises(RuntimeError):
            model.score_pairs(*self.images())
        self.assertEqual(model.token_cache_info["entries"], 0)
        self.assertEqual(model.token_cache_info["last_clear"], "inference_exception")

    def test_training_mode_never_reuses_tokens(self):
        model = self.model()
        model.enable_token_cache()
        model._net.train()
        left, right = self.images()
        model.score_pairs(left, right)
        model.score_pairs(right, left)
        self.assertEqual(model._net.encoder.widths, [3, 3, 3, 3])
        self.assertEqual(model.token_cache_info["entries"], 0)

    def test_cuda_auto_cache_is_disabled_when_moving_to_cpu(self):
        model = self.model()
        model.enable_token_cache()
        model.score_pairs(*self.images())
        model._token_cache_cuda_auto = True
        model.device = "cuda"  # Fake device state; no accelerator operations.
        model._move("cpu")
        self.assertEqual(model.device, "cpu")
        self.assertFalse(model.token_cache_info["enabled"])
        self.assertFalse(model.token_cache_info["cuda_auto"])
        self.assertEqual(model.token_cache_info["entries"], 0)

    def test_explicit_enable_resets_auto_flag_and_retains_manual_cpu_policy(self):
        model = self.model()
        model.enable_token_cache()
        model._token_cache_cuda_auto = True
        model.enable_token_cache(storage="cpu")
        self.assertFalse(model.token_cache_info["cuda_auto"])
        model.score_pairs(*self.images())
        model._move("cpu")
        self.assertTrue(model.token_cache_info["enabled"])
        self.assertEqual(model.token_cache_info["storage"], "cpu")
        self.assertEqual(model.token_cache_info["entries"], 0)
        model.disable_token_cache()
        self.assertFalse(model.token_cache_info["cuda_auto"])

    def test_cuda_inference_fallback_does_not_enable_cpu_auto_cache(self):
        model = self.model()
        model.enable_token_cache()
        model.score_pairs(*self.images())
        model._token_cache_cuda_auto = True
        model.device = "cuda"
        def forward(left, right):
            if model.device == "cuda":
                raise RuntimeError("synthetic accelerator failure")
            self.assertFalse(model.token_cache_info["enabled"])
            return np.full(len(left), .4, dtype=np.float32)
        model._forward = forward
        result = model.score_pairs(*self.images())
        np.testing.assert_array_equal(result, np.full(3, .4, dtype=np.float32))
        self.assertEqual(model.device, "cpu")
        self.assertFalse(model.token_cache_info["cuda_auto"])
        self.assertEqual(len(model.fallbacks), 1)


if __name__ == "__main__":
    unittest.main()
