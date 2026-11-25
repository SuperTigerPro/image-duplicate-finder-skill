"""Synthetic static-image similarity calibration, invariants and safety tests."""
import csv
import hashlib
import io
import math
import os
import random
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from test_find_duplicates import Fixture, Image, PngImagePlugin, ROOT, SCRIPT, finder, png_bytes

try:
    import numpy as np
    from scripts import image_similarity as sim
except ImportError:
    np, sim = None, None

if Image is not None:
    from PIL import ImageDraw, ImageEnhance, ImageFilter, ImageOps

AVAILABLE = Image is not None and np is not None


@unittest.skipUnless(AVAILABLE, "Install requirements-similar.txt for similarity tests")
class SimilarityTests(Fixture):
    def scene(self, name="base.png", different=False):
        y, x = np.mgrid[0:256, 0:384]
        if different:
            values = np.stack((128 + 100 * np.sin(y / 11), 120 + 100 * np.cos(x / 8),
                               120 + 90 * np.sin((x - y) / 6)), axis=-1)
        else:
            values = np.stack((70 + 90 * x / 383 + 25 * np.sin(y / 20),
                               50 + 110 * y / 255 + 20 * np.cos(x / 13),
                               100 + 45 * np.sin((x + y) / 30)), axis=-1)
        path = self.images / name
        with Image.fromarray(np.clip(values, 0, 255).astype(np.uint8)) as image:
            draw = ImageDraw.Draw(image)
            if different:
                draw.polygon([(0, 0), (380, 255), (30, 240)], fill=(20, 240, 90))
            else:
                draw.rectangle((25, 25, 140, 105), fill=(230, 180, 60), outline="black", width=3)
                draw.ellipse((180, 60, 310, 180), fill=(40, 150, 210), outline="white", width=4)
                draw.line((20, 230, 350, 200), fill=(240, 230, 220), width=7)
            image.save(path)
        return path

    def variants(self):
        base = self.scene()
        with Image.open(base) as image:
            image.save(self.images / "compressed.jpg", quality=70)
            with image.resize((192, 128), Image.Resampling.LANCZOS) as small:
                small.save(self.images / "resized.png")
            with ImageEnhance.Brightness(image).enhance(1.04) as bright:
                bright.save(self.images / "brighter.png")
            with image.filter(ImageFilter.GaussianBlur(0.6)) as blurred:
                blurred.save(self.images / "slight-blur.png")
        self.scene("unrelated.png", different=True)
        return base

    def feature_list(self):
        scanner = finder.Scanner([self.images], self.options("similar", similarity_threshold=90), (Image, ImageOps))
        for path in sorted(self.images.iterdir()):
            scanner.add_file(path)
        return [sim.extract_feature(record, scanner, finder.checked_stream, finder.pixel_digest)
                for record in scanner.files]

    @staticmethod
    def pair_map(report):
        return {tuple(item["path"] for item in pair["files"]): pair["score"]
                for pair in report["similar_pairs"]}

    def test_default_90_finds_compression_resize_brightness_variants(self):
        base = self.variants()
        report = self.scan("similar")
        self.assertTrue(report["complete"], report["errors"])
        self.assertEqual(report["similarity_threshold"], 90)
        candidates = self.pair_map(report)
        for name in ("compressed.jpg", "resized.png", "brighter.png", "slight-blur.png"):
            key = tuple(sorted((str(base), str(self.images / name))))
            self.assertIn(key, candidates, (name, candidates))
        self.assertTrue(all("unrelated.png" not in path for pair in candidates for path in pair))

    def test_threshold_scores_and_membership_are_monotonic(self):
        self.variants()
        all_pairs = self.pair_map(self.scan("similar", similarity_threshold=0))
        previous = set(all_pairs)
        for threshold in (50, 80, 85, 90, 95, 99, 100):
            with self.subTest(threshold=threshold):
                report = self.scan("similar", similarity_threshold=threshold)
                self.assertTrue(report["complete"], report["errors"])
                current = self.pair_map(report)
                self.assertLessEqual(set(current), previous)
                self.assertEqual(current, {key: score for key, score in all_pairs.items() if score >= threshold})
                self.assertTrue(all(score == all_pairs[key] for key, score in current.items()))
                previous = set(current)

    def test_pair_score_all_101_thresholds_equals_fixed_score(self):
        self.variants()
        features = self.feature_list()
        a = next(f for f in features if f.record.path.name == "base.png")
        b = next(f for f in features if f.record.path.name == "compressed.jpg")
        baseline = sim.pair_score(a, b, 0)[0]
        for threshold in range(101):
            with self.subTest(threshold=threshold):
                result = sim.pair_score(a, b, threshold)
                self.assertEqual(result is not None, baseline >= threshold)
                if result is not None:
                    self.assertAlmostEqual(result[0], baseline)

    def test_zero_includes_all_valid_static_pairs_and_warns(self):
        self.scene()
        self.scene("different.png", different=True)
        self.write("tiny.png")
        result, report, _ = self.cli("--mode", "similar", "--similarity-threshold", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["summary"]["similar_pairs"], 3)
        self.assertEqual(report["summary"]["possible_pairs"], 3)
        self.assertIn("误报", result.stderr)
        self.assertTrue(all(0 <= pair["score"] <= 100 for pair in report["similar_pairs"]))

    def test_100_requires_original_resolution_pixel_hash(self):
        self.write("a.png")
        self.write("b.png")
        with Image.new("RGB", (2, 2), "red") as image:
            image.save(self.images / "same-thumbnail-but-larger.png")
        broad = self.scan("similar", similarity_threshold=90)
        self.assertEqual(broad["summary"]["similar_pairs"], 3)
        self.assertTrue(any(pair["score"] == 99.9 for pair in broad["similar_pairs"]))
        strict = self.scan("similar", similarity_threshold=100)
        self.assertEqual(strict["summary"]["similar_pairs"], 1)
        self.assertTrue(strict["similar_pairs"][0]["pixel_identical"])
        self.assertEqual(strict["similar_pairs"][0]["score"], 100)

    def test_one_pixel_change_cannot_round_up_to_100(self):
        base = self.scene()
        with Image.open(base) as image:
            image.putpixel((10, 10), (1, 2, 3))
            image.save(self.images / "changed.png")
        report = self.scan("similar", similarity_threshold=90)
        self.assertEqual(len(report["similar_pairs"]), 1)
        self.assertLess(report["similar_pairs"][0]["score"], 100)
        self.assertFalse(report["similar_pairs"][0]["pixel_identical"])
        self.assertEqual(self.scan("similar", similarity_threshold=100)["similar_pairs"], [])

    def test_100_matches_pixels_mode_across_metadata_and_lossless_format(self):
        base = self.scene()
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Comment", "metadata only")
        with Image.open(base) as image:
            image.save(self.images / "metadata.png", pnginfo=metadata)
            image.save(self.images / "lossless.bmp")
        strict = self.scan("pixels")
        report = self.scan("similar", similarity_threshold=100)
        self.assertEqual(len(strict["groups"][0]["files"]), 3)
        self.assertEqual(report["summary"]["pixel_identical_pairs"], 3)
        self.assertTrue(all(pair["score"] == 100 for pair in report["similar_pairs"]))

    def test_exif_direction_normalization(self):
        with Image.new("RGB", (2, 3)) as image:
            for y in range(3):
                for x in range(2):
                    image.putpixel((x, y), (x * 100, y * 70, 10))
            exif = Image.Exif()
            exif[274] = 6
            image.save(self.images / "exif.png", exif=exif)
            with image.transpose(Image.Transpose.ROTATE_270) as rotated:
                rotated.save(self.images / "physically-rotated.png")
        report = self.scan("similar", similarity_threshold=100)
        self.assertTrue(report["complete"], report["errors"])
        self.assertEqual(report["summary"]["pixel_identical_pairs"], 1)

    def test_color_prevents_same_luminance_false_positive(self):
        for name, color in (("red.png", (255, 0, 0)), ("green.png", (0, 129, 0))):
            with Image.new("RGB", (64, 64), color) as image:
                image.save(self.images / name)
        report = self.scan("similar", similarity_threshold=90)
        self.assertEqual(report["similar_pairs"], [])
        low = self.scan("similar", similarity_threshold=0)["similar_pairs"][0]
        self.assertEqual(low["metrics"]["phash_distance"], 0)
        self.assertLess(low["metrics"]["color_score"], 60)

    def test_alpha_channel_not_lost_on_white_background(self):
        for name, alpha in (("clear.png", 0), ("opaque.png", 255)):
            with Image.new("RGBA", (64, 64), (255, 255, 255, alpha)) as image:
                image.save(self.images / name)
        self.assertEqual(self.scan("similar")["similar_pairs"], [])
        pair = self.scan("similar", similarity_threshold=0)["similar_pairs"][0]
        self.assertEqual(pair["metrics"]["alpha_score"], 0)
        self.assertEqual(pair["score"], 0)

    def test_icc_difference_has_fixed_conservative_cap(self):
        for name, profile in (("a.png", b"A"), ("b.png", b"B")):
            with Image.new("RGB", (64, 64), "red") as image:
                image.save(self.images / name, icc_profile=profile)
        self.assertEqual(self.scan("similar")["similar_pairs"], [])
        pair = self.scan("similar", similarity_threshold=80)["similar_pairs"][0]
        self.assertEqual(pair["score"], 89.9)
        self.assertFalse(pair["metrics"]["icc_equal"])
        self.assertTrue(pair["warnings"])

    def test_low_information_image_is_not_confidently_claimed_same(self):
        self.write("a.png")
        with Image.new("RGB", (1, 1), (254, 0, 0)) as image:
            image.save(self.images / "b.png")
        pair = self.scan("similar")["similar_pairs"][0]
        self.assertFalse(pair["pixel_identical"])
        self.assertTrue(any("低纹理" in note for note in pair["warnings"]))

    def test_low_contrast_noise_is_marked_for_review(self):
        rng = np.random.default_rng(19)
        values = rng.integers(125, 130, (128, 128, 3), dtype=np.uint8)
        with Image.fromarray(values) as image:
            image.save(self.images / "low-contrast.png")
        self.assertTrue(self.feature_list()[0].low_information)

    def test_aspect_ratio_gate_is_part_of_fixed_score(self):
        self.scene()
        feature = self.feature_list()[0]
        changed = replace(feature, pixels="different-full-image", aspect=feature.aspect * 2)
        result = sim.pair_score(feature, changed, 0)
        self.assertIsNotNone(result)
        self.assertLessEqual(result[0], 25)
        self.assertIsNone(sim.pair_score(feature, changed, 90))

    def test_rejects_animation_and_all_tiff_pages_not_just_first(self):
        with Image.new("RGB", (4, 4), "red") as first, Image.new("RGB", (4, 4), "blue") as last:
            for name in ("animated.gif", "multi.tif"):
                first.save(self.images / name, save_all=True, append_images=[last])
        result, report, _ = self.cli("--mode", "similar")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(report["complete"])
        self.assertEqual(report["summary"]["errors"], 2)
        self.assertTrue(all("静态" in error["message"] for error in report["errors"]))

    def test_corrupt_images_keep_partial_pairs_and_exit_one(self):
        self.write("a.png")
        self.write("b.png")
        self.write("corrupt.jpg", b"invalid")
        result, report, _ = self.cli("--mode", "similar")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["summary"]["similar_pairs"], 1)
        self.assertFalse(report["complete"])
        self.assertIn("corrupt.jpg", result.stderr)

    def test_high_bit_depth_png_is_not_lossily_compared(self):
        self.write("a.png", png_bytes(1000, bit_depth=16))
        self.write("b.png", png_bytes(1001, bit_depth=16))
        report = self.scan("similar")
        self.assertFalse(report["complete"])
        self.assertEqual(report["summary"]["errors"], 2)
        self.assertEqual(report["similar_pairs"], [])

    def test_scientific_integer_mode_explicitly_rejected(self):
        with Image.frombytes("I;16", (2, 1), b"\x01\x00\x02\x00") as image:
            image.save(self.images / "scientific.tif")
        report = self.scan("similar")
        self.assertFalse(report["complete"])
        self.assertIn("高位深", report["errors"][0]["message"])

    def test_original_images_bytes_and_mtime_unchanged(self):
        self.variants()
        before = {str(p): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                  for p in self.images.iterdir()}
        result, report, _ = self.cli("--mode", "similar")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = {str(p): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                 for p in self.images.iterdir()}
        self.assertEqual(before, after)
        self.assertGreater(report["summary"]["similar_pairs"], 0)

    def test_changed_result_file_is_removed_at_final_verification(self):
        first = self.write("a.png")
        self.write("b.png")
        original = sim.pair_score
        def mutate(a, b, threshold):
            result = original(a, b, threshold)
            first.write_bytes(b"changed during comparison")
            return result
        with mock.patch.object(sim, "pair_score", side_effect=mutate):
            report = self.scan("similar")
        self.assertFalse(report["complete"])
        self.assertEqual(report["similar_pairs"], [])
        self.assertEqual(report["errors"][0]["operation"], "verify")

    def test_result_limit_is_explicit_partial_not_silent_success(self):
        for name in ("a.png", "b.png", "c.png"):
            self.write(name)
        with mock.patch.object(sim, "MAX_MATCHED_PAIRS", 1):
            report = self.scan("similar")
        self.assertFalse(report["complete"])
        self.assertEqual(report["summary"]["similar_pairs"], 1)
        self.assertTrue(report["summary"]["safety_limit_reached"])
        self.assertEqual(report["errors"][0]["error_type"], "SimilarityLimitError")

    def test_candidate_limit_is_explicit(self):
        self.write("a.png")
        self.write("b.png")
        with mock.patch.object(sim, "MAX_CANDIDATE_PAIRS", 0):
            report = self.scan("similar")
        self.assertFalse(report["complete"])
        self.assertTrue(report["summary"]["safety_limit_reached"])
        self.assertEqual(report["similar_pairs"], [])

    def test_image_limit_is_explicit(self):
        self.write("a.png")
        self.write("b.png")
        with mock.patch.object(sim, "MAX_FEATURE_IMAGES", 1):
            report = self.scan("similar")
        self.assertFalse(report["complete"])
        self.assertEqual(report["summary"]["fingerprinted_images"], 1)
        self.assertTrue(report["summary"]["safety_limit_reached"])

    def test_json_csv_markdown_and_preview_agree(self):
        self.variants()
        result, report, directory = self.cli("--mode", "similar")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["algorithm"], sim.ALGORITHM)
        self.assertEqual(report["groups"], [])
        self.assertEqual(report["summary"]["extra_copies"], 0)
        with (directory / "duplicates.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), len(report["similar_pairs"]))
        self.assertEqual({r["pair_id"] for r in rows}, {p["id"] for p in report["similar_pairs"]})
        self.assertTrue(all(float(r["similarity_score"]) >= 90 for r in rows))
        markdown = (directory / "duplicates.md").read_text(encoding="utf-8")
        self.assertIn("相似度门槛", markdown)
        self.assertIn("similarity-preview.png", markdown)
        with Image.open(directory / "similarity-preview.png") as preview:
            self.assertEqual(preview.format, "PNG")
            self.assertGreater(preview.height, 128)

    def test_empty_input_has_no_preview_and_is_success(self):
        result, report, directory = self.cli("--mode", "similar")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report["complete"])
        self.assertIsNone(report["preview"])
        self.assertFalse((directory / "similarity-preview.png").exists())

    def test_pair_ids_stable_no_transitive_grouping(self):
        self.variants()
        one = self.scan("similar")
        two = self.scan("similar")
        self.assertEqual(one["similar_pairs"], two["similar_pairs"])
        self.assertEqual(one["groups"], [])
        self.assertTrue(all(len(pair["files"]) == 2 for pair in one["similar_pairs"]))

    def test_hidden_and_nonrecursive_scope_respected(self):
        self.write("a.png")
        self.write(".hidden.png")
        self.write("sub/b.png")
        result, report, _ = self.cli("--mode", "similar", "--no-recursive")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["summary"]["scanned_images"], 1)
        result, report, _ = self.cli("--mode", "similar", "--include-hidden")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["summary"]["similar_pairs"], 3)

    def test_invalid_thresholds_and_wrong_modes_rejected(self):
        for arguments in (("--mode", "similar", "--similarity-threshold", "-1"),
                          ("--mode", "similar", "--similarity-threshold", "101"),
                          ("--mode", "similar", "--similarity-threshold", "90.5"),
                          ("--mode", "similar", "--similarity-threshold", "nan"),
                          ("--mode", "exact", "--similarity-threshold", "90")):
            with self.subTest(arguments=arguments):
                result, report, _ = self.cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIsNone(report)

    def test_help_has_one_similarity_tuning_option_no_low_level_knobs(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, timeout=30)
        help_text = result.stdout.decode("utf-8")
        self.assertIn("--similarity-threshold", help_text)
        for hidden in ("--phash", "--dhash", "--ssim", "--color-weight", "--aspect-tolerance"):
            self.assertNotIn(hidden, help_text)

    def test_missing_optional_dependency_has_clear_error(self):
        with mock.patch.object(finder, "similarity_module", side_effect=ImportError("numpy")):
            with mock.patch.object(sys, "stderr", io.StringIO()) as output:
                with self.assertRaises(SystemExit) as error:
                    finder.main([str(self.images), "--mode", "similar"])
                self.assertEqual(error.exception.code, 2)
                self.assertIn("requirements-similar.txt", output.getvalue())

    def test_bktree_radius_queries_equal_bruteforce(self):
        rng = random.Random(31)
        hashes = [rng.getrandbits(64) for _ in range(60)] + [0, 0, 7, 31]
        tree = sim.BKTree()
        for index, value in enumerate(hashes):
            tree.add(value, index)
        for query in (0, 7, hashes[0], rng.getrandbits(64)):
            for radius in (0, 1, 6, 12, 32, 64):
                with self.subTest(query=query, radius=radius):
                    expected = {i for i, value in enumerate(hashes) if (query ^ value).bit_count() <= radius}
                    found = list(tree.query(query, radius))
                    self.assertEqual(set(found), expected)
                    self.assertEqual(len(found), len(set(found)))

    def test_similarity_chain_does_not_create_unchecked_pair(self):
        for level in (100, 110, 120):
            with Image.new("RGB", (32, 32), (level, level, level)) as image:
                image.save(self.images / f"{level}.png")
        report = self.scan("similar", similarity_threshold=95)
        names = {tuple(Path(item["path"]).name for item in pair["files"]) for pair in report["similar_pairs"]}
        self.assertEqual(names, {("100.png", "110.png"), ("110.png", "120.png")})
        self.assertEqual(report["groups"], [])

    def test_preview_is_bounded_but_complete_pair_report_is_not(self):
        for number in range(6):
            self.write(f"{number}.png")
        result, report, directory = self.cli("--mode", "similar")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["summary"]["similar_pairs"], 15)
        self.assertEqual(report["preview"]["pairs_shown"], 12)
        with Image.open(directory / "similarity-preview.png") as preview:
            self.assertEqual(preview.height, 30 + 12 * 158)

    def test_hard_links_annotated_without_reclaimable_bytes_claim(self):
        first = self.write("a.png")
        try:
            os.link(first, self.images / "b.png")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(str(exc))
        report = self.scan("similar", similarity_threshold=100)
        self.assertTrue(report["similar_pairs"][0]["same_inode"])
        self.assertEqual(report["summary"]["extra_copy_logical_bytes"], 0)

    def test_ssim_matches_direct_single_window_formula(self):
        left = np.arange(49, dtype=np.uint8).reshape(7, 7)
        right = np.flip(left, axis=1).copy()
        a, b = left.astype(float) / 255, right.astype(float) / 255
        ma, mb = a.mean(), b.mean()
        va, vb = a.var(ddof=1), b.var(ddof=1)
        cov = np.sum((a - ma) * (b - mb)) / 48
        expected = ((2 * ma * mb + 0.01 ** 2) * (2 * cov + 0.03 ** 2)
                    / ((ma * ma + mb * mb + 0.01 ** 2) * (va + vb + 0.03 ** 2)))
        self.assertAlmostEqual(sim.structural_similarity(left, right), expected, places=10)

    def test_ssim_identity_symmetry_and_bounds(self):
        rng = np.random.default_rng(11)
        left = rng.integers(0, 256, (128, 128), dtype=np.uint8)
        right = rng.integers(0, 256, (128, 128), dtype=np.uint8)
        self.assertAlmostEqual(sim.structural_similarity(left, left), 1)
        self.assertAlmostEqual(sim.structural_similarity(left, right), sim.structural_similarity(right, left))
        self.assertGreaterEqual(sim.structural_similarity(left, right), -1)
        self.assertLessEqual(sim.structural_similarity(left, right), 1)
        self.assertLess(sim.structural_similarity(left, right), 0.1)

    def test_derived_gates_tighten_and_match_recommended_value(self):
        previous = sim.threshold_settings(0)
        for threshold in range(1, 101):
            current = sim.threshold_settings(threshold)
            self.assertLessEqual(current["phash_max_distance"], previous["phash_max_distance"])
            self.assertLessEqual(current["dhash_max_distance"], previous["dhash_max_distance"])
            self.assertGreaterEqual(current["ssim_min"], previous["ssim_min"])
            self.assertGreaterEqual(current["aspect_ratio_min"], previous["aspect_ratio_min"])
            previous = current
        recommended = sim.threshold_settings(90)
        self.assertEqual(recommended["phash_max_distance"], 6)
        self.assertEqual(recommended["dhash_max_distance"], 8)
        self.assertAlmostEqual(recommended["ssim_min"], 0.95)

    def test_constant_hash_is_stable_across_brightness(self):
        hashes = []
        for level in (0, 30, 127, 255):
            with Image.new("L", (128, 128), level) as image:
                hashes.append((sim.perceptual_hash(image), sim.difference_hash(image)))
        self.assertEqual(len(set(hashes)), 1)


if __name__ == "__main__":
    unittest.main()
