"""Generated fixtures only: no personal images, network, or destructive operations."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import find_duplicates as finder

try:
    from PIL import Image, PngImagePlugin
except ImportError:
    Image = None
    PngImagePlugin = None

SCRIPT = ROOT / "scripts" / "find_duplicates.py"


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png_bytes(red=255, bit_depth=8):
    pixel = bytes((red, 0, 0)) if bit_depth == 8 else struct.pack(">HHH", red, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, bit_depth, 2, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(b"\0" + pixel)) + png_chunk(b"IEND", b""))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="image-duplicate-finder-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.images = self.root / "图片 空格"
        self.images.mkdir()
        self.counter = 0

    def write(self, name, content=None):
        path = self.images / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes() if content is None else content)
        return path

    def options(self, mode="exact", **overrides):
        values = dict(mode=mode, recursive=True, include_hidden=False,
                      extensions=set(finder.DEFAULT_EXTENSIONS), max_pixels=40_000_000,
                      max_frames=200, max_total_pixels=100_000_000)
        values.update(overrides)
        return SimpleNamespace(**values)

    def scan(self, mode="exact", roots=None, **overrides):
        pillow = None if Image is None else (Image, __import__("PIL.ImageOps", fromlist=["ImageOps"]))
        return finder.Scanner(roots or [self.images], self.options(mode, **overrides), pillow).run()

    def cli(self, *arguments, roots=None, output=None, isolated=False):
        self.counter += 1
        output = output or (self.root / f"报告-{self.counter}")
        command = [sys.executable]
        if isolated:
            command += ["-I", "-S"]
        command += [str(SCRIPT)] + [str(path) for path in (roots or [self.images])]
        command += list(arguments) + ["--output-dir", str(output)]
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", timeout=30, env=environment)
        report_path = output / "duplicates.json"
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else None
        return result, report, output


class ExactTests(Fixture):
    def test_recursive_unicode_and_renamed_copies(self):
        first = self.write("原图.png")
        self.write("子目录/副本.JPG")
        self.write("子目录/another.webp")
        self.write("不同图.png", png_bytes(1))
        result, report, _ = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["summary"]["scanned_images"], 4)
        self.assertEqual(report["summary"]["duplicate_groups"], 1)
        self.assertEqual(report["summary"]["extra_copies"], 2)
        self.assertEqual(report["groups"][0]["digest"], hashlib.sha256(first.read_bytes()).hexdigest())
        self.assertTrue(all(Path(item["path"]).is_absolute() for item in report["groups"][0]["files"]))

    def test_same_name_same_size_different_bytes_not_duplicates(self):
        self.write("a/same.png", b"1234")
        self.write("b/same.png", b"4321")
        report = self.scan()
        self.assertEqual(report["groups"], [])
        self.assertEqual(report["summary"]["fingerprinted_images"], 2)

    def test_unique_sizes_skip_hashing(self):
        self.write("a.jpg", b"a")
        self.write("b.jpg", b"bb")
        report = self.scan()
        self.assertEqual(report["summary"]["scanned_images"], 2)
        self.assertEqual(report["summary"]["fingerprinted_images"], 0)

    def test_exact_extension_filter_is_not_decoder_validation(self):
        self.write("a.jpg", b"not actually a jpeg")
        self.write("b.jpg", b"not actually a jpeg")
        report = self.scan()
        self.assertTrue(report["complete"])
        self.assertEqual(len(report["groups"]), 1)

    def test_multiple_overlapping_roots_and_repeated_file(self):
        first = self.write("a.png")
        second = self.write("sub/b.png")
        result, report, _ = self.cli(roots=[self.images / "sub", self.images, self.images, first, second])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["summary"]["scanned_images"], 2)
        self.assertEqual(report["summary"]["extra_copies"], 1)

    def test_individual_file_inputs(self):
        paths = [self.write("a.png"), self.write("b.png")]
        result, report, _ = self.cli(roots=paths)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["summary"]["scanned_images"], 2)

    def test_hidden_files_and_directories_opt_in(self):
        self.write("a.png")
        self.write(".hidden.png")
        self.write(".hidden-dir/b.png")
        normal = self.scan()
        hidden = self.scan(include_hidden=True)
        self.assertEqual(normal["summary"]["scanned_images"], 1)
        self.assertEqual(normal["summary"]["skipped"]["hidden_entries"], 2)
        self.assertEqual(hidden["summary"]["duplicate_images"], 3)

    def test_explicit_hidden_root_and_file_are_respected(self):
        hidden = self.write(".album/.photo.png")
        self.write(".album/normal.png")
        result, report, _ = self.cli(roots=[hidden, hidden.parent])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["summary"]["duplicate_images"], 2)

    def test_no_recursive(self):
        self.write("a.png")
        self.write("sub/b.png")
        result, report, _ = self.cli("--no-recursive")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["summary"]["scanned_images"], 1)
        self.assertFalse(report["options"]["recursive"])

    def test_extension_filter_custom_and_wildcard(self):
        self.write("a.CUSTOM")
        self.write("b.custom")
        self.write("unrelated.txt")
        self.assertEqual(self.scan()["summary"]["scanned_images"], 0)
        result, report, _ = self.cli("--extensions", ".CUSTOM")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["summary"]["duplicate_images"], 2)
        result, report, _ = self.cli("--extensions", "*")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["summary"]["duplicate_images"], 3)

    def test_empty_directory_is_success_with_empty_report(self):
        result, report, _ = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report["complete"])
        self.assertEqual(report["summary"]["scanned_images"], 0)
        self.assertEqual(report["groups"], [])

    def test_exact_mode_works_without_site_packages(self):
        self.write("a.png")
        self.write("b.png")
        result, report, _ = self.cli(isolated=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(report["groups"]), 1)

    def test_pixels_missing_pillow_actionable_error(self):
        result, report, _ = self.cli("--mode", "pixels", isolated=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Pillow", result.stderr)
        self.assertIsNone(report)

    def test_symbolic_files_and_directory_cycles_not_followed(self):
        original = self.write("a.png")
        self.write("b.png")
        try:
            (self.images / "link.png").symlink_to(original)
            (self.images / "cycle").symlink_to(self.images, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(str(exc))
        report = self.scan()
        self.assertEqual(report["summary"]["duplicate_images"], 2)
        self.assertEqual(report["summary"]["skipped"]["symlink_entries"], 2)
        result, report, _ = self.cli(roots=[self.images / "link.png"])
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(report)

    def test_hard_links_listed_and_annotated_not_claimed_as_disk_savings(self):
        original = self.write("a.png")
        try:
            os.link(original, self.images / "b.png")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(str(exc))
        report = self.scan()
        self.assertEqual(report["summary"]["duplicate_images"], 2)
        self.assertEqual(report["summary"]["hard_link_duplicate_paths"], 1)
        self.assertEqual(report["groups"][0]["files"][1]["hard_link_to"], str(original))

    def test_large_file_streaming_across_chunks(self):
        payload = b"x" * (finder.CHUNK_SIZE * 2 + 17)
        self.write("a.png", payload)
        self.write("b.png", payload)
        report = self.scan()
        self.assertEqual(report["groups"][0]["digest"], hashlib.sha256(payload).hexdigest())

    def test_group_ids_and_reference_are_deterministic(self):
        self.write("z.png", b"1")
        self.write("a.png", b"1")
        self.write("b.png", b"22")
        self.write("c.png", b"22")
        one = self.scan()
        two = self.scan()
        self.assertEqual(one["groups"], two["groups"])
        self.assertEqual([g["id"] for g in one["groups"]], ["G0001", "G0002"])
        self.assertTrue(one["groups"][0]["reference"].endswith("a.png"))


class SafetyAndReportTests(Fixture):
    def test_original_bytes_names_and_mtimes_unchanged(self):
        self.write("a.png")
        self.write("sub/b.png")
        before = {str(path.relative_to(self.images)): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.images.rglob("*") if path.is_file()}
        result, _, _ = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = {str(path.relative_to(self.images)): (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in self.images.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_reports_json_csv_markdown_agree(self):
        self.write("a.png")
        self.write("b.png")
        result, report, directory = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["mode"], "exact")
        self.assertEqual(report["algorithm"], "sha256")
        self.assertTrue((directory / "duplicates.csv").read_bytes().startswith(b"\xef\xbb\xbf"))
        with (directory / "duplicates.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["path"] for row in rows}, {item["path"] for item in report["groups"][0]["files"]})
        self.assertIn("G0001", (directory / "duplicates.md").read_text(encoding="utf-8"))
        self.assertEqual(set(path.name for path in directory.iterdir()),
                         {"duplicates.json", "duplicates.csv", "duplicates.md"})

    def test_existing_output_never_overwritten(self):
        output = self.root / "existing"
        output.mkdir()
        sentinel = output / "duplicates.json"
        sentinel.write_text("keep me", encoding="utf-8")
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.images), "--output-dir", str(output)],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me")

    def test_source_file_cannot_be_output(self):
        source = self.write("a.png")
        before = source.read_bytes()
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.images), "--output-dir", str(source)],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(source.read_bytes(), before)

    def test_report_created_inside_input_only_after_scan(self):
        self.write("a.png")
        self.write("b.png")
        result, report, _ = self.cli("--extensions", "*", output=self.images / "new-report")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["summary"]["scanned_images"], 2)

    def test_output_write_failure_exit_two(self):
        blocker = self.root / "not-a-directory"
        blocker.write_bytes(b"keep")
        result, report, _ = self.cli(output=blocker / "report")
        self.assertEqual(result.returncode, 2)
        self.assertIn("报告写入失败", result.stderr)
        self.assertEqual(blocker.read_bytes(), b"keep")
        self.assertIsNone(report)

    def test_missing_path_and_invalid_arguments_exit_two(self):
        result, report, _ = self.cli(roots=[self.root / "missing"])
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(report)
        for argument in [("--max-pixels", "0"), ("--max-frames", "-1"),
                         ("--max-total-pixels", "x"), ("--extensions", "jpg,,png"),
                         ("--extensions", "../png"), ("--mode", "fuzzy")]:
            with self.subTest(argument=argument):
                result, report, _ = self.cli(*argument)
                self.assertEqual(result.returncode, 2)
                self.assertIsNone(report)

    def test_permission_errors_are_reported_not_silently_ignored(self):
        with mock.patch.object(finder.os, "scandir", side_effect=PermissionError("denied")):
            report = self.scan()
        self.assertFalse(report["complete"])
        self.assertEqual(report["errors"][0]["operation"], "scan")
        self.assertEqual(report["summary"]["errors"], 1)

    def test_read_errors_do_not_abort_other_candidates(self):
        self.write("a.png")
        self.write("b.png")
        with mock.patch.object(finder, "checked_stream", side_effect=PermissionError("denied")):
            report = self.scan()
        self.assertFalse(report["complete"])
        self.assertEqual(report["summary"]["errors"], 2)

    def test_file_changed_during_read_rejected(self):
        first = self.write("a.png", b"same")
        self.write("b.png", b"same")
        original = finder.exact_digest
        def mutating_digest(stream):
            result = original(stream)
            first.write_bytes(b"different")
            return result
        with mock.patch.object(finder, "exact_digest", side_effect=mutating_digest):
            report = self.scan()
        self.assertFalse(report["complete"])
        self.assertEqual(report["groups"], [])
        self.assertEqual(report["errors"][0]["error_type"], "ChangedFileError")

    def test_file_changed_after_hash_before_final_verification_rejected(self):
        first = self.write("a.png", b"same")
        self.write("b.png", b"same")
        original = finder.exact_digest
        calls = 0
        def mutating_digest(stream):
            nonlocal calls
            result = original(stream)
            calls += 1
            if calls == 2:
                first.write_bytes(b"changed")
            return result
        with mock.patch.object(finder, "exact_digest", side_effect=mutating_digest):
            report = self.scan()
        self.assertFalse(report["complete"])
        self.assertEqual(report["groups"], [])
        self.assertEqual(report["errors"][0]["operation"], "verify")

    @unittest.skipIf(os.name == "nt", "Windows does not permit these filename characters")
    def test_hostile_filenames_are_safe_and_csv_roundtrips(self):
        first = self.write("<script>\x1b[31m,换行\n.png")
        self.write("copy.png")
        result, report, directory = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\x1b", result.stdout)
        self.assertIn("\\u001b", result.stdout)
        markdown = (directory / "duplicates.md").read_text(encoding="utf-8")
        self.assertNotIn("<script>", markdown)
        self.assertIn("&lt;script&gt;", markdown)
        with (directory / "duplicates.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertIn(str(first), [row["path"] for row in rows])
        self.assertIn(str(first), [item["path"] for item in report["groups"][0]["files"]])

    def test_console_only_does_not_create_reports(self):
        self.write("a.png")
        before = set(self.root.iterdir())
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.images)],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(set(self.root.iterdir()), before)


@unittest.skipUnless(Image is not None, "Install requirements-pixels.txt to run pixel tests")
class PixelTests(Fixture):
    def save(self, name, color="red", size=(3, 2), **kwargs):
        path = self.images / name
        with Image.new("RGB", size, color) as image:
            image.save(path, **kwargs)
        return path

    def animation(self, name, second="blue", duration=100, loop=0, comment=b"a"):
        path = self.images / name
        with Image.new("RGB", (3, 3), "red") as first, Image.new("RGB", (3, 3), second) as last:
            first.save(path, save_all=True, append_images=[last], duration=duration,
                       loop=loop, comment=comment)
        return path

    def test_metadata_and_lossless_encoding_differences(self):
        self.save("a.png")
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Comment", "different metadata, same pixels")
        self.save("b.png", pnginfo=metadata, compress_level=1)
        self.save("c.bmp")
        self.assertEqual(self.scan()["groups"], [])
        result, report, _ = self.cli("--mode", "pixels")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["summary"]["duplicate_images"], 3)
        self.assertEqual(report["algorithm"], "decoded-pixels-sha256-v1")

    def test_single_pixel_difference_not_fuzzy_match(self):
        self.save("a.png")
        self.save("b.png")
        with Image.new("RGB", (3, 2), "red") as image:
            image.putpixel((0, 0), (254, 0, 0))
            image.save(self.images / "different.png")
        report = self.scan("pixels")
        self.assertEqual(report["summary"]["duplicate_images"], 2)
        self.assertTrue(all("different" not in item["path"] for item in report["groups"][0]["files"]))

    def test_dimensions_are_part_of_fingerprint(self):
        self.save("a.png", size=(2, 3))
        self.save("b.png", size=(3, 2))
        self.assertEqual(self.scan("pixels")["groups"], [])

    def test_alpha_channel_is_not_ignored(self):
        for name, alpha in [("a.png", 255), ("b.png", 254)]:
            with Image.new("RGBA", (1, 1), (255, 0, 0, alpha)) as image:
                image.save(self.images / name)
        self.assertEqual(self.scan("pixels")["groups"], [])

    def test_palette_and_rgb_are_normalized(self):
        self.save("rgb.png", size=(1, 1))
        with Image.new("P", (1, 1)) as image:
            image.putpalette([255, 0, 0] + [0] * 765)
            image.save(self.images / "palette.png")
        self.assertEqual(self.scan("pixels")["summary"]["duplicate_images"], 2)

    def test_exif_orientation_is_applied(self):
        with Image.new("RGB", (2, 3)) as image:
            for y in range(3):
                for x in range(2):
                    image.putpixel((x, y), (x * 100, y * 70, 10))
            exif = Image.Exif()
            exif[274] = 6
            image.save(self.images / "oriented.png", exif=exif)
            with image.transpose(Image.Transpose.ROTATE_270) as rotated:
                rotated.save(self.images / "physical-rotation.png")
        report = self.scan("pixels")
        self.assertTrue(report["complete"], report["errors"])
        self.assertEqual(report["summary"]["duplicate_images"], 2)

    def test_later_animation_frames_must_match(self):
        self.animation("a.gif", comment=b"one")
        self.animation("copy.gif", comment=b"two")
        self.animation("different.gif", second="green")
        report = self.scan("pixels")
        self.assertTrue(report["complete"], report["errors"])
        self.assertEqual(report["summary"]["duplicate_images"], 2)
        self.assertTrue(all("different" not in item["path"] for item in report["groups"][0]["files"]))

    def test_animation_duration_must_match(self):
        self.animation("a.gif", duration=100)
        self.animation("b.gif", duration=200)
        self.assertEqual(self.scan("pixels")["groups"], [])

    def test_animation_loop_must_match(self):
        self.animation("a.gif", loop=0)
        self.animation("b.gif", loop=1)
        self.assertEqual(self.scan("pixels")["groups"], [])

    def test_all_tiff_pages_checked(self):
        with Image.new("RGB", (2, 2), "red") as first, Image.new("RGB", (2, 2), "blue") as second:
            first.save(self.images / "a.tif", save_all=True, append_images=[second])
            first.save(self.images / "b.tif", save_all=True, append_images=[second], compression="tiff_lzw")
        with Image.new("RGB", (2, 2), "red") as first, Image.new("RGB", (2, 2), "green") as second:
            first.save(self.images / "different.tif", save_all=True, append_images=[second])
        report = self.scan("pixels")
        self.assertTrue(report["complete"], report["errors"])
        self.assertEqual(report["summary"]["duplicate_images"], 2)

    def test_different_icc_profiles_are_conservatively_distinct(self):
        self.save("a.png", icc_profile=b"profile A")
        self.save("b.png", icc_profile=b"profile B")
        report = self.scan("pixels")
        self.assertTrue(report["complete"], report["errors"])
        self.assertEqual(report["groups"], [])

    def test_high_bit_depth_grayscale_not_reduced_to_8bit(self):
        for name, values in [("a.tif", (1000, 1001)), ("b.tif", (1000, 1002))]:
            with Image.frombytes("I;16", (2, 1), struct.pack("<HH", *values)) as image:
                image.save(self.images / name)
        report = self.scan("pixels")
        self.assertTrue(report["complete"], report["errors"])
        self.assertEqual(report["groups"], [])

    def test_multichannel_16bit_png_rejected_instead_of_lossy_match(self):
        self.write("a.png", png_bytes(1000, bit_depth=16))
        self.write("b.png", png_bytes(1001, bit_depth=16))
        report = self.scan("pixels")
        self.assertFalse(report["complete"])
        self.assertEqual(report["summary"]["errors"], 2)
        self.assertEqual(report["groups"], [])
        self.assertIn("16", report["errors"][0]["message"])

    def test_corrupt_file_partial_report_and_exit_one(self):
        self.save("a.png")
        self.save("b.png")
        self.write("broken.jpg", b"not an image")
        result, report, directory = self.cli("--mode", "pixels")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(report["complete"])
        self.assertEqual(report["summary"]["duplicate_groups"], 1)
        self.assertEqual(report["summary"]["errors"], 1)
        self.assertIn("不完整", (directory / "duplicates.md").read_text(encoding="utf-8"))
        self.assertIn("broken.jpg", result.stderr)

    def test_unsupported_decoder_is_an_error_not_no_duplicates(self):
        self.write("vector.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        report = self.scan("pixels")
        self.assertFalse(report["complete"])
        self.assertEqual(report["summary"]["errors"], 1)

    def test_pixel_frame_and_total_limits(self):
        self.save("still.png", size=(3, 3))
        report = self.scan("pixels", max_pixels=8)
        self.assertFalse(report["complete"])
        self.assertEqual(report["summary"]["errors"], 1)
        self.animation("animation.gif")
        report = self.scan("pixels", max_frames=1)
        self.assertFalse(report["complete"])
        self.assertTrue(any("帧数" in error["message"] for error in report["errors"]))
        report = self.scan("pixels", max_total_pixels=10)
        self.assertFalse(report["complete"])
        self.assertTrue(any("安全上限" in error["message"] for error in report["errors"]))

    def test_multiresolution_ico_is_not_compared_as_single_image(self):
        with Image.new("RGB", (32, 32), "red") as image:
            image.save(self.images / "icon.ico", sizes=[(16, 16), (32, 32)])
        report = self.scan("pixels")
        self.assertFalse(report["complete"])
        self.assertEqual(report["summary"]["errors"], 1)
        self.assertIn("ICO", report["errors"][0]["message"])

    def test_pillow_decompression_bomb_protection_remains_enabled(self):
        self.save("a.png", size=(2, 1))
        with mock.patch.object(Image, "MAX_IMAGE_PIXELS", 1):
            report = self.scan("pixels")
        self.assertFalse(report["complete"])
        self.assertEqual(report["summary"]["errors"], 1)
        self.assertIn("DecompressionBomb", report["errors"][0]["error_type"])


if __name__ == "__main__":
    unittest.main()
