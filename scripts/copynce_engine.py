"""File selection, cached CopyNCE whole-image/region descriptors and cosine retrieval."""
from __future__ import annotations

import heapq
import math
import os
import sqlite3
import warnings
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageOps

if __package__:
    from . import find_duplicates as core
else:
    import find_duplicates as core

PIPELINE = "copynce-static-rgb224-exif-v4-f32"
BLOCK_IMAGES = 128
AUTO_INDEX_VIEWS = 5000
MAX_IMAGES = 10000
MAX_VIEWS = 100000
MAX_RETURNED_PAIRS = 20000


def _boxes(size, tiles):
    width, height = size
    regions = [(0, 0, width, height)]
    if not tiles:
        return regions
    cw, ch = (3 * width + 4) // 5, (3 * height + 4) // 5
    dx, dy = width - cw, height - ch
    for x, y in ((0, 0), (dx, 0), (0, dy), (dx, dy), (dx // 2, dy // 2)):
        box = (x, y, x + cw, y + ch)
        if box not in regions:
            regions.append(box)
    cw, ch = (7 * width + 19) // 20, (7 * height + 19) // 20
    for row in range(4):
        y = ((height - ch) * row + 1) // 3
        for col in range(4):
            x = ((width - cw) * col + 1) // 3
            box = (x, y, x + cw, y + ch)
            if box not in regions:
                regions.append(box)
    return regions


def _collect(paths, recursive, include_hidden, extensions, file_stamps):
    if not paths and file_stamps is None:
        raise ValueError("Select an image or directory.")
    options = SimpleNamespace(recursive=recursive, include_hidden=include_hidden, extensions=extensions)
    if file_stamps is not None:
        # The parent process has already enumerated specific regular files.
        # A renamed/replaced file must never broaden its approved scope.
        roots = sorted({Path(value).absolute() for value in paths}, key=str)
        scanner = core.Scanner(roots, options)
        for path in roots:
            try:
                expected = file_stamps.get(str(path))
                if expected is None or len(expected) != 5:
                    raise ValueError("Missing source identity in the selection manifest.")
                record = core.ImageFile(path, expected[2], tuple(expected))
                core.assert_unchanged(record, os.stat(path, follow_symlinks=False))
                scanner.files.append(record)
            except (OSError, ValueError) as exc:
                scanner.error(path, "verify", exc)
        return scanner
    roots = []
    for value in paths:
        path = core.canonical_input(value)
        if not (path.is_file() or path.is_dir()):
            raise ValueError(f"Select a file or directory: {path}")
        roots.append(path)
    scanner = core.Scanner(sorted(set(roots), key=str), options)
    for root in scanner.roots:
        if root.is_dir():
            scanner.walk(root)
        else:
            scanner.add_file(root)
    scanner.files.sort(key=lambda item: str(item.path))
    return scanner


def _decode(record, tiles, max_pixels):
    views = []
    try:
        with core.checked_stream(record) as stream, warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            prefix = stream.read(26)
            stream.seek(0)
            with Image.open(stream) as image:
                if image.width * image.height > max_pixels:
                    raise ValueError("Image exceeds the configured pixel limit.")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("CopyNCE supports only single-frame static images.")
                bits = getattr(image, "tag_v2", {}).get(258, ())
                if isinstance(bits, int):
                    bits = (bits,)
                deep_png = prefix.startswith(b"\x89PNG\r\n\x1a\n") and prefix[24:25] == b"\x10"
                if image.mode not in core.NORMALIZE_MODES or deep_png or (bits and max(bits) > 8):
                    raise ValueError("High bit-depth or special color format; use exact or pixels.")
                if image.format == "ICO" and len(image.ico.sizes()) != 1:
                    raise ValueError("Multi-resolution ICO is unsupported by CopyNCE.")
                with ImageOps.exif_transpose(image) as oriented, oriented.convert("RGB") as rgb:
                    size = rgb.size
                    boxes = _boxes(size, tiles)
                    for box in boxes:
                        if box == boxes[0]:
                            views.append(rgb.resize((224, 224), Image.Resampling.BICUBIC))
                        else:
                            with rgb.crop(box) as crop:
                                views.append(crop.resize((224, 224), Image.Resampling.BICUBIC))
        return size, boxes, views
    except BaseException:
        for view in views:
            view.close()
        raise


def _open_cache(location):
    path = Path(location).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("The feature cache cannot be a symbolic link.")
    os.close(os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600))
    conn = sqlite3.connect(path, timeout=30)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS descriptors ("
        "path TEXT NOT NULL, pipeline TEXT NOT NULL, stat_key TEXT NOT NULL, "
        "width INTEGER NOT NULL, height INTEGER NOT NULL, features BLOB NOT NULL, "
        "PRIMARY KEY(path, pipeline))"
    )
    return conn


def _load_cached(conn, path, pipeline, signature, tiles, dimension):
    if conn is None:
        return None
    row = conn.execute(
        "SELECT width, height, features FROM descriptors WHERE path=? AND pipeline=? AND stat_key=?",
        (str(path), pipeline, signature),
    ).fetchone()
    if row is None:
        return None
    width, height, data = row
    if width < 1 or height < 1:
        return None
    boxes = _boxes((width, height), tiles)
    if len(data) != len(boxes) * dimension * 4:
        return None
    features = np.frombuffer(data, dtype="<f4").reshape(len(boxes), dimension)
    if not np.isfinite(features).all():
        return None
    return str(path), (width, height), boxes, features


def _features(files, model, cache, pipeline, tiles, batch_size, max_pixels, report_error, content_hashes=None,
              pixel_hashes=None):
    items = []
    pending_images = []
    pending = []
    cache_hits = set()
    view_total = 0

    def flush():
        if not pending:
            return
        batches = []
        try:
            for start in range(0, len(pending_images), batch_size):
                images = pending_images[start:start + batch_size]
                vectors = np.asarray(model.encode(images), dtype=np.float32)
                if vectors.shape != (len(images), model.dimension) or not np.isfinite(vectors).all():
                    raise ValueError("Descriptor returned invalid features")
                lengths = np.linalg.norm(vectors, axis=1, keepdims=True)
                if np.any(lengths <= 0):
                    raise ValueError("Descriptor returned a zero-length vector")
                batches.append(vectors / lengths)
        finally:
            for image in pending_images:
                image.close()
        all_vectors = np.concatenate(batches)
        offset = 0
        for record, size, boxes, signature in pending:
            vectors = all_vectors[offset:offset + len(boxes)]
            offset += len(boxes)
            items.append((str(record.path), size, boxes, vectors))
            if cache is not None:
                cache.execute("INSERT OR REPLACE INTO descriptors VALUES(?,?,?,?,?,?)",
                              (str(record.path), pipeline, signature, *size,
                               vectors.astype("<f4", copy=False).tobytes()))
        if cache is not None:
            cache.commit()
        pending.clear()
        pending_images.clear()

    for record in files:
        try:
            core.assert_unchanged(record, os.stat(record.path, follow_symlinks=False))
            with core.checked_stream(record) as stream:
                content_id = core.exact_digest(stream)
            if content_hashes is not None:
                content_hashes[str(record.path)] = content_id
            if pixel_hashes is not None:
                if __package__:
                    from .copynce_sn import pixel_sha256
                else:
                    from copynce_sn import pixel_sha256
                pixel_hashes[str(record.path)] = pixel_sha256(record, max_pixels)
            stamp = ":".join(map(str, record.stamp)) + ":" + content_id
            item = _load_cached(cache, record.path, pipeline, stamp, tiles, model.dimension)
            if item is not None:
                if item[1][0] * item[1][1] > max_pixels:
                    raise ValueError("Image exceeds the configured pixel limit.")
                if view_total + len(item[2]) > MAX_VIEWS:
                    report_error(record.path, "ModelLimitError: reached the descriptor view limit; remaining files were not processed")
                    break
                view_total += len(item[2])
                items.append(item)
                cache_hits.add(str(record.path))
                continue
            size, boxes, views = _decode(record, tiles, max_pixels)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            report_error(record.path, str(exc))
            continue
        if view_total + len(views) > MAX_VIEWS:
            for view in views:
                view.close()
            report_error(record.path, "ModelLimitError: reached the descriptor view limit; remaining files were not processed")
            break
        view_total += len(views)
        pending.append((record, size, boxes, stamp))
        pending_images.extend(views)
        if len(pending_images) >= batch_size:
            flush()
    flush()
    return sorted(items, key=lambda item: item[0]), cache_hits


def _retain(heap, limit, items, a, b, scores):
    best = int(scores.argmax())
    i, j = divmod(best, scores.shape[1])
    # When the last block was padded, padded views duplicate the whole image.
    if i >= len(items[a][2]):
        i = 0
    if j >= len(items[b][2]):
        j = 0
    score = float(np.clip(scores.flat[best], -1.0, 1.0))
    row = (score, -a, -b, i, j)
    if len(heap) < limit:
        heapq.heappush(heap, row)
    elif row > heap[0]:
        heapq.heapreplace(heap, row)


def _format_matches(items, heap):
    result = []
    for score, neg_a, neg_b, view_a, view_b in sorted(heap, reverse=True):
        path_a, size_a, boxes_a, _ = items[-neg_a]
        path_b, size_b, boxes_b, _ = items[-neg_b]
        result.append(dict(path_a=path_a, path_b=path_b, score=score, box_a=list(boxes_a[view_a]),
                           box_b=list(boxes_b[view_b]), size_a=list(size_a), size_b=list(size_b)))
    return result


def _match(items, threshold, top_k):
    if len(items) < 2:
        return 0, []
    heap = []
    found = 0
    views = max(len(item[2]) for item in items)
    dim = items[0][3].shape[1]
    vectors = np.empty((len(items), views, dim), dtype=np.float32)
    for index, (_, _, boxes, embeddings) in enumerate(items):
        vectors[index, :len(boxes)] = embeddings
        vectors[index, len(boxes):] = embeddings[0]
    block = min(BLOCK_IMAGES, max(1, 768 // views))
    for a in range(0, len(items), block):
        left = vectors[a:a + block]
        for b in range(a, len(items), block):
            right = vectors[b:b + block]
            products = left.reshape(-1, dim) @ right.reshape(-1, dim).T
            scores = products.reshape(len(left), views, len(right), views).transpose(0, 2, 1, 3)
            best = np.clip(scores.max(axis=(2, 3)), -1.0, 1.0)
            mask = best >= threshold
            if a == b:
                mask = np.triu(mask, k=1)
            found += int(np.count_nonzero(mask))
            if len(heap) == top_k:
                mask &= best >= heap[0][0]
            for i, j in zip(*np.nonzero(mask)):
                _retain(heap, top_k, items, a + int(i), b + int(j), scores[i, j])
    return found, _format_matches(items, heap)


def _match_indexed(items, threshold, top_k, neighbors, faiss):
    if len(items) < 2:
        return 0, 0, []
    counts = [len(item[2]) for item in items]
    owners = np.repeat(np.arange(len(items), dtype=np.int64), counts)
    index = faiss.IndexHNSWFlat(items[0][3].shape[1], 32, faiss.METRIC_INNER_PRODUCT)
    index.hnsw.efConstruction = 100
    k = min(neighbors, len(items) - 1)
    probe = min(len(owners), (k + 1) * max(counts))
    index.hnsw.efSearch = max(64, probe)
    for start in range(0, len(items), BLOCK_IMAGES):
        index.add(np.concatenate([item[3] for item in items[start:start + BLOCK_IMAGES]]))

    # Deduplicate repeated view matches without keeping all edges in RAM.
    spool = sqlite3.connect("")
    heap = []
    assessed = found = 0
    try:
        spool.execute("PRAGMA journal_mode=OFF")
        spool.execute("PRAGMA synchronous=OFF")
        spool.execute("PRAGMA cache_size=-16384")
        spool.execute("CREATE TABLE pairs (a INTEGER,b INTEGER,PRIMARY KEY(a,b)) WITHOUT ROWID")
        batch = max(1, min(256, 262144 // probe))
        for start in range(0, len(items), BLOCK_IMAGES):
            group = items[start:start + BLOCK_IMAGES]
            vectors = np.concatenate([item[3] for item in group])
            query_owners = np.repeat(np.arange(start, start + len(group)), counts[start:start + len(group)])
            for offset in range(0, len(vectors), batch):
                _, hits = index.search(vectors[offset:offset + batch], probe)
                edges = set()
                for owner, row in zip(query_owners[offset:offset + batch], hits):
                    a = int(owner)
                    others = set()
                    for hit in row:
                        if hit < 0:
                            continue
                        b = int(owners[hit])
                        if a == b or b in others:
                            continue
                        others.add(b)
                        edges.add((min(a, b), max(a, b)))
                        if len(others) == k:
                            break
                spool.executemany("INSERT OR IGNORE INTO pairs VALUES(?,?)", sorted(edges))
        for a, b in spool.execute("SELECT a,b FROM pairs ORDER BY a,b"):
            scores = items[a][3] @ items[b][3].T
            assessed += 1
            if float(np.clip(scores.max(), -1.0, 1.0)) >= threshold:
                found += 1
                _retain(heap, top_k, items, a, b, scores)
    finally:
        spool.close()
    return assessed, found, _format_matches(items, heap)


def _load_descriptor(weights, device):
    if __package__:
        from .copynce_model import Descriptor
    else:
        from copynce_model import Descriptor
    return Descriptor(str(weights), device)


def scan(paths: list[Path], *, weights: Path, cache: Path | None, device="auto", batch_size=8,
         tiles=True, threshold=0.75, top_k=100, mode="auto", recursive=True, include_hidden=False,
         extensions=core.DEFAULT_EXTENSIONS, max_pixels=40_000_000, neighbors=32, file_stamps=None,
         profile="fast", matcher_weights=None, sn_index=None, candidate_k=32, matcher_budget=10000,
         local_view_pairs=2, sn_extra_budget=1000, matcher_threshold=.5, source_groups=None) -> dict:
    """Rank distinct pairs using the highest cosine between their requested views."""
    if not math.isfinite(threshold) or not -1 <= threshold <= 1:
        raise ValueError("threshold must be a finite number between -1 and 1")
    for value in (top_k, batch_size, max_pixels, neighbors):
        if type(value) is not int or value < 1:
            raise ValueError("top_k, batch_size, max_pixels and neighbors must be positive integers")
    if top_k > MAX_RETURNED_PAIRS:
        raise ValueError("top_k exceeds the model result safety limit")
    if mode not in ("exact", "indexed", "auto"):
        raise ValueError("mode must be auto, exact or indexed")
    if profile not in {"fast", "enhanced", "deep"}:
        raise ValueError("profile must be fast, enhanced or deep")
    if profile != "fast" and mode == "indexed":
        raise ValueError("Enhanced profiles currently require exact or auto candidate search")
    if profile == "deep" and not tiles:
        raise ValueError("Deep uses local crop ensembling and cannot be combined with whole-only")
    if profile != "fast" and cache is not None:
        if __package__:
            from .copynce_sn import _private_path
        else:
            from copynce_sn import _private_path
        cache = _private_path(cache, exists=False)
    scanner = _collect(paths, recursive, include_hidden, extensions, file_stamps)
    candidate_count = len(paths) if file_stamps is not None else len(scanner.files)
    if len(scanner.files) > MAX_IMAGES:
        scanner.errors.append(dict(path="", operation="limit", error_type="ModelLimitError",
                                   message="Reached the model image limit; remaining files were not processed"))
        scanner.files = scanner.files[:MAX_IMAGES]
    model = _load_descriptor(weights, device)

    def report_error(path, message):
        scanner.errors.append(dict(path=str(path), operation="descriptor", message=message))

    variant = f"{model.weights_id}:{PIPELINE}:tiles={int(tiles)}"
    content_hashes = {}
    pixel_hashes = {} if profile != "fast" else None
    conn = _open_cache(cache) if cache is not None else None
    try:
        items, hits = _features(scanner.files, model, conn, variant, tiles, batch_size, max_pixels, report_error,
                                content_hashes=content_hashes, pixel_hashes=pixel_hashes)
    finally:
        if conn is not None:
            conn.close()
    selected = {str(item.path): item for item in scanner.files}

    def unchanged_items(rows):
        accepted = []
        for item in rows:
            record = selected[item[0]]
            try:
                core.assert_unchanged(record, os.stat(record.path, follow_symlinks=False))
            except OSError as exc:
                scanner.error(record.path, "verify", exc)
            else:
                accepted.append(item)
        return accepted

    items = unchanged_items(items)
    n = len(items)
    hits = len(hits.intersection(item[0] for item in items))
    possible = n * (n - 1) // 2
    view_count = sum(len(item[2]) for item in items)
    if profile != "fast":
        if __package__:
            from . import copynce_pipeline
        else:
            import copynce_pipeline
        descriptor_device = model.device
        enhanced = copynce_pipeline.run(items, selected, content_hashes, scanner, model, profile=profile,
                                       matcher_weights=matcher_weights, sn_index=sn_index, candidate_k=candidate_k,
                                       matcher_budget=matcher_budget, local_view_pairs=local_view_pairs,
                                       sn_extra_budget=sn_extra_budget, matcher_threshold=matcher_threshold,
                                       top_k=top_k, device=device, max_pixels=max_pixels,
                                       pixel_hashes=pixel_hashes, source_groups=source_groups, cache=cache)
        return dict(inputs=[str(path) for path in scanner.roots], skipped=dict(scanner.skipped),
                    directory=str(scanner.roots[0]) if len(scanner.roots) == 1 else None,
                    weights_sha256=model.weights_id, dimension=model.dimension, device=descriptor_device,
                    pipeline=PIPELINE, precision="float32", model_id="copynce-descriptor-vits16-224",
                    device_fallbacks=list(model.fallbacks), threshold=threshold, descriptor_threshold_applied=False,
                    top_k=top_k, tiles=tiles, recursive=recursive, mode_requested=mode, neighbors=neighbors,
                    auto_index_views=AUTO_INDEX_VIEWS, fallback=None, view_count=view_count,
                    max_views_per_image=max((len(item[2]) for item in items), default=0), pairs_possible=possible,
                    candidate_file_count=candidate_count, image_count=n,
                    safety_limits=dict(max_images=MAX_IMAGES, max_views=MAX_VIEWS, max_returned_pairs=MAX_RETURNED_PAIRS,
                                       max_candidate_pairs=copynce_pipeline.MAX_CANDIDATE_PAIRS),
                    cache_hits=hits, cache_misses=n - hits, error_count=len(scanner.errors), errors=scanner.errors,
                    coordinate_system="EXIF-oriented pixel [x0,y0,x1,y1), exclusive upper endpoints", **enhanced)
    use_index = mode == "indexed" or (mode == "auto" and view_count > AUTO_INDEX_VIEWS)
    fallback = None
    if use_index:
        try:
            import faiss
        except ImportError as exc:
            if mode == "indexed":
                raise RuntimeError("indexed mode requires faiss-cpu") from exc
            use_index = False
            fallback = "faiss-cpu unavailable; used exact search"
    if use_index:
        assessed, total, rows = _match_indexed(items, threshold, top_k, neighbors, faiss)
    else:
        assessed = possible
        total, rows = _match(items, threshold, top_k)
    # Do not deliver stale candidate paths if files changed during retrieval.
    still_valid = {item[0] for item in unchanged_items(items)}
    rows = [row for row in rows if row["path_a"] in still_valid and row["path_b"] in still_valid]
    return dict(
        inputs=[str(path) for path in scanner.roots], skipped=dict(scanner.skipped),
        directory=str(scanner.roots[0]) if len(scanner.roots) == 1 else None,
        weights_sha256=model.weights_id, dimension=model.dimension, device=model.device,
        pipeline=PIPELINE, precision="float32", model_id="copynce-descriptor-vits16-224",
        device_fallbacks=list(model.fallbacks), threshold=threshold, top_k=top_k, tiles=tiles,
        recursive=recursive, mode_requested=mode, mode_used="indexed" if use_index else "exact",
        neighbors=neighbors, auto_index_views=AUTO_INDEX_VIEWS, fallback=fallback,
        view_count=view_count, max_views_per_image=max((len(item[2]) for item in items), default=0),
        exhaustive=(assessed == possible), pairs_possible=possible, pairs_assessed=assessed,
        count_scope="retrieved_candidates" if use_index else "all_pairs",
        workload="hnsw_candidates_exact_cosine" if use_index else "exact_blockwise_cosine",
        score_type="raw_cosine", coordinate_system="EXIF-oriented pixel [x0,y0,x1,y1), exclusive upper endpoints",
        candidate_file_count=candidate_count,
        safety_limits=dict(max_images=MAX_IMAGES, max_views=MAX_VIEWS, max_returned_pairs=MAX_RETURNED_PAIRS),
        image_count=n, pairs_compared=assessed, pairs_above_threshold=total, pairs_returned=len(rows),
        truncated=total > len(rows), cache_hits=hits, cache_misses=n - hits,
        error_count=len(scanner.errors), errors=scanner.errors, matches=rows,
    )
