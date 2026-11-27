"""Exercise file identity, crop matching and cache semantics without downloading weights."""
import os
import hashlib
import sys
import tempfile
import threading
import time
import unittest
import warnings
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

@unittest.skipIf(engine is None, "Pixel digest cache tests require Pillow and NumPy")
class CanonicalPixelCacheTests(unittest.TestCase):
    def setUp(self):
        import copynce_sn as sn
        self.sn = sn
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "source.png"
        Image.new("RGB", (24, 17), (53, 97, 151)).save(self.path)
        self.location = self.root / "features.sqlite3"
        self.conn = engine._open_cache(self.location)
        self.addCleanup(self.conn.close)

    def scan(self, *, max_pixels=40000000, pipeline="fixture-pixel-cache", collect_pixels=True):
        info = self.path.stat()
        record = engine.core.ImageFile(self.path, info.st_size, engine.core.signature(info))
        self.errors, self.pixels, self.raw = [], {} if collect_pixels else None, {}
        with mock.patch.object(self.sn, "pixel_sha256", wraps=self.sn.pixel_sha256) as pixel_hash, \
             mock.patch.object(self.sn, "_rgb_pixel_sha256", wraps=self.sn._rgb_pixel_sha256) as rgb_hash:
            self.items, self.hits = engine._features([record], ColorDescriptor(), self.conn, pipeline, True, 8,
                max_pixels, lambda path, message: self.errors.append(message),
                content_hashes=self.raw, pixel_hashes=self.pixels)
        self.full_pixel_calls = pixel_hash.call_count
        return rgb_hash.call_count

    def test_warm_hit_still_hashes_source_without_full_rgb_decode(self):
        self.assertEqual(self.scan(), 1)
        expected = dict(self.pixels)
        with mock.patch.object(Image.Image, "getexif", side_effect=AssertionError("No full PNG EXIF load on hit")), \
             mock.patch.object(Image.Image, "load", side_effect=AssertionError("No RGB decode on hit")), \
             mock.patch.object(engine.core, "exact_digest", wraps=engine.core.exact_digest) as raw_hash:
            self.assertEqual(self.scan(), 0)
        self.assertEqual(raw_hash.call_count, 1)
        self.assertEqual(len(self.hits), 1)
        self.assertEqual(self.pixels, expected)
        self.assertEqual(self.raw[str(self.path)], hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_legacy_descriptor_cache_backfills_missing_digest_table(self):
        self.scan()
        self.conn.execute("DROP TABLE canonical_pixel_digests_v1")
        self.conn.commit()
        self.assertEqual(self.scan(), 1)
        self.assertEqual(len(self.hits), 1)
        self.assertEqual(self.scan(), 0)

    def test_changed_bytes_recalculate_pixels_and_descriptor(self):
        self.scan()
        previous = dict(self.pixels)
        Image.new("RGB", (24, 17), "green").save(self.path)
        self.assertEqual(self.scan(), 1)
        self.assertEqual(self.hits, set())
        self.assertNotEqual(self.pixels, previous)

    def test_corrupted_valid_hex_digest_is_recalculated(self):
        self.scan()
        expected = dict(self.pixels)
        self.conn.execute("UPDATE canonical_pixel_digests_v1 SET pixel_sha256=?", ("0" * 64,))
        self.conn.commit()
        self.assertEqual(self.scan(), 1)
        self.assertEqual(self.pixels, expected)
        self.assertEqual(self.scan(), 0)

    def test_descriptor_geometry_is_independently_revalidated(self):
        self.scan()
        self.conn.execute("UPDATE descriptors SET width=17,height=24")
        self.conn.commit()
        self.assertEqual(self.scan(), 1)
        self.assertEqual(self.hits, set())
        self.assertEqual(self.items[0][1], (24, 17))

    def test_new_pixel_limit_and_format_checks_cannot_be_bypassed(self):
        self.scan()
        self.assertEqual(self.scan(max_pixels=407), 0)
        self.assertEqual(self.items, [])
        self.assertEqual(len(self.errors), 1)
        with mock.patch.object(engine.core, "NORMALIZE_MODES", frozenset()):
            self.assertEqual(self.scan(), 0)
            self.assertEqual(self.items, [])
            self.assertEqual(len(self.errors), 1)

    def test_current_pillow_bomb_guard_is_rechecked(self):
        self.scan()
        with mock.patch.object(Image, "MAX_IMAGE_PIXELS", 1):
            self.assertEqual(self.scan(), 0)
            self.assertEqual(self.items, [])
            self.assertEqual(len(self.errors), 1)

    def test_policy_change_invalidates_pixel_proof(self):
        self.scan()
        with mock.patch.object(engine, "_pixel_policy", return_value="updated-pixel-policy"):
            self.assertEqual(self.scan(), 1)
            self.assertEqual(self.scan(), 0)

    def test_decoder_truncation_policy_change_does_not_reuse_old_proof(self):
        self.scan()
        with mock.patch.object(engine.PillowImageFile, "LOAD_TRUNCATED_IMAGES", True):
            self.assertEqual(self.scan(), 1)
            self.assertEqual(self.scan(), 0)

    def test_invalid_descriptor_does_not_allow_digest_only_reuse(self):
        self.scan()
        self.conn.execute("UPDATE descriptors SET features=?", (b"invalid",))
        self.conn.commit()
        self.assertEqual(self.scan(), 1)
        self.assertEqual(self.hits, set())

    def test_bad_descriptor_geometry_or_blob_types_are_cache_misses(self):
        self.scan()
        for column, value in (("width", "invalid"), ("height", "invalid"), ("features", "invalid")):
            with self.subTest(column=column):
                self.conn.execute(f"UPDATE descriptors SET {column}=?", (value,))
                self.conn.commit()
                self.assertEqual(self.scan(), 1)
                self.assertEqual(self.hits, set())
                self.assertEqual(self.errors, [])

    def test_malformed_optional_table_falls_back_to_original_hash(self):
        self.scan()
        self.conn.execute("DROP TABLE canonical_pixel_digests_v1")
        self.conn.execute("CREATE TABLE canonical_pixel_digests_v1 (unexpected TEXT)")
        self.conn.commit()
        self.assertEqual(self.scan(), 1)
        self.assertEqual(self.scan(), 1)
        self.assertEqual(self.errors, [])

    def test_oriented_dimensions_are_part_of_the_proof(self):
        self.path = self.root / "oriented.jpg"
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (31, 19), (23, 57, 99)).save(self.path, exif=exif)
        self.assertEqual(self.scan(), 1)
        self.assertEqual(self.items[0][1], (19, 31))
        self.assertEqual(self.scan(), 0)
        self.assertEqual(self.items[0][1], (19, 31))

    def test_fast_features_do_not_create_a_pixel_digest_table(self):
        self.assertEqual(self.scan(collect_pixels=False), 0)
        tables = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("canonical_pixel_digests_v1", tables)

    def test_cold_digest_is_fused_and_legacy_backfill_keeps_original_validation(self):
        self.assertEqual(self.scan(), 1)
        self.assertEqual(self.full_pixel_calls, 0)
        self.conn.execute("DROP TABLE canonical_pixel_digests_v1")
        self.conn.commit()
        self.assertEqual(self.scan(), 1)
        self.assertEqual(self.full_pixel_calls, 1)
        self.assertEqual(self.scan(), 0)
        self.assertEqual(self.full_pixel_calls, 0)

    def test_fused_digest_and_selected_views_match_original_across_formats(self):
        for suffix, mode, orientation in (("png", "RGBA", None), ("jpg", "RGB", 6), ("jpg", "RGB", 8)):
            with self.subTest(suffix=suffix, orientation=orientation):
                path = self.root / f"fused-{mode}-{orientation}.{suffix}"
                image = Image.new(mode, (37, 257), (41, 73, 101, 127) if mode == "RGBA" else (41, 73, 101))
                options = {}
                if orientation:
                    exif = Image.Exif()
                    exif[274] = orientation
                    options["exif"] = exif
                image.save(path, **options)
                image.close()
                info = path.stat()
                record = engine.core.ImageFile(path, info.st_size, engine.core.signature(info))
                expected_digest = self.sn.pixel_sha256(record)
                size, boxes, all_views = engine._decode(record, True, 40000000)
                wanted = [len(boxes) - 1, 0, 3]
                metadata = {}
                with mock.patch.object(engine.ImageOps, "exif_transpose", wraps=engine.ImageOps.exif_transpose) as transpose:
                    actual_size, actual_boxes, selected = engine._decode(record, True, 40000000,
                                                                         view_indices=wanted, pixel_info=metadata)
                try:
                    self.assertEqual(transpose.call_count, 1)
                    self.assertEqual((actual_size, actual_boxes), (size, boxes))
                    self.assertEqual(metadata, {"raw_size": (37, 257), "oriented_size": size,
                                                "pixel_sha256": expected_digest})
                    for index, view in zip(wanted, selected):
                        np.testing.assert_array_equal(np.asarray(view), np.asarray(all_views[index]))
                finally:
                    for view in (*all_views, *selected):
                        view.close()

    def test_fused_metadata_is_not_published_for_failed_decode(self):
        info = self.path.stat()
        record = engine.core.ImageFile(self.path, info.st_size, engine.core.signature(info))
        metadata = {"keep": True}
        with self.assertRaises(ValueError):
            engine._decode(record, True, 40000000, view_indices=[999], pixel_info=metadata)
        self.assertEqual(metadata, {"keep": True})
        with self.assertRaises(ValueError):
            engine._decode(record, True, 40000000, pixel_info="not a dictionary")

    def test_fusion_keeps_descriptor_file_order_and_8_8_6_batches(self):
        paths = [self.path, self.root / "second.png"]
        Image.new("RGB", (24, 17), "green").save(paths[1])
        records = [engine.core.ImageFile(path, path.stat().st_size, engine.core.signature(path.stat())) for path in paths]
        batches = []
        class Descriptor(ColorDescriptor):
            def encode(self, images):
                batches.append(len(images))
                return super().encode(images)
        items, _ = engine._features(records, Descriptor(), None, "fusion-fixture", True, 8, 40000000,
                                   lambda *args: self.fail("Unexpected image error"), pixel_hashes={})
        self.assertEqual(batches, [8, 8, 6, 8, 8, 6])
        self.assertEqual([item[0] for item in items], sorted(map(str, paths)))


@unittest.skipIf(engine is None, "Ordered preparation requires Pillow and NumPy")
class OrderedPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def records(self, count=3, size=(24, 17)):
        result = []
        for i in range(count):
            path = self.root / f"{i:02}.png"
            array = np.random.default_rng(i).integers(0, 256, (size[1], size[0], 3), dtype=np.uint8)
            with Image.fromarray(array) as image:
                image.save(path)
            info = path.stat()
            result.append(engine.core.ImageFile(path, info.st_size, engine.core.signature(info)))
        return result

    def features(self, records, *, model=None, cache=None, max_pixels=40000000, raw=None, pixels=None):
        errors = []
        items, hits = engine._features(records, model or ColorDescriptor(), cache, "ordered-preparation", True, 8,
            max_pixels, lambda path, message: errors.append((str(path), message)),
            content_hashes=raw, pixel_hashes=pixels)
        return items, hits, errors

    def assert_closed(self, images):
        self.assertTrue(images)
        for image in images:
            with self.assertRaises(ValueError):
                image.getpixel((0, 0))

    def test_out_of_order_cpu_completion_preserves_every_batch_and_feature_bit(self):
        records = self.records()
        observed = []
        class Descriptor(ColorDescriptor):
            def encode(self, images):
                observed.append([hashlib.sha256(np.asarray(image).tobytes()).hexdigest() for image in images])
                return super().encode(images)
        with mock.patch.object(engine, "DECODE_PREFETCH", 1):
            serial, _, _ = self.features(records, model=Descriptor(), pixels={})
        serial_batches = list(observed)
        observed.clear()
        second_finished = threading.Event()
        completion = []
        original = engine._decode
        def prepare(record, *args, **kwargs):
            if record == records[0] and not second_finished.wait(3):
                raise AssertionError("Second preparation did not run concurrently")
            result = original(record, *args, **kwargs)
            completion.append(record.path.name)
            if record == records[1]:
                second_finished.set()
            return result
        with mock.patch.object(engine, "_decode", side_effect=prepare):
            parallel, _, errors = self.features(records, model=Descriptor(), pixels={})
        self.assertEqual(errors, [])
        self.assertEqual(completion[0], records[1].path.name)
        self.assertEqual(observed, serial_batches)
        self.assertEqual([len(batch) for batch in observed], [8, 8, 6] * 3)
        for a, b in zip(serial, parallel):
            self.assertEqual(a[:3], b[:3])
            np.testing.assert_array_equal(a[3], b[3])

    def test_one_bad_file_does_not_reorder_other_records(self):
        records = self.records()
        records[1].path.write_bytes(b"invalid image")
        info = records[1].path.stat()
        records[1] = engine.core.ImageFile(records[1].path, info.st_size, engine.core.signature(info))
        raw, pixels = {}, {}
        items, _, errors = self.features(records, raw=raw, pixels=pixels)
        self.assertEqual([item[0] for item in items], [str(records[0].path), str(records[2].path)])
        self.assertEqual([error[0] for error in errors], [str(records[1].path)])
        self.assertEqual(set(raw), {str(record.path) for record in records})
        self.assertEqual(set(pixels), {str(records[i].path) for i in (0, 2)})

    def test_worker_decode_error_is_reported_once_and_other_images_finish(self):
        records = self.records()
        captured = []
        original = engine._decode
        def prepare(record, *args, **kwargs):
            if record == records[1]:
                raise OSError("synthetic decode failure")
            result = original(record, *args, **kwargs)
            captured.extend(result[2])
            return result
        with mock.patch.object(engine, "_decode", side_effect=prepare):
            items, _, errors = self.features(records, pixels={})
        self.assertEqual([item[0] for item in items], [str(records[i].path) for i in (0, 2)])
        self.assertEqual(errors, [(str(records[1].path), "synthetic decode failure")])
        self.assert_closed(captured)

    def test_all_cache_hits_start_no_decoder_or_executor(self):
        records = self.records()
        conn = engine._open_cache(self.root / "cache.sqlite3")
        self.addCleanup(conn.close)
        self.features(records, cache=conn, pixels={})
        with mock.patch.object(engine, "_decode", side_effect=AssertionError("No decode for cache hits")), \
             mock.patch.object(engine, "ThreadPoolExecutor", side_effect=AssertionError("No idle pool for cache hits")):
            items, hits, errors = self.features(records, cache=conn, pixels={})
        self.assertEqual((len(items), len(hits), errors), (3, 3, []))

    def test_limits_close_ready_unconsumed_views_and_publish_only_consumed_hashes(self):
        records = self.records()
        captured, raw, pixels = [], {}, {}
        third_ready = threading.Event()
        original = engine._decode
        def prepare(record, *args, **kwargs):
            result = original(record, *args, **kwargs)
            captured.extend(result[2])
            if record == records[2]:
                third_ready.set()
            return result
        class Descriptor(ColorDescriptor):
            def encode(inner, images):
                if not third_ready.wait(3):
                    raise AssertionError("The extra ready result was not prepared")
                return super().encode(images)
        before = list(warnings.filters)
        with mock.patch.object(engine, "_decode", side_effect=prepare), mock.patch.object(engine, "MAX_VIEWS", 22):
            items, _, errors = self.features(records, model=Descriptor(), raw=raw, pixels=pixels)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(set(raw), {str(record.path) for record in records[:2]})
        self.assertEqual(set(pixels), set(raw))
        self.assertEqual(warnings.filters, before)
        self.assertEqual(len(captured), 66)
        self.assert_closed(captured)

    def test_model_error_or_interrupt_closes_unconsumed_results(self):
        for exception in (RuntimeError, KeyboardInterrupt):
            with self.subTest(exception=exception.__name__):
                records = self.records()
                captured, raw, pixels = [], {}, {}
                second_ready = threading.Event()
                original = engine._decode
                def prepare(record, *args, **kwargs):
                    result = original(record, *args, **kwargs)
                    captured.extend(result[2])
                    if record == records[1]:
                        second_ready.set()
                    return result
                class Descriptor(ColorDescriptor):
                    def encode(inner, images):
                        if not second_ready.wait(3):
                            raise AssertionError("Ready result was not available")
                        raise exception("synthetic model interruption")
                before = list(warnings.filters)
                with mock.patch.object(engine, "_decode", side_effect=prepare):
                    with self.assertRaises(exception):
                        self.features(records, model=Descriptor(), raw=raw, pixels=pixels)
                self.assertEqual(set(raw), {str(records[0].path)})
                self.assertEqual(set(pixels), set(raw))
                self.assertEqual(warnings.filters, before)
                self.assert_closed(captured)

    def test_native_pixel_budget_serializes_large_inputs(self):
        records = self.records(count=4, size=(20, 20))
        active, peak = 0, 0
        lock = threading.Lock()
        original = engine._decode
        def prepare(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(.02)
                return original(*args, **kwargs)
            finally:
                with lock:
                    active -= 1
        with mock.patch.object(engine, "_decode", side_effect=prepare):
            items, _, errors = self.features(records, max_pixels=600)
        self.assertEqual((len(items), errors), (4, []))
        self.assertEqual(peak, 1)

    def test_non_lazy_header_plugins_are_probed_without_native_workers(self):
        records = self.records(count=2, size=(20, 20))
        Image.new("RGB", (20, 20), "red").save(records[1].path, format="BMP")
        info = records[1].path.stat()
        records[1] = engine.core.ImageFile(records[1].path, info.st_size, engine.core.signature(info))
        active = threading.Event()
        header_checks = []
        original_decode, original_header = engine._decode, engine._decode_header_size
        def prepare(record, *args, **kwargs):
            active.set()
            try:
                time.sleep(.02)
                return original_decode(record, *args, **kwargs)
            finally:
                active.clear()
        def header(record, *args):
            if record == records[1]:
                header_checks.append(active.is_set())
            return original_header(record, *args)
        with mock.patch.object(engine, "_decode", side_effect=prepare), \
             mock.patch.object(engine, "_decode_header_size", side_effect=header):
            items, _, errors = self.features(records, max_pixels=1000)
        self.assertEqual((len(items), errors), (2, []))
        self.assertEqual(header_checks, [False])

    def test_sql_hash_and_model_operations_remain_on_calling_thread(self):
        records = self.records()
        conn = engine._open_cache(self.root / "threads.sqlite3")
        self.addCleanup(conn.close)
        owner = threading.get_ident()
        raw_threads, writer_threads, model_threads, decode_threads = [], [], [], []
        digest, store, decode = engine.core.exact_digest, engine._store_pixel_digest, engine._decode
        def raw_hash(*args):
            raw_threads.append(threading.get_ident())
            return digest(*args)
        def write(*args):
            writer_threads.append(threading.get_ident())
            return store(*args)
        def prepare(*args, **kwargs):
            decode_threads.append(threading.get_ident())
            return decode(*args, **kwargs)
        class Descriptor(ColorDescriptor):
            def encode(inner, images):
                model_threads.append(threading.get_ident())
                return super().encode(images)
        with mock.patch.object(engine.core, "exact_digest", side_effect=raw_hash), \
             mock.patch.object(engine, "_store_pixel_digest", side_effect=write), \
             mock.patch.object(engine, "_decode", side_effect=prepare):
            _, _, errors = self.features(records, cache=conn, model=Descriptor(), pixels={})
        self.assertEqual(errors, [])
        self.assertEqual(set(raw_threads + writer_threads + model_threads), {owner})
        self.assertTrue(decode_threads)
        self.assertNotIn(owner, decode_threads)

    def test_invalid_native_budget_request_fails_without_waiting(self):
        budget = engine._DecodeBudget(10)
        with self.assertRaises(ValueError):
            with budget.claim(11):
                self.fail("Over-budget preparation started")


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
