"""File selection, cached CopyNCE whole-image/region descriptors and cosine retrieval."""
from __future__ import annotations

from collections import deque
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
import heapq
import hashlib
import json
import math
import os
import sqlite3
import stat
import threading
import warnings
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageFile as PillowImageFile, ImageOps, __version__ as PILLOW_VERSION

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
PIXEL_DIGEST_POLICY = "copynce-oriented-rgb-full-v1-static-8bit-safety-v1"
DECODE_PREFETCH = 2


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


def _validate_decode_header(image, prefix, max_pixels):
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


def _decode_header_size(record, max_pixels):
    with core.checked_stream(record) as stream, warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        prefix = stream.read(26)
        stream.seek(0)
        with Image.open(stream) as image:
            _validate_decode_header(image, prefix, max_pixels)
            return image.size


class _DecodeBudget:
    """Limit concurrent native image pixels, not compressed input bytes."""
    def __init__(self, limit):
        self.limit, self.used, self.stopped = limit, 0, False
        self.condition = threading.Condition()

    @contextmanager
    def claim(self, pixels):
        if type(pixels) is not int or not 0 < pixels <= self.limit:
            raise ValueError("Decoded image pixels exceed the native preparation budget")
        with self.condition:
            while self.used + pixels > self.limit and not self.stopped:
                self.condition.wait()
            if self.stopped:
                raise CancelledError()
            self.used += pixels
        try:
            yield
        finally:
            with self.condition:
                self.used -= pixels
                self.condition.notify_all()

    def stop(self):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()


def _decode(record, tiles, max_pixels, *, view_indices=None, pixel_info=None):
    if pixel_info is not None and not isinstance(pixel_info, dict):
        raise ValueError("pixel_info must be a metadata dictionary")
    views = []
    metadata = None
    try:
        with core.checked_stream(record) as stream, warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            prefix = stream.read(26)
            stream.seek(0)
            with Image.open(stream) as image:
                _validate_decode_header(image, prefix, max_pixels)
                with ImageOps.exif_transpose(image) as oriented, oriented.convert("RGB") as rgb:
                    size = rgb.size
                    boxes = _boxes(size, tiles)
                    indices = range(len(boxes)) if view_indices is None else tuple(view_indices)
                    if any(type(index) is not int or not 0 <= index < len(boxes) for index in indices):
                        raise ValueError("Invalid requested CopyNCE view index.")
                    if pixel_info is not None:
                        if __package__:
                            from .copynce_sn import _rgb_pixel_sha256
                        else:
                            from copynce_sn import _rgb_pixel_sha256
                        metadata = dict(raw_size=image.size, oriented_size=size,
                                        pixel_sha256=_rgb_pixel_sha256(rgb))
                    for index in indices:
                        box = boxes[index]
                        if box == boxes[0]:
                            views.append(rgb.resize((224, 224), Image.Resampling.BICUBIC))
                        else:
                            with rgb.crop(box) as crop:
                                views.append(crop.resize((224, 224), Image.Resampling.BICUBIC))
        if pixel_info is not None:
            pixel_info.update(metadata)
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
    if type(width) is not int or type(height) is not int or width < 1 or height < 1:
        return None
    boxes = _boxes((width, height), tiles)
    if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) != len(boxes) * dimension * 4:
        return None
    try:
        features = np.frombuffer(data, dtype="<f4").reshape(len(boxes), dimension)
    except (TypeError, ValueError, BufferError):
        return None
    if not np.isfinite(features).all():
        return None
    return str(path), (width, height), boxes, features


def _pixel_cache_ready(conn):
    """The optional digest table only lives in an owned private SQLite cache."""
    if conn is None:
        return False
    try:
        database = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
        if database:
            info = os.stat(database, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or os.name != "nt" and
                    (info.st_uid != os.getuid() or info.st_mode & 0o077)):
                return False
        conn.execute("CREATE TABLE IF NOT EXISTS canonical_pixel_digests_v1 ("
                     "path TEXT NOT NULL, pipeline TEXT NOT NULL, stat_key TEXT NOT NULL, "
                     "raw_sha256 TEXT NOT NULL, policy TEXT NOT NULL, "
                     "raw_width INTEGER NOT NULL, raw_height INTEGER NOT NULL, "
                     "width INTEGER NOT NULL, height INTEGER NOT NULL, "
                     "pixel_sha256 TEXT NOT NULL, validation_sha256 TEXT NOT NULL, "
                     "PRIMARY KEY(path,pipeline))")
        return True
    except (OSError, sqlite3.DatabaseError, StopIteration):
        return False  # Optional optimization: retain full original validation.


def _pixel_policy():
    return (PIXEL_DIGEST_POLICY + ":pillow=" + PILLOW_VERSION +
            ":bomb=" + repr(Image.MAX_IMAGE_PIXELS) +
            ":truncated=" + repr(PillowImageFile.LOAD_TRUNCATED_IMAGES) +
            ":modes=" + ",".join(sorted(core.NORMALIZE_MODES)))


def _pixel_header(record, max_pixels, *, oriented=False):
    """Recheck current format and safety without decoding cached RGB pixels.

    PNG getexif can load the image, so only ask for orientation when creating
    new validation evidence. A matching raw SHA binds that evidence thereafter.
    """
    with core.checked_stream(record) as stream, warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        prefix = stream.read(26)
        stream.seek(0)
        with Image.open(stream) as image:
            bits = getattr(image, "tag_v2", {}).get(258, ())
            bits = (bits,) if isinstance(bits, int) else bits
            if (image.width * image.height > max_pixels or getattr(image, "n_frames", 1) != 1 or
                    image.mode not in core.NORMALIZE_MODES or
                    prefix.startswith(b"\x89PNG\r\n\x1a\n") and prefix[24:25] == b"\x10" or
                    bits and max(bits) > 8 or image.format == "ICO" and len(image.ico.sizes()) != 1):
                raise ValueError("SN pixel identities require supported static 8-bit images within safety limits")
            raw = image.size
            if not oriented:
                return raw, None
            orientation = image.getexif().get(274)
            return raw, ((raw[1], raw[0]) if orientation in (5, 6, 7, 8) else raw)


def _pixel_validation_digest(values):
    return hashlib.sha256(json.dumps(list(values), ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _load_pixel_digest(conn, path, pipeline, signature, raw_sha256, policy, raw_size, size):
    try:
        row = conn.execute("SELECT path,pipeline,stat_key,raw_sha256,policy,raw_width,raw_height,width,height,"
                           "pixel_sha256,validation_sha256 FROM canonical_pixel_digests_v1 "
                           "WHERE path=? AND pipeline=?", (str(path), pipeline)).fetchone()
        expected = (str(path), pipeline, signature, raw_sha256, policy, *raw_size, *size)
        if (row is None or row[:9] != expected or any(type(v) is not int for v in row[5:9]) or
                not isinstance(row[9], str) or len(row[9]) != 64 or
                any(c not in "0123456789abcdef" for c in row[9]) or
                row[10] != _pixel_validation_digest(row[:10])):
            return None
        return row[9]
    except (sqlite3.DatabaseError, TypeError, ValueError):
        return None


def _store_pixel_digest(conn, path, pipeline, signature, raw_sha256, policy, raw_size, size, digest):
    values = (str(path), pipeline, signature, raw_sha256, policy, *raw_size, *size, digest)
    try:
        conn.execute("INSERT OR REPLACE INTO canonical_pixel_digests_v1 VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (*values, _pixel_validation_digest(values)))
        return True
    except sqlite3.DatabaseError:
        return False


def _features(files, model, cache, pipeline, tiles, batch_size, max_pixels, report_error, content_hashes=None,
              pixel_hashes=None):
    items = []
    pending_images = []
    pending = []
    cache_hits = set()
    view_total = 0
    pixel_cache = pixel_hashes is not None and _pixel_cache_ready(cache)
    pixel_policy = _pixel_policy() if pixel_hashes is not None else None
    if pixel_hashes is not None:
        if __package__:
            from .copynce_sn import pixel_sha256
        else:
            from copynce_sn import pixel_sha256

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
            pending_images.clear()
        all_vectors = np.concatenate(batches)
        offset = 0
        for record, size, boxes, signature, pixel_digest, raw_size, content_id in pending:
            vectors = all_vectors[offset:offset + len(boxes)]
            offset += len(boxes)
            items.append((str(record.path), size, boxes, vectors))
            if cache is not None:
                cache.execute("INSERT OR REPLACE INTO descriptors VALUES(?,?,?,?,?,?)",
                              (str(record.path), pipeline, signature, *size,
                               vectors.astype("<f4", copy=False).tobytes()))
                if pixel_cache:
                    _store_pixel_digest(cache, record.path, pipeline, signature, content_id,
                                        pixel_policy, raw_size, size, pixel_digest)
        if cache is not None:
            cache.commit()
        pending.clear()
        pending_images.clear()

    budget = _DecodeBudget(max_pixels)
    image_errors = (OSError, ValueError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError)

    def inspect_record(record):
        job = SimpleNamespace(record=record, content_id=None, stamp=None, item=None, pixel_digest=None,
                              raw_size=None, pixel_backfill=False, publish_pixel=False, error=None, future=None,
                              lazy_header=False)
        try:
            core.assert_unchanged(record, os.stat(record.path, follow_symlinks=False))
            with core.checked_stream(record) as stream:
                prefix = stream.read(26)
                stream.seek(0)
                job.content_id = core.exact_digest(stream)
            # Other plugins can load pixels while probing frames/ICO headers.
            # Serialize their header inspection against native worker decodes.
            job.lazy_header = prefix.startswith(b"\x89PNG\r\n\x1a\n") or prefix.startswith(b"\xff\xd8")
            job.stamp = ":".join(map(str, record.stamp)) + ":" + job.content_id
            job.item = _load_cached(cache, record.path, pipeline, job.stamp, tiles, model.dimension)
            if pixel_hashes is not None:
                if job.item is not None and any(type(v) is not int for v in job.item[1]):
                    job.item = None
                if job.item is not None:
                    with nullcontext() if job.lazy_header else budget.claim(max_pixels):
                        job.raw_size, _ = _pixel_header(record, max_pixels)
                    if pixel_cache:
                        job.pixel_digest = _load_pixel_digest(cache, record.path, pipeline, job.stamp, job.content_id,
                                                              pixel_policy, job.raw_size, job.item[1])
                    if job.pixel_digest is None:
                        # Legacy proof backfill shares the native pixel budget
                        # with any already-running miss preparations.
                        with budget.claim(job.raw_size[0] * job.raw_size[1]):
                            job.pixel_digest = pixel_sha256(record, max_pixels)
                            _, oriented_size = _pixel_header(record, max_pixels, oriented=True)
                        if oriented_size != job.item[1]:
                            job.item = None
                        elif pixel_cache:
                            job.pixel_backfill = True
                    job.publish_pixel = True
            if job.item is not None:
                if job.item[1][0] * job.item[1][1] > max_pixels:
                    raise ValueError("Image exceeds the configured pixel limit.")
            elif job.raw_size is None:
                with nullcontext() if job.lazy_header else budget.claim(max_pixels):
                    job.raw_size = _decode_header_size(record, max_pixels)
        except image_errors as exc:
            job.error = exc
        return job

    def prepare_views(job):
        result = None
        metadata = {} if pixel_hashes is not None and job.pixel_digest is None else None
        try:
            with budget.claim(job.raw_size[0] * job.raw_size[1]):
                if metadata is None:
                    result = _decode(job.record, tiles, max_pixels)
                else:
                    result = _decode(job.record, tiles, max_pixels, pixel_info=metadata)
                core.assert_unchanged(job.record, os.stat(job.record.path, follow_symlinks=False))
            return (*result, metadata)
        except BaseException:
            if result is not None:
                for image in result[2]:
                    image.close()
            raise

    jobs, owned = deque(), set()
    source = iter(files)
    pool = None
    exhausted = False
    slots = min(2, max(1, DECODE_PREFETCH))

    def fill_jobs():
        nonlocal pool, exhausted
        while len(jobs) < slots and not exhausted:
            try:
                record = next(source)
            except StopIteration:
                exhausted = True
                break
            job = inspect_record(record)  # SQLite and source hashes stay on this thread.
            if job.error is None and job.item is None:
                if pool is None:
                    pool = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="copynce-decode")
                job.future = pool.submit(prepare_views, job)
                owned.add(job.future)
            jobs.append(job)

    def discard(future):
        try:
            result = future.result()
        except Exception:
            pass
        else:
            for image in result[2]:
                image.close()
        owned.discard(future)

    # All workers inherit and restore the same strict Pillow warning policy.
    # Join them before restoring process-global filters.
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            fill_jobs()
            while jobs:
                job = jobs.popleft()
                record = job.record
                try:
                    core.assert_unchanged(record, os.stat(record.path, follow_symlinks=False))
                    if content_hashes is not None and job.content_id is not None:
                        content_hashes[str(record.path)] = job.content_id
                    if job.publish_pixel:
                        pixel_hashes[str(record.path)] = job.pixel_digest
                    if job.error is not None:
                        raise job.error
                    if job.item is not None:
                        item = job.item
                        if job.pixel_backfill:
                            _store_pixel_digest(cache, record.path, pipeline, job.stamp, job.content_id,
                                                pixel_policy, job.raw_size, item[1], job.pixel_digest)
                        if view_total + len(item[2]) > MAX_VIEWS:
                            report_error(record.path, "ModelLimitError: reached the descriptor view limit; remaining files were not processed")
                            break
                        view_total += len(item[2])
                        items.append(item)
                        cache_hits.add(str(record.path))
                    else:
                        size, boxes, views, metadata = job.future.result()
                        core.assert_unchanged(record, os.stat(record.path, follow_symlinks=False))
                        if metadata is not None:
                            job.pixel_digest, job.raw_size = metadata["pixel_sha256"], metadata["raw_size"]
                            pixel_hashes[str(record.path)] = job.pixel_digest
                        if view_total + len(views) > MAX_VIEWS:
                            discard(job.future)
                            report_error(record.path, "ModelLimitError: reached the descriptor view limit; remaining files were not processed")
                            break
                        view_total += len(views)
                        pending.append((record, size, boxes, job.stamp, job.pixel_digest, job.raw_size, job.content_id))
                        pending_images.extend(views)
                        owned.discard(job.future)  # Ownership has moved to the model batch.
                except image_errors as exc:
                    if job.future in owned:
                        discard(job.future)
                    report_error(record.path, str(exc))
                fill_jobs()
                if len(pending_images) >= batch_size:
                    flush()
            flush()
        finally:
            budget.stop()
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
            for future in tuple(owned):
                try:
                    discard(future)
                except BaseException:
                    pass  # Preserve the active interruption/error, not a future beyond scope.
            for image in pending_images:
                image.close()
            pending_images.clear()
    if pixel_cache:
        cache.commit()  # Backfill successful cache-hit rows together, not per pair.
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
