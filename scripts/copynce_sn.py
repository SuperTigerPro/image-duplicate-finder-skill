"""Official query-only score normalization with a private, versioned background index."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import warnings
import zipfile

import numpy as np
from PIL import Image, ImageOps

if __package__:
    from . import copynce_engine as engine
else:
    import copynce_engine as engine

GLOBAL_PIPELINE = "copynce-static-rgb224-exif-v4-f32:global-only"
INDEX_TYPE = "copynce-sn-global-background"
MAX_AUXILIARY_IMAGES = 10000
MAX_INDEX_BYTES = 300 * 1024 * 1024
HEX = re.compile(r"[0-9a-f]{64}\Z")


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def _digest_file(path):
    record = engine.core.ImageFile(path, path.stat().st_size, engine.core.signature(path.stat()))
    with engine.core.checked_stream(record) as stream:
        return engine.core.exact_digest(stream)


def _rgb_pixel_sha256(rgb):
    """Hash already validated oriented RGB pixels without modifying the image."""
    if not isinstance(rgb, Image.Image) or rgb.mode != "RGB" or min(rgb.size) < 1:
        raise ValueError("Pixel identity requires a nonempty RGB PIL image")
    digest = hashlib.sha256(b"copynce-oriented-rgb-full-v1\0")
    digest.update(struct.pack(">QQ", *rgb.size))
    for top in range(0, rgb.height, 128):
        with rgb.crop((0, top, rgb.width, min(top + 128, rgb.height))) as stripe:
            digest.update(stripe.tobytes())
    return digest.hexdigest()


def pixel_sha256(record, max_pixels=40000000):
    """Full oriented RGB identity survives metadata removal and lossless cloud PNG transfer."""
    with engine.core.checked_stream(record) as stream, warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        prefix = stream.read(26)
        stream.seek(0)
        with Image.open(stream) as image:
            bits = getattr(image, "tag_v2", {}).get(258, ())
            bits = (bits,) if isinstance(bits, int) else bits
            if (image.width * image.height > max_pixels or getattr(image, "n_frames", 1) != 1 or
                    image.mode not in engine.core.NORMALIZE_MODES or
                    prefix.startswith(b"\x89PNG\r\n\x1a\n") and prefix[24:25] == b"\x10" or
                    bits and max(bits) > 8 or image.format == "ICO" and len(image.ico.sizes()) != 1):
                raise ValueError("SN pixel identities require supported static 8-bit images within safety limits")
            with ImageOps.exif_transpose(image) as oriented, oriented.convert("RGB") as rgb:
                result = _rgb_pixel_sha256(rgb)
    return result


def _private_path(value, *, exists=True):
    path = Path(value).expanduser().absolute()
    missing = []
    ancestor = path
    while not ancestor.exists() and not ancestor.is_symlink():
        missing.append(ancestor.name)
        ancestor = ancestor.parent
    ancestor = engine.core.canonical_input(ancestor)
    path = ancestor.joinpath(*reversed(missing))
    skill = Path(__file__).resolve().parents[1]
    if path == skill or skill in path.parents or any((p / ".git").exists() for p in (path, *path.parents)):
        raise ValueError("SN resources must be outside the Skill and all Git worktrees")
    if exists:
        path = engine.core.canonical_input(path)
    return path


def _private_regular(path, *, directory=False):
    info = os.stat(path, follow_symlinks=False)
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if path.is_symlink() or not expected(info.st_mode):
        raise ValueError("SN resource must be a private regular file or directory")
    if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("SN resources must be owned by the current user and private")


def _index_paths(value):
    path = _private_path(value)
    directory = path if path.is_dir() else path.parent
    if not path.is_dir() and path.name != "manifest.json":
        raise ValueError("SN index must be a directory or its manifest.json")
    _private_regular(directory, directory=True)
    manifest_path, data_path = directory / "manifest.json", directory / "features.npz"
    for file in (manifest_path, data_path):
        engine.core.canonical_input(file)
        _private_regular(file)
    if manifest_path.stat().st_size > 65536 or data_path.stat().st_size > MAX_INDEX_BYTES:
        raise ValueError("SN resource exceeds the supported size limit")
    return manifest_path, data_path


def _fingerprint(manifest):
    return hashlib.sha256(_canonical_json({k: v for k, v in manifest.items() if k != "fingerprint"})).hexdigest()


def inspect_index(path, *, expected_weights_sha256, expected_pipeline=GLOBAL_PIPELINE):
    """Check provenance and stream the data checksum without decompressing vectors."""
    manifest_path, data_path = _index_paths(path)
    record = engine.core.ImageFile(manifest_path, manifest_path.stat().st_size,
                                   engine.core.signature(manifest_path.stat()))
    with engine.core.checked_stream(record) as stream:
        manifest = json.loads(stream.read().decode("utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("index_type") != INDEX_TYPE:
        raise ValueError("Unsupported SN index schema")
    if manifest.get("fingerprint") != _fingerprint(manifest):
        raise ValueError("SN manifest fingerprint mismatch")
    if manifest.get("weights_sha256") != expected_weights_sha256 or manifest.get("pipeline") != expected_pipeline:
        raise ValueError("SN background model or preprocessing differs from the query model")
    if manifest.get("precision") != "float32" or manifest.get("view_policy") != "whole-image-only":
        raise ValueError("SN requires float32 global-image descriptors")
    if (type(manifest.get("images")) is not int or not 5 <= manifest["images"] <= MAX_AUXILIARY_IMAGES or
            type(manifest.get("dimension")) is not int or not 1 <= manifest["dimension"] <= 4096):
        raise ValueError("Invalid SN background dimensions")
    if not HEX.fullmatch(str(manifest.get("source_sha256", ""))) or not HEX.fullmatch(str(manifest.get("npz_sha256", ""))):
        raise ValueError("Invalid SN provenance checksum")
    if _digest_file(data_path) != manifest["npz_sha256"]:
        raise ValueError("SN background checksum mismatch")
    return manifest


@dataclass(frozen=True)
class AuxiliaryIndex:
    manifest: dict
    vectors: np.ndarray
    image_sha256: tuple[str, ...]
    source_group_sha256: tuple[str, ...]
    pixel_sha256: tuple[str, ...]

    @property
    def fingerprint(self):
        return self.manifest["fingerprint"]


def load_index(path, *, expected_weights_sha256, expected_pipeline=GLOBAL_PIPELINE):
    manifest = inspect_index(path, expected_weights_sha256=expected_weights_sha256,
                             expected_pipeline=expected_pipeline)
    _, data_path = _index_paths(path)
    record = engine.core.ImageFile(data_path, data_path.stat().st_size, engine.core.signature(data_path.stat()))
    with engine.core.checked_stream(record) as stream:
        if engine.core.exact_digest(stream) != manifest["npz_sha256"]:
            raise ValueError("SN background changed while loading")
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            names = archive.namelist()
            if (len(names) != 4 or set(names) != {"features.npy", "image_sha256.npy", "source_group_sha256.npy", "pixel_sha256.npy"} or
                    sum(info.file_size for info in archive.infolist()) > MAX_INDEX_BYTES):
                raise ValueError("Unexpected or oversized SN index arrays")
            # Reject hostile shape headers before np.load can allocate their claimed arrays.
            for name in names:
                with archive.open(name) as array:
                    version = np.lib.format.read_magic(array)
                    if version == (1, 0):
                        shape, fortran, dtype = np.lib.format.read_array_header_1_0(array)
                    elif version == (2, 0):
                        shape, fortran, dtype = np.lib.format.read_array_header_2_0(array)
                    else:
                        raise ValueError("Unsupported SN array encoding")
                    expected_shape = ((manifest["images"], manifest["dimension"]) if name == "features.npy"
                                      else (manifest["images"],))
                    expected_dtype = np.dtype("float32") if name == "features.npy" else np.dtype("U64")
                    if (shape != expected_shape or dtype != expected_dtype or fortran or
                            array.tell() + math.prod(shape) * dtype.itemsize != archive.getinfo(name).file_size):
                        raise ValueError("SN array header does not match its manifest")
        stream.seek(0)
        with np.load(stream, allow_pickle=False) as stored:
            vectors = stored["features"]
            hashes, groups, pixels = stored["image_sha256"], stored["source_group_sha256"], stored["pixel_sha256"]
    n, dim = manifest["images"], manifest["dimension"]
    if (vectors.dtype != np.dtype("float32") or vectors.shape != (n, dim) or
            any(a.dtype.kind != "U" or a.shape != (n,) for a in (hashes, groups, pixels))):
        raise ValueError("SN arrays do not match the manifest")
    if not np.isfinite(vectors).all() or not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=2e-5):
        raise ValueError("SN background contains invalid or unnormalized features")
    hashes, groups, pixels = tuple(hashes.tolist()), tuple(groups.tolist()), tuple(pixels.tolist())
    if (any(not HEX.fullmatch(value) for value in (*hashes, *groups, *pixels)) or len(set(hashes)) != n or
            len(set(pixels)) != n or
            hashlib.sha256(_canonical_json(list(zip(hashes, groups, pixels)))).hexdigest() != manifest["source_sha256"]):
        raise ValueError("SN source identities do not match their provenance")
    vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors.flags.writeable = False
    return AuxiliaryIndex(manifest, vectors, hashes, groups, pixels)


def compute_bias(query_vectors, index, *, excluded_sha256=(), excluded_source_groups=(), excluded_pixel_sha256=(),
                 alpha=1.0, start=1, end=5, block_size=128, auxiliary_block=4096):
    """Exact blockwise official [start:end] query bias; no reference-dependent term."""
    if not math.isfinite(alpha) or alpha < 0 or type(start) is not int or type(end) is not int or not 0 <= start < end:
        raise ValueError("Invalid SN alpha or half-open neighbor slice")
    if type(block_size) is not int or block_size < 1 or type(auxiliary_block) is not int or auxiliary_block < 1:
        raise ValueError("SN block sizes must be positive integers")
    query = np.asarray(query_vectors, dtype=np.float32)
    if query.ndim != 2 or query.shape[1] != index.vectors.shape[1] or not np.isfinite(query).all():
        raise ValueError("SN query features have invalid dimensions or values")
    norms = np.linalg.norm(query, axis=1, keepdims=True)
    if np.any(norms <= 0):
        raise ValueError("SN query contains a zero vector")
    query = query / norms
    excluded, excluded_groups, excluded_pixels = set(excluded_sha256), set(excluded_source_groups), set(excluded_pixel_sha256)
    accepted = np.array([h not in excluded and g not in excluded_groups and p not in excluded_pixels
                         for h, g, p in zip(index.image_sha256, index.source_group_sha256, index.pixel_sha256)], dtype=bool)
    count = int(accepted.sum())
    if count < end:
        raise ValueError("SN requires at least five disjoint background images after overlap exclusion")
    kept_ids = np.flatnonzero(accepted)
    bias = np.empty(len(query), dtype=np.float32)
    for offset in range(0, len(query), block_size):
        q = query[offset:offset + block_size]
        best = np.empty((len(q), 0), dtype=np.float32)
        for position in range(0, count, auxiliary_block):
            scores = q @ index.vectors[kept_ids[position:position + auxiliary_block]].T
            combined = np.concatenate((best, scores), axis=1)
            keep = min(end, combined.shape[1])
            best = np.partition(combined, combined.shape[1] - keep, axis=1)[:, -keep:]
        best.sort(axis=1)
        bias[offset:offset + len(q)] = alpha * np.flip(best, axis=1)[:, start:end].mean(axis=1)
    return bias, dict(profile="official-query-only-global-sn", alpha=float(alpha), start=start, end=end,
                      index_fingerprint=index.fingerprint, background_images=index.manifest["images"],
                      background_images_used=count, overlap_excluded=index.manifest["images"] - count,
                      overlap_policy="exact content and oriented full-resolution RGB pixel SHA-256; optional supplied source groups",
                      source_group_provenance=index.manifest.get("source_groups_provided", False),
                      source_group_exclusion_applied=bool(excluded_groups), bias_includes_alpha=True)


def query_bias(query_vectors, index, **kwargs):
    return compute_bias(query_vectors, index, **kwargs)[0]


def build_index(paths, weights, output_dir, *, device="auto", batch_size=8, max_pixels=40000000,
                recursive=True, include_hidden=False, extensions=engine.core.DEFAULT_EXTENSIONS,
                source_groups=None, provenance=None):
    """Build a private global-only background; never store paths in its manifest."""
    target = _private_path(output_dir, exists=False)
    if target.exists():
        raise ValueError("SN output already exists; choose a new private directory")
    scanner = engine._collect(list(map(Path, paths)), recursive, include_hidden, extensions, None)
    if scanner.errors or not 5 <= len(scanner.files) <= MAX_AUXILIARY_IMAGES:
        raise ValueError("SN background must contain between five and the supported maximum readable images")
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("SN batch size must be positive")
    descriptor = engine._load_descriptor(weights, device)
    features, hashes, groups, pixels = [], [], [], []
    seen, seen_pixels = set(), set()
    pending = []

    def flush():
        if not pending:
            return
        try:
            encoded = np.asarray(descriptor.encode(pending), dtype=np.float32)
            if encoded.shape != (len(pending), descriptor.dimension) or not np.isfinite(encoded).all():
                raise ValueError("SN descriptor returned invalid features")
            norm = np.linalg.norm(encoded, axis=1, keepdims=True)
            if np.any(norm <= 0):
                raise ValueError("SN descriptor returned a zero vector")
            features.append(encoded / norm)
        finally:
            for image in pending:
                image.close()
            pending.clear()

    try:
        for record in scanner.files:
            with engine.core.checked_stream(record) as stream:
                digest = engine.core.exact_digest(stream)
            if digest in seen:
                continue
            seen.add(digest)
            pixel = pixel_sha256(record, max_pixels)
            if pixel in seen_pixels:
                continue
            seen_pixels.add(pixel)
            _, _, views = engine._decode(record, False, max_pixels)
            hashes.append(digest)
            pixels.append(pixel)
            label = None if source_groups is None else source_groups.get(str(record.path))
            groups.append(digest if label is None else hashlib.sha256(str(label).encode()).hexdigest())
            pending.extend(views)
            if len(pending) >= batch_size:
                flush()
        flush()
        for record in scanner.files:
            engine.core.assert_unchanged(record, os.stat(record.path, follow_symlinks=False))
    finally:
        for image in pending:
            image.close()
    if len(hashes) < 5:
        raise ValueError("SN needs at least five unique background images")
    vectors = np.concatenate(features).astype(np.float32, copy=False)
    if vectors.nbytes + len(hashes) * 768 > MAX_INDEX_BYTES:
        raise ValueError("SN background exceeds the supported index size")
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        data = target / "features.npz"
        with data.open("xb") as stream:
            os.chmod(data, 0o600)
            np.savez(stream, features=vectors, image_sha256=np.asarray(hashes, dtype="U64"),
                     source_group_sha256=np.asarray(groups, dtype="U64"), pixel_sha256=np.asarray(pixels, dtype="U64"))
        manifest = dict(schema_version=1, index_type=INDEX_TYPE, created_at=datetime.now(timezone.utc).isoformat(),
                        model_id="copynce-descriptor-vits16-224", weights_sha256=descriptor.weights_id,
                        pipeline=GLOBAL_PIPELINE, precision="float32", dimension=descriptor.dimension,
                        images=len(hashes), image_count=len(hashes), view_policy="whole-image-only", npz_sha256=_digest_file(data),
                        source_sha256=hashlib.sha256(_canonical_json(list(zip(hashes, groups, pixels)))).hexdigest(),
                        source_groups_provided=source_groups is not None,
                        provenance={} if provenance is None else provenance)
        manifest["fingerprint"] = _fingerprint(manifest)
        document = _canonical_json(manifest) + b"\n"
        if len(document) > 65536:
            raise ValueError("SN provenance must be a small JSON object")
        with (target / "manifest.json").open("xb") as stream:
            os.chmod(target / "manifest.json", 0o600)
            stream.write(document)
        return inspect_index(target, expected_weights_sha256=descriptor.weights_id)
    except BaseException:
        for name in ("features.npz", "manifest.json"):
            (target / name).unlink(missing_ok=True)
        target.rmdir()
        raise
