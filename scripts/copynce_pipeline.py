"""Bounded per-image recall, directed global SN and independent Matcher verification."""
from __future__ import annotations

import heapq
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
from contextlib import contextmanager
import warnings

import numpy as np
from PIL import Image, ImageOps

if __package__:
    from . import copynce_engine as engine
    from . import copynce_sn as sn
else:
    import copynce_engine as engine
    import copynce_sn as sn

MAX_CANDIDATE_PAIRS = 200000
MAX_CANDIDATE_K = 256
MAX_LOCAL_VIEW_PAIRS = 8
EXTRA_PER_QUERY_CAP = 8
MATCHER_CACHE_POLICY = "matcher-result-v1-bidirectional-f32"
MAX_MATCHER_CACHE_ROWS = 200000


def _load_matcher(weights, device):
    if __package__:
        from .copynce_matcher import Matcher
    else:
        from copynce_matcher import Matcher
    return Matcher(str(weights), device)


def _matcher_weights_id(weights):
    """Verify the real asset even when no new inference model needs to be loaded."""
    if __package__:
        from .copynce_matcher import OFFICIAL_BYTES, OFFICIAL_SHA256
    else:
        from copynce_matcher import OFFICIAL_BYTES, OFFICIAL_SHA256
    path = engine.core.canonical_input(weights)
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_size != OFFICIAL_BYTES:
        raise ValueError("Matcher checkpoint size mismatch")
    record = engine.core.ImageFile(path, info.st_size, engine.core.signature(info))
    with engine.core.checked_stream(record) as stream:
        digest = engine.core.exact_digest(stream)
    if digest != OFFICIAL_SHA256:
        raise ValueError("Matcher checkpoint checksum mismatch")
    return digest


@contextmanager
def _matcher_cache(location):
    if location is None:
        yield None
        return
    path = sn._private_path(location, exists=False)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags, 0o600)
    connection = None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or os.name != "nt" and
                (info.st_uid != os.getuid() or info.st_mode & 0o077)):
            raise ValueError("Matcher cache must be an owned private regular file")
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=30)
        current = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("Matcher cache changed while opening")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("CREATE TABLE IF NOT EXISTS matcher_results (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        _trim_matcher_cache(connection)
        connection.commit()
        yield connection
    finally:
        if connection is not None:
            connection.close()
        os.close(fd)


def _trim_matcher_cache(connection):
    count = connection.execute("SELECT COUNT(*) FROM matcher_results").fetchone()[0]
    if count > MAX_MATCHER_CACHE_ROWS:
        connection.execute("DELETE FROM matcher_results WHERE rowid IN "
                           "(SELECT rowid FROM matcher_results ORDER BY rowid LIMIT ?)",
                           (count - MAX_MATCHER_CACHE_ROWS,))


def _cache_key(items, a, b, content_hashes, pixel_hashes, descriptor_hash, matcher_hash, profile,
               local_view_pairs, selected_views):
    identity = dict(policy=MATCHER_CACHE_POLICY, pipeline=engine.PIPELINE,
                    descriptor_weights_sha256=descriptor_hash, matcher_weights_sha256=matcher_hash,
                    profile=profile, local_view_pairs=local_view_pairs,
                    selected_views=selected_views if profile == "enhanced" else None,
                    files=[dict(path=items[i][0], raw_sha256=content_hashes[items[i][0]],
                                pixel_sha256=pixel_hashes[items[i][0]]) for i in (a, b)])
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _cache_payload(row, comparisons, device):
    keys = ("matcher_score", "matcher_a_to_b", "matcher_b_to_a", "box_a", "box_b", "box_source", "matcher_view_policy")
    return {**{key: row[key] for key in keys}, "model_comparisons": comparisons, "device": device}


def _load_cached_matcher(connection, key, items, a, b, profile, local_view_pairs):
    if connection is None:
        return None
    record = connection.execute("SELECT payload FROM matcher_results WHERE cache_key=?", (key,)).fetchone()
    if record is None or not isinstance(record[0], str) or len(record[0]) > 4096:
        return None
    try:
        row = json.loads(record[0])
        expected = {"matcher_score", "matcher_a_to_b", "matcher_b_to_a", "box_a", "box_b", "box_source",
                    "matcher_view_policy", "model_comparisons", "device"}
        if not isinstance(row, dict) or set(row) != expected:
            return None
        for key in ("matcher_score", "matcher_a_to_b", "matcher_b_to_a"):
            if type(row[key]) not in (int, float) or not math.isfinite(row[key]) or not 0 <= row[key] <= 1:
                return None
        if abs(row["matcher_score"] - max(row["matcher_a_to_b"], row["matcher_b_to_a"])) > 1e-7:
            return None
        policy = ("official-star-35-bidirectional" if profile == "deep"
                  else "whole-plus-descriptor-evidence-bidirectional")
        if row["matcher_view_policy"] != policy or row["box_source"] != ("descriptor-evidence" if profile == "deep" else "matcher-tested-window"):
            return None
        expected_calls = (70 if profile == "deep" else
                          2 * (1 + min(local_view_pairs, len(items[a][2]) * len(items[b][2]) - 1)))
        if type(row["model_comparisons"]) is not int or row["model_comparisons"] != expected_calls:
            return None
        if not isinstance(row["device"], str) or not re.fullmatch(r"cpu|mps|cuda(:[0-9]{1,2})?", row["device"]):
            return None
        for side, item in (("a", items[a]), ("b", items[b])):
            box = row["box_" + side]
            if (not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box) or
                    not 0 <= box[0] < box[2] <= item[1][0] or not 0 <= box[1] < box[3] <= item[1][1]):
                return None
            if profile == "enhanced" and tuple(box) not in item[2]:
                return None
        return row
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def _store_cached_matcher(connection, key, row, comparisons, device, *, prune=False):
    if connection is None:
        return
    connection.execute("INSERT OR REPLACE INTO matcher_results VALUES(?,?)",
                       (key, json.dumps(_cache_payload(row, comparisons, device), allow_nan=False, separators=(",", ":"))))
    if prune:
        _trim_matcher_cache(connection)
    connection.commit()  # Preserve every completed pair if the next one is cancelled.


def _keep(heap, score, neighbor, k):
    value = (float(score), -neighbor)
    if len(heap) < k:
        heapq.heappush(heap, value)
    elif value > heap[0]:
        heapq.heapreplace(heap, value)


def _merge_row(heap, values, neighbors, k):
    if not k:
        return
    accepted = np.isfinite(values)
    if len(heap) == k:
        accepted &= values >= heap[0][0]
    indices = np.flatnonzero(accepted)
    if len(indices) > k:
        indices = indices[np.lexsort((neighbors[indices], -values[indices]))[:k]]
    for position in indices:
        _keep(heap, values[position], int(neighbors[position]), k)


def _top_candidates(items, k):
    """Score every unordered image pair in bounded blocks, keep only per-image K."""
    n = len(items)
    global_heaps, local_heaps = [[] for _ in items], [[] for _ in items]
    if n < 2:
        return global_heaps, local_heaps, 0
    views = max(len(item[2]) for item in items)
    dim = items[0][3].shape[1]
    vectors = np.empty((n, views, dim), dtype=np.float32)
    for i, item in enumerate(items):
        count = len(item[2])
        vectors[i, :count] = item[3]
        vectors[i, count:] = item[3][0]
    block = min(engine.BLOCK_IMAGES, max(1, 768 // views))
    assessed = 0
    for a in range(0, n, block):
        left = vectors[a:a + block]
        for b in range(a, n, block):
            right = vectors[b:b + block]
            products = left.reshape(-1, dim) @ right.reshape(-1, dim).T
            scores = products.reshape(len(left), views, len(right), views).transpose(0, 2, 1, 3)
            globals_ = np.clip(scores[:, :, 0, 0], -1, 1)
            locals_ = np.clip(scores.max(axis=(2, 3)), -1, 1)
            assessed += len(left) * (len(left) - 1) // 2 if a == b else len(left) * len(right)
            for heaps, values in ((global_heaps, globals_), (local_heaps, locals_)):
                if a == b:
                    np.fill_diagonal(values, -np.inf)
                right_ids = np.arange(b, b + len(right))
                for i, row in enumerate(values):
                    _merge_row(heaps[a + i], row, right_ids, k)
                if a != b:
                    left_ids = np.arange(a, a + len(left))
                    for j, row in enumerate(values.T):
                        _merge_row(heaps[b + j], row, left_ids, k)
    return global_heaps, local_heaps, assessed


def _base_candidates(global_heaps, local_heaps):
    ordered = [[[-neighbor for _, neighbor in sorted(heap, reverse=True)]
                for heap in heaps] for heaps in (global_heaps, local_heaps)]
    plan = {}
    limit_reached = False
    rank_count = max((len(row) for channel in ordered for row in channel), default=0)
    for rank in range(rank_count):
        for query in range(len(global_heaps)):
            for channel_id, channel in enumerate(ordered):
                if rank >= len(channel[query]):
                    continue
                reference = channel[query][rank]
                edge = tuple(sorted((query, reference)))
                if edge not in plan:
                    if len(plan) == MAX_CANDIDATE_PAIRS:
                        limit_reached = True
                        continue
                    plan[edge] = {"channels": set(), "directions": set(), "tier": "base"}
                plan[edge]["channels"].add("global" if channel_id == 0 else "local")
                plan[edge]["directions"].add("a-to-b" if query == edge[0] else "b-to-a")
    return plan, limit_reached


def _extra_candidates(items, bias, base, budget):
    """Official directed SN orders extra global candidates; per-query rounds cap dominance."""
    n = len(items)
    if not budget or n < 2:
        return {}, 0
    quota = min(EXTRA_PER_QUERY_CAP, max(1, (budget + n - 1) // n))
    buckets = [[] for _ in items]
    vectors = np.stack([item[3][0] for item in items])
    blocked = [set([i]) for i in range(n)]
    for a, b in base:
        blocked[a].add(b)
        blocked[b].add(a)
    reference_ids = np.arange(n)
    for a in range(0, n, engine.BLOCK_IMAGES):
        scores = np.clip(vectors[a:a + engine.BLOCK_IMAGES] @ vectors.T, -1, 1)
        for i, row in enumerate(scores):
            query = a + i
            row[list(blocked[query])] = -np.inf
            best = np.lexsort((reference_ids, -row))[:quota]
            buckets[query] = [(float(row[j]) - float(bias[query]), -int(j)) for j in best if np.isfinite(row[j])]
    extra = {}
    for rank in range(quota):
        current = [(bucket[rank][0], query, -bucket[rank][1])
                   for query, bucket in enumerate(buckets) if rank < len(bucket)]
        for score, query, reference in sorted(current, key=lambda x: (-x[0], x[1], x[2])):
            edge = tuple(sorted((query, reference)))
            if edge in extra:
                extra[edge]["directions"].add("a-to-b" if query == edge[0] else "b-to-a")
                continue
            if len(extra) == budget or len(base) + len(extra) == MAX_CANDIDATE_PAIRS:
                return extra, quota
            extra[edge] = dict(channels={"global-sn-extra"}, directions={"a-to-b" if query == edge[0] else "b-to-a"},
                               tier="extra", scheduling_query="a" if query == edge[0] else "b")
    return extra, quota


def _evidence(items, a, b, bias, local_view_pairs):
    item_a, item_b = items[a], items[b]
    products = np.clip(item_a[3] @ item_b[3].T, -1, 1)
    chosen = []
    for index in np.argsort(-products, axis=None, kind="stable"):
        x, y = np.unravel_index(int(index), products.shape)
        if (x, y) == (0, 0):
            continue
        chosen.append((int(x), int(y)))
        if len(chosen) == local_view_pairs:
            break
    if local_view_pairs == 0:
        chosen = []
    x, y = np.unravel_index(int(products.argmax()), products.shape)
    whole = float(products[0, 0])
    return dict(path_a=item_a[0], path_b=item_b[0], size_a=list(item_a[1]), size_b=list(item_b[1]),
                box_a=list(item_a[2][x]), box_b=list(item_b[2][y]), descriptor_cosine=whole,
                local_cosine=float(products[x, y]), sn_a_to_b=whole - float(bias[a]),
                sn_b_to_a=whole - float(bias[b]), sn_bias_a=float(bias[a]), sn_bias_b=float(bias[b])), chosen


def _original_rgb(record, max_pixels):
    # Apply the same format/safety checks as feature extraction before loading native-size deep views.
    _, _, validation = engine._decode(record, False, max_pixels)
    for image in validation:
        image.close()
    with engine.core.checked_stream(record) as stream, warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(stream) as image, ImageOps.exif_transpose(image) as oriented:
            return oriented.convert("RGB")


def _verify_pair(items, records, a, b, matcher, profile, local_view_pairs, bias, max_pixels):
    row, local_views = _evidence(items, a, b, bias, local_view_pairs)
    if profile == "deep":
        with _original_rgb(records[row["path_a"]], max_pixels) as left, _original_rgb(records[row["path_b"]], max_pixels) as right:
            ab = float(np.asarray(matcher.score_pairs_lce([left], [right]))[0])
            ba = float(np.asarray(matcher.score_pairs_lce([right], [left]))[0])
        count = 70
        row.update(matcher_view_policy="official-star-35-bidirectional", box_source="descriptor-evidence")
    else:
        _, _, left = engine._decode(records[row["path_a"]], len(items[a][2]) > 1, max_pixels)
        try:
            _, _, right = engine._decode(records[row["path_b"]], len(items[b][2]) > 1, max_pixels)
            try:
                views = [(0, 0), *local_views]
                queries, references = [left[x] for x, _ in views], [right[y] for _, y in views]
                scores_ab = np.asarray(matcher.score_pairs(queries, references), dtype=np.float32)
                scores_ba = np.asarray(matcher.score_pairs(references, queries), dtype=np.float32)
                if scores_ab.shape != (len(views),) or scores_ba.shape != (len(views),):
                    raise ValueError("Matcher returned an invalid number of view scores")
                ab, ba = float(scores_ab.max()), float(scores_ba.max())
                best = int(np.maximum(scores_ab, scores_ba).argmax())
                x, y = views[best]
                row.update(box_a=list(items[a][2][x]), box_b=list(items[b][2][y]),
                           matcher_view_policy="whole-plus-descriptor-evidence-bidirectional", box_source="matcher-tested-window")
                count = 2 * len(views)
            finally:
                for image in right:
                    image.close()
        finally:
            for image in left:
                image.close()
    if not all(math.isfinite(s) and 0 <= s <= 1 for s in (ab, ba)):
        raise ValueError("Matcher returned a non-finite or invalid probability score")
    row.update(score=max(ab, ba), matcher_score=max(ab, ba), matcher_a_to_b=ab, matcher_b_to_a=ba,
               matcher_verified=True, matcher_direction="bidirectional-max", score_type="classifier_sigmoid")
    return row, count


def run(items, records, content_hashes, scanner, descriptor, *, profile, matcher_weights, sn_index,
        candidate_k=32, matcher_budget=10000, local_view_pairs=2, sn_extra_budget=1000,
        matcher_threshold=.5, top_k=100, device="auto", max_pixels=40000000, pixel_hashes=None, source_groups=None,
        cache=None):
    if profile not in {"enhanced", "deep"}:
        raise ValueError("Enhanced pipeline profile must be enhanced or deep")
    for value, limit, label in ((candidate_k, MAX_CANDIDATE_K, "candidate_k"),
                                (matcher_budget, 100000, "matcher_budget")):
        if type(value) is not int or not 1 <= value <= limit:
            raise ValueError(f"Invalid {label} resource limit")
    if type(local_view_pairs) is not int or not 0 <= local_view_pairs <= MAX_LOCAL_VIEW_PAIRS:
        raise ValueError("Invalid local view-pair limit")
    if type(sn_extra_budget) is not int or not 0 <= sn_extra_budget <= 10000:
        raise ValueError("Invalid SN extra-candidate budget")
    if not math.isfinite(matcher_threshold) or not 0 <= matcher_threshold <= 1:
        raise ValueError("Matcher threshold must be an independent probability threshold")
    if matcher_weights is None or sn_index is None:
        raise ValueError("Enhanced profiles require Matcher weights and an independent SN background index")
    background = sn.load_index(sn_index, expected_weights_sha256=descriptor.weights_id)
    whole = np.stack([item[3][0] for item in items]) if items else np.empty((0, descriptor.dimension), np.float32)
    group_hashes = []
    if source_groups is not None:
        import hashlib
        group_hashes = [hashlib.sha256(str(source_groups[item[0]]).encode()).hexdigest()
                        for item in items if item[0] in source_groups]
    bias, sn_info = sn.compute_bias(whole, background, excluded_sha256=content_hashes.values(),
                                    excluded_pixel_sha256=() if pixel_hashes is None else pixel_hashes.values(),
                                    excluded_source_groups=group_hashes)
    sn_info["query_source_groups_complete"] = source_groups is not None and all(item[0] in source_groups for item in items)
    k = min(candidate_k, max(0, len(items) - 1))
    global_heaps, local_heaps, assessed = _top_candidates(items, k)
    base, limited = _base_candidates(global_heaps, local_heaps)
    extra, quota = _extra_candidates(items, bias, base, sn_extra_budget)
    plan = dict(base)
    plan.update(extra)
    if limited:
        scanner.errors.append(dict(path="", operation="candidate-limit", error_type="ModelLimitError",
                                   message="Candidate safety limit reached; per-image coverage is incomplete"))
    previous_device = descriptor.device
    if hasattr(descriptor, "_move"):
        descriptor._move("cpu")
        if hasattr(descriptor, "_release_accelerator"):
            descriptor._release_accelerator(previous_device)
    matcher_hash = _matcher_weights_id(matcher_weights)
    matcher = None
    visited = set()
    cached_edges, new_edges = set(), set()
    cached_devices = set()
    attempted_new = 0
    cache_writes = 0
    comparisons = 0
    verified = {}
    heap = []
    with _matcher_cache(cache) as connection:
        for edge, reason in plan.items():
            a, b = edge
            try:
                for i in edge:
                    record = records[items[i][0]]
                    engine.core.assert_unchanged(record, os.stat(record.path, follow_symlinks=False))
                # Recompute all Descriptor/SN fields and selection reasons in the current run.
                row, views = _evidence(items, a, b, bias, local_view_pairs)
                key = (_cache_key(items, a, b, content_hashes, pixel_hashes, descriptor.weights_id, matcher_hash,
                                  profile, local_view_pairs, views) if connection is not None else None)
                stored = _load_cached_matcher(connection, key, items, a, b, profile, local_view_pairs)
                if stored is not None:
                    visited.add(edge)
                    cached_edges.add(edge)
                    cached_devices.add(stored["device"])
                    row.update(matcher_score=stored["matcher_score"], score=stored["matcher_score"],
                               matcher_a_to_b=stored["matcher_a_to_b"], matcher_b_to_a=stored["matcher_b_to_a"],
                               matcher_view_policy=stored["matcher_view_policy"], box_source=stored["box_source"],
                               matcher_verified=True, matcher_direction="bidirectional-max", score_type="classifier_sigmoid",
                               matcher_cache_hit=True, matcher_cached_device=stored["device"])
                    if profile == "enhanced":
                        row.update(box_a=stored["box_a"], box_b=stored["box_b"])
                else:
                    if attempted_new == matcher_budget:
                        continue  # Later planned pairs can still have reusable verified cache entries.
                    visited.add(edge)
                    attempted_new += 1
                    if matcher is None:
                        matcher = _load_matcher(matcher_weights, device)
                        if matcher.weights_id != matcher_hash:
                            raise ValueError("Matcher identity differs from the verified cache policy")
                    row, calls = _verify_pair(items, records, a, b, matcher, profile, local_view_pairs, bias, max_pixels)
                    comparisons += calls
                    row["matcher_cache_hit"] = False
                    cache_writes += 1
                    _store_cached_matcher(connection, key, row, calls, matcher.device, prune=cache_writes % 256 == 0)
                    new_edges.add(edge)
                row.update(candidate_channels=sorted(reason["channels"]), candidate_directions=sorted(reason["directions"]),
                           candidate_tier=reason["tier"], scheduling_query=reason.get("scheduling_query"))
                verified[edge] = row["matcher_score"] >= matcher_threshold
                if verified[edge]:
                    value = (row["matcher_score"], -a, -b, row)
                    if len(heap) < top_k:
                        heapq.heappush(heap, value)
                    elif value[:3] > heap[0][:3]:
                        heapq.heapreplace(heap, value)
            except (OSError, ValueError, RuntimeError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
                visited.add(edge)
                scanner.errors.append(dict(path=items[a][0], path_b=items[b][0], operation="matcher", message=str(exc)))
    valid = set()
    for item in items:
        record = records[item[0]]
        try:
            engine.core.assert_unchanged(record, os.stat(record.path, follow_symlinks=False))
            valid.add(item[0])
        except OSError as exc:
            scanner.error(record.path, "verify", exc)
    verified = {edge: hit for edge, hit in verified.items() if items[edge[0]][0] in valid and items[edge[1]][0] in valid}
    rows = [value[3] for value in sorted(heap, key=lambda v: v[:3], reverse=True)
            if value[3]["path_a"] in valid and value[3]["path_b"] in valid]
    involved = {i for edge in verified for i in edge}
    queried = {i for edge in plan for i in edge}
    possible = len(items) * (len(items) - 1) // 2
    hits = sum(verified.values())
    attempted = len(visited)
    budget_complete = attempted == len(plan) and not limited
    stage = dict(descriptor_pairs_assessed=assessed,
                 global_directed_candidates=sum(map(len, global_heaps)), local_directed_candidates=sum(map(len, local_heaps)),
                 base_candidates=len(base), extra_candidates=len(extra), planned_candidates=len(plan),
                 matcher_attempted=attempted, matcher_verified=len(verified), matcher_failed=attempted - len(verified),
                 matcher_pending=len(plan) - attempted, matcher_model_comparisons=comparisons,
                 matcher_cache_hits=len(cached_edges.intersection(verified)),
                 matcher_new_verified=len(new_edges.intersection(verified)), matcher_attempted_new=attempted_new,
                 queries_with_candidates=len(queried), queries_with_verified_pairs=len(involved),
                 queries_without_verified_pairs=len(items) - len(involved), candidate_limit_reached=limited,
                 base_budget_complete=all(edge in visited for edge in base) and not limited, sn_extra_per_query_cap=quota)
    return dict(schema_version=4, profile=profile, matches=rows, score_type="classifier_sigmoid",
                pairs_assessed=len(verified), pairs_compared=len(verified), pairs_above_threshold=hits,
                pairs_returned=len(rows), exhaustive=len(verified) == possible and not limited,
                truncated=hits > len(rows), budget_complete=budget_complete, stage_totals=stage,
                matcher_weights_sha256=matcher_hash, matcher_device=matcher.device if matcher is not None else "cache",
                matcher_device_fallbacks=list(matcher.fallbacks) if matcher is not None else [], matcher_threshold=matcher_threshold,
                matcher_cache_enabled=cache is not None, matcher_cache_devices=sorted(cached_devices),
                matcher_cache_policy=MATCHER_CACHE_POLICY,
                matcher_threshold_calibrated=False, matcher_aggregation="bidirectional-max",
                sn_index_fingerprint=background.fingerprint, sn=sn_info, candidate_k=candidate_k,
                matcher_budget=matcher_budget, local_view_pairs=local_view_pairs, sn_extra_budget=sn_extra_budget,
                candidate_retrieval_mode="exact-blockwise-per-image-topk-union",
                retrieval_exhaustive=assessed == possible, mode_used="exact-candidate-generation",
                count_scope="matcher-verified-candidates", workload="global-and-local-recall-sn-scheduling-matcher",
                budget_unit="new_unique_unordered_image_pairs", pending_candidates=len(plan) - len(visited),
                unverified_candidates=len(plan) - len(verified))
