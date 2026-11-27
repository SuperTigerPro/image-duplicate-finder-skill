"""Verify candidate coverage, Matcher-only decisions and bounded stage accounting."""
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    import numpy as np
    from PIL import Image
    import copynce_engine as engine
    import copynce_pipeline as pipeline
    import copynce_sn as sn
except ImportError:
    pipeline = None


class Descriptor:
    dimension = 4
    device = "cpu"
    weights_id = "c" * 64
    fallbacks = ()

    def encode(self, images):
        return np.array([[*np.asarray(image).mean(axis=(0, 1)), 1] for image in images], dtype=np.float32)


class Matcher:
    device = "cpu"
    weights_id = "e" * 64
    fallbacks = ()

    def score_pairs(self, left, right):
        return np.full(len(left), .8, dtype=np.float32)

    def score_pairs_lce(self, left, right):
        return self.score_pairs(left, right)


@unittest.skipIf(pipeline is None, "Pipeline needs the optional image dependencies")
class EnhancedPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.images = self.root / "query"
        self.images.mkdir()
        background = self.root / "background"
        background.mkdir()
        for i in range(8):
            Image.new("RGB", (11, 13), (10 + i * 15, 20 + i * 7, 60 + i * 5)).save(background / f"background-{i}.png")
        self.descriptor = Descriptor()
        patch = mock.patch.object(engine, "_load_descriptor", return_value=self.descriptor)
        patch.start()
        self.addCleanup(patch.stop)
        self.index = self.root / "sn-index"
        sn.build_index([background], Path("fixture"), self.index)
        self.matcher = Matcher()
        self.real_matcher_loader = pipeline._load_matcher
        patch = mock.patch.object(pipeline, "_matcher_weights_id", return_value=self.matcher.weights_id)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(pipeline, "_load_matcher", return_value=self.matcher)
        patch.start()
        self.addCleanup(patch.stop)

    def imageset(self, count):
        for i in range(count):
            Image.new("RGB", (24, 17), (220 - i * 20, i * 40, 150 - i * 17)).save(self.images / f"query-{i}.png")

    def scan(self, **kwargs):
        cache = kwargs.pop("cache", None)
        options = dict(profile="enhanced", matcher_weights=Path("fixture-matcher"), sn_index=self.index,
                       candidate_k=32, matcher_budget=100, sn_extra_budget=0, top_k=100)
        options.update(kwargs)
        return engine.scan([self.images], weights=Path("fixture"), cache=cache, **options)

    def test_threshold_is_matcher_only_and_fast_stays_compatible(self):
        self.imageset(3)
        result = self.scan(threshold=1, matcher_threshold=.7)
        self.assertEqual(result["schema_version"], 4)
        self.assertEqual((result["pairs_possible"], result["pairs_assessed"], len(result["matches"])), (3, 3, 3))
        self.assertTrue(result["budget_complete"])
        self.assertTrue(result["exhaustive"])
        self.assertEqual(result["score_type"], "classifier_sigmoid")
        row = result["matches"][0]
        self.assertIn("descriptor_cosine", row)
        self.assertIn("local_cosine", row)
        self.assertIn("sn_a_to_b", row)
        self.assertIn("sn_b_to_a", row)
        self.assertTrue(row["matcher_verified"])
        self.assertFalse(result["descriptor_threshold_applied"])
        self.assertEqual(self.scan(matcher_threshold=.9)["matches"], [])
        fast = engine.scan([self.images], weights=Path("fixture"), cache=None, tiles=False, threshold=1)
        self.assertNotEqual(fast.get("schema_version"), 4)

    def test_pending_candidates_are_not_claimed_as_matcher_hits(self):
        self.imageset(4)
        raw = self.scan(matcher_budget=1)
        stage = raw["stage_totals"]
        self.assertEqual(raw["pairs_assessed"], 1)
        self.assertGreater(stage["matcher_pending"], 0)
        self.assertEqual(stage["planned_candidates"], stage["matcher_verified"] + stage["matcher_failed"] + stage["matcher_pending"])
        self.assertFalse(raw["budget_complete"])
        self.assertFalse(raw["exhaustive"])
        self.assertEqual(len(raw["matches"]), 1)

    def test_topk_return_limit_is_distinct_from_verification_budget(self):
        self.imageset(4)
        raw = self.scan(top_k=1)
        self.assertEqual(raw["pairs_above_threshold"], 6)
        self.assertTrue(raw["truncated"])
        self.assertTrue(raw["budget_complete"])
        self.assertEqual(len(raw["matches"]), 1)

    def test_matcher_failures_are_errors_not_fabricated_hits(self):
        self.imageset(3)
        self.matcher.score_pairs = mock.Mock(side_effect=RuntimeError("synthetic failure"))
        raw = self.scan()
        self.assertEqual(raw["matches"], [])
        self.assertGreater(raw["error_count"], 0)
        self.assertEqual(raw["stage_totals"]["matcher_failed"], 3)
        self.assertEqual(raw["stage_totals"]["matcher_pending"], 0)
        self.assertTrue(raw["budget_complete"])

    def test_local_evidence_is_tested_with_both_directions(self):
        self.imageset(2)
        def score(left, right):
            return np.array([.1, .9], dtype=np.float32)
        self.matcher.score_pairs = mock.Mock(side_effect=score)
        raw = self.scan(local_view_pairs=1)
        row = raw["matches"][0]
        self.assertEqual(self.matcher.score_pairs.call_count, 2)
        self.assertEqual(raw["stage_totals"]["matcher_model_comparisons"], 4)
        self.assertEqual(row["box_source"], "matcher-tested-window")
        self.assertNotEqual((row["box_a"], row["box_b"]), ([0, 0, 24, 17], [0, 0, 24, 17]))

    def test_new_pairs_reuse_the_evidence_already_computed_for_the_cache_key(self):
        self.imageset(3)
        with mock.patch.object(pipeline, "_evidence", wraps=pipeline._evidence) as evidence:
            raw = self.scan()
        self.assertEqual(raw["stage_totals"]["matcher_new_verified"], 3)
        self.assertEqual(evidence.call_count, 3)

    def test_automatic_token_reuse_is_limited_to_cuda_enhanced_unsplit_batches(self):
        for device, profile, views, expected in (("cpu", "enhanced", 2, False),
                                                  ("mps", "enhanced", 2, False),
                                                  ("cuda", "deep", 2, False),
                                                  ("cuda", "enhanced", 8, False),
                                                  ("cuda", "enhanced", 2, True)):
            with self.subTest(device=device, profile=profile, views=views):
                model = SimpleNamespace(device=device, max_batch=8, enable_token_cache=mock.Mock())
                module = SimpleNamespace(Matcher=mock.Mock(return_value=model))
                with mock.patch.dict(sys.modules, {"copynce_matcher": module}):
                    result = self.real_matcher_loader(Path("fixture"), "auto", profile=profile,
                                                      local_view_pairs=views)
                self.assertIs(result, model)
                self.assertEqual(model.enable_token_cache.call_count, int(expected))
                self.assertEqual(getattr(model, "_token_cache_cuda_auto", False), expected)

    def test_deep_uses_native_images_and_reports_two_star_passes(self):
        self.imageset(2)
        sizes = []
        def lce(left, right):
            sizes.extend(image.size for image in left + right)
            return np.full(len(left), .9, dtype=np.float32)
        self.matcher.score_pairs_lce = mock.Mock(side_effect=lce)
        raw = self.scan(profile="deep")
        self.assertEqual(sizes, [(24, 17)] * 4)
        self.assertEqual(self.matcher.score_pairs_lce.call_count, 2)
        self.assertEqual(raw["stage_totals"]["matcher_model_comparisons"], 70)
        self.assertEqual(raw["matches"][0]["box_source"], "descriptor-evidence")

    def test_changed_input_is_removed_after_matcher_even_if_it_scored_high(self):
        self.imageset(2)
        first = self.images / "query-0.png"
        def score(left, right):
            first.unlink(missing_ok=True)
            return np.full(len(left), .99, dtype=np.float32)
        self.matcher.score_pairs = mock.Mock(side_effect=score)
        raw = self.scan()
        self.assertEqual(raw["matches"], [])
        self.assertEqual(raw["pairs_assessed"], 0)
        self.assertTrue(any(error["operation"] == "verify" for error in raw["errors"]))

    def test_recall_heap_matches_brute_force_without_a_cosine_threshold(self):
        rng = np.random.default_rng(7)
        items = []
        for i in range(7):
            vectors = rng.normal(size=(3, 4)).astype(np.float32)
            vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
            items.append((str(i), (10, 10), [(0, 0, 10, 10)] * 3, vectors))
        global_heaps, local_heaps, assessed = pipeline._top_candidates(items, 2)
        self.assertEqual(assessed, 21)
        for i in range(len(items)):
            expected = sorted(((float((items[i][3] @ items[j][3].T).max()), -j)
                               for j in range(len(items)) if i != j), reverse=True)[:2]
            self.assertEqual([-x[1] for x in sorted(local_heaps[i], reverse=True)], [-x[1] for x in expected])

    def test_sn_extra_budget_never_preempts_per_image_base(self):
        self.imageset(6)
        raw = self.scan(candidate_k=1, matcher_budget=1, sn_extra_budget=4)
        self.assertEqual(raw["matches"][0]["candidate_tier"], "base")
        self.assertEqual(raw["stage_totals"]["matcher_attempted"], 1)
        self.assertFalse(raw["stage_totals"]["base_budget_complete"])

    def test_deep_whole_only_is_rejected_before_model_loading(self):
        with self.assertRaisesRegex(ValueError, "whole-only"):
            self.scan(profile="deep", tiles=False)

    def test_repeating_budget_one_progresses_without_rechecking_previous_pairs(self):
        self.imageset(3)
        cache = self.root / "resume.sqlite3"
        self.matcher.score_pairs = mock.Mock(wraps=self.matcher.score_pairs)
        for run in range(1, 4):
            raw = self.scan(cache=cache, matcher_budget=1)
            stage = raw["stage_totals"]
            self.assertEqual((stage["matcher_cache_hits"], stage["matcher_new_verified"], stage["matcher_attempted_new"]),
                             (run - 1, 1, 1))
            self.assertEqual((stage["matcher_verified"], stage["matcher_pending"]), (run, 3 - run))
            self.assertEqual(self.matcher.score_pairs.call_count, run * 2)
        self.assertTrue(raw["budget_complete"])
        with mock.patch.object(pipeline, "_load_matcher", side_effect=AssertionError("cached-only must not load a model")):
            raw = self.scan(cache=cache, matcher_budget=1)
        self.assertEqual(raw["stage_totals"]["matcher_cache_hits"], 3)
        self.assertEqual(raw["stage_totals"]["matcher_attempted_new"], 0)
        self.assertEqual(raw["stage_totals"]["matcher_model_comparisons"], 0)
        self.assertEqual(raw["matcher_device"], "cache")
        self.assertEqual(raw["matcher_cache_devices"], ["cpu"])

    def test_negative_scores_are_cached_and_new_threshold_is_applied(self):
        self.imageset(3)
        cache = self.root / "negative.sqlite3"
        self.matcher.score_pairs = mock.Mock(side_effect=lambda left, right: np.full(len(left), .2, np.float32))
        first = self.scan(cache=cache, matcher_threshold=.5)
        self.assertEqual(first["matches"], [])
        with mock.patch.object(pipeline, "_load_matcher", side_effect=AssertionError("negative results are reusable")):
            second = self.scan(cache=cache, matcher_threshold=.1)
        self.assertEqual((len(second["matches"]), second["stage_totals"]["matcher_cache_hits"]), (3, 3))

    def test_later_cache_hits_are_read_after_new_budget_is_spent(self):
        self.imageset(3)
        cache = self.root / "later.sqlite3"
        self.scan(cache=cache)
        with closing(sqlite3.connect(cache)) as connection, connection:
            key = connection.execute("SELECT cache_key FROM matcher_results ORDER BY rowid LIMIT 1").fetchone()[0]
            connection.execute("DELETE FROM matcher_results WHERE cache_key=?", (key,))
        raw = self.scan(cache=cache, matcher_budget=1)
        self.assertEqual((raw["stage_totals"]["matcher_new_verified"], raw["stage_totals"]["matcher_cache_hits"]), (1, 2))
        self.assertEqual(raw["stage_totals"]["matcher_pending"], 0)

    def test_profile_views_and_weight_identity_invalidate_matcher_results(self):
        self.imageset(2)
        cache = self.root / "profiles.sqlite3"
        self.scan(cache=cache, local_view_pairs=0)
        changed_view = self.scan(cache=cache, local_view_pairs=1)
        self.assertEqual(changed_view["stage_totals"]["matcher_new_verified"], 1)
        changed_profile = self.scan(cache=cache, profile="deep", local_view_pairs=1)
        self.assertEqual(changed_profile["stage_totals"]["matcher_new_verified"], 1)
        with mock.patch.object(pipeline, "_matcher_weights_id", return_value="f" * 64), \
                mock.patch.object(self.matcher, "weights_id", "f" * 64):
            changed_weights = self.scan(cache=cache, local_view_pairs=1)
        self.assertEqual(changed_weights["stage_totals"]["matcher_new_verified"], 1)
        with mock.patch.object(self.descriptor, "weights_id", "d" * 64):
            index = self.root / "new-descriptor-index"
            sn.build_index([self.root / "background"], Path("fixture"), index)
            changed_descriptor = self.scan(cache=cache, local_view_pairs=1, sn_index=index)
        self.assertEqual(changed_descriptor["stage_totals"]["matcher_new_verified"], 1)

    def test_changed_file_invalidates_only_its_pairs(self):
        self.imageset(3)
        cache = self.root / "changed.sqlite3"
        self.scan(cache=cache)
        Image.new("RGB", (24, 17), (1, 2, 3)).save(self.images / "query-0.png")
        raw = self.scan(cache=cache)
        self.assertEqual(raw["stage_totals"]["matcher_cache_hits"], 1)
        self.assertEqual(raw["stage_totals"]["matcher_new_verified"], 2)

    def test_corrupt_cached_values_and_out_of_scope_fields_are_misses(self):
        self.imageset(2)
        cache = self.root / "corrupt.sqlite3"
        self.scan(cache=cache)
        for corruption in ({"path_a": "/unapproved"}, {"box_a": [-1, 0, 1, 1]},
                           {"matcher_score": float("nan")}, {"model_comparisons": 3}):
            with closing(sqlite3.connect(cache)) as connection, connection:
                key, text = connection.execute("SELECT cache_key,payload FROM matcher_results").fetchone()
                payload = json.loads(text)
                payload.update(corruption)
                connection.execute("UPDATE matcher_results SET payload=? WHERE cache_key=?", (json.dumps(payload), key))
            raw = self.scan(cache=cache)
            self.assertEqual((raw["stage_totals"]["matcher_cache_hits"], raw["stage_totals"]["matcher_new_verified"]), (0, 1))
            self.assertTrue(all(Path(row[side]).parent == self.images for row in raw["matches"] for side in ("path_a", "path_b")))

    def test_background_bias_is_recomputed_while_matcher_scores_are_reused(self):
        self.imageset(3)
        cache = self.root / "background-change.sqlite3"
        first = self.scan(cache=cache)
        alternate = self.root / "alternate-background"
        alternate.mkdir()
        for i in range(8):
            Image.new("RGB", (13, 11), (2 + i, 240 - i * 17, 70 + i * 5)).save(alternate / f"alternate-{i}.png")
        index = self.root / "alternate-index"
        sn.build_index([alternate], Path("fixture"), index)
        with mock.patch.object(pipeline, "_load_matcher", side_effect=AssertionError("SN does not invalidate Matcher results")):
            second = self.scan(cache=cache, sn_index=index)
        self.assertNotEqual(first["sn_index_fingerprint"], second["sn_index_fingerprint"])
        a = {(r["path_a"], r["path_b"]): r for r in first["matches"]}
        b = {(r["path_a"], r["path_b"]): r for r in second["matches"]}
        self.assertEqual(second["stage_totals"]["matcher_cache_hits"], 3)
        self.assertTrue(all(a[key]["matcher_score"] == b[key]["matcher_score"] for key in a))
        self.assertTrue(any(abs(a[key]["sn_a_to_b"] - b[key]["sn_a_to_b"]) > .001 for key in a))

    def test_cancelled_run_preserves_prior_successful_pair(self):
        self.imageset(3)
        cache = self.root / "cancel.sqlite3"
        calls = 0
        def score(left, right):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise KeyboardInterrupt
            return np.full(len(left), .8, np.float32)
        self.matcher.score_pairs = score
        with self.assertRaises(KeyboardInterrupt):
            self.scan(cache=cache)
        self.matcher.score_pairs = lambda left, right: np.full(len(left), .8, np.float32)
        resumed = self.scan(cache=cache, matcher_budget=1)
        self.assertEqual((resumed["stage_totals"]["matcher_cache_hits"], resumed["stage_totals"]["matcher_new_verified"]), (1, 1))

    def test_enhanced_cache_is_rejected_inside_git_before_creating_a_database(self):
        git = self.root / "repo"
        git.mkdir()
        (git / ".git").mkdir()
        cache = git / "private.sqlite3"
        with self.assertRaisesRegex(ValueError, "Git"):
            self.scan(cache=cache)
        self.assertFalse(cache.exists())

    def test_cache_row_cap_prunes_only_old_matcher_results(self):
        self.imageset(3)
        cache = self.root / "bounded.sqlite3"
        self.scan(cache=cache)
        with mock.patch.object(pipeline, "MAX_MATCHER_CACHE_ROWS", 2):
            with pipeline._matcher_cache(cache) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM matcher_results").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM descriptors").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
