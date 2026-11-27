# SPDX-License-Identifier: Apache-2.0
# Adapted from https://github.com/eddielyc/CopyNCE; see third_party/CopyNCE-LICENSE.
# The portable runtime, strict checkpoint checks and view metadata are modifications.
"""Portable FP32 inference for the official ViT-S/16 CopyNCE Matcher.

Inputs are decoded RGB images, already oriented by the caller. The official
224-pixel bicubic/ImageNet preprocessing is applied without modifying inputs.
Scores are the classification head's sigmoid output, not calibrated duplicate
probabilities. The official-code LCE helper uses 35 star comparisons, whereas
the paper describes a different, 260-comparison Cartesian ensemble.
"""
from __future__ import annotations

from collections.abc import Mapping
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat

import numpy as np
from PIL import Image
import torch
from torch import nn

if __package__:
    from .copynce_model import Descriptor, _Block, _PatchEmbedding, _full_precision
else:
    from copynce_model import Descriptor, _Block, _PatchEmbedding, _full_precision

OFFICIAL_SHA256 = "dd8dea1c345039bbda303c42457aa3a0389d46fb37ea39506bfb1faddedda1df"
OFFICIAL_BYTES = 86_733_215
OFFICIAL_FILE_ID = "1g6bAUTfyEXR20bUbEtHH_i5NBfrbDYHq"
OFFICIAL_DOWNLOAD_URL = "https://drive.usercontent.google.com/download"
OFFICIAL_SOURCE_URL = "https://github.com/eddielyc/CopyNCE"
CHECKPOINT = "matcher_vits_224.pth.tar"
MODEL_ID = "copynce-matcher-vits16-224"
PREPROCESS_ID = "copynce-matcher-rgb224-bicubic-imagenet-v1-f32"
OFFICIAL_LCE_PROFILE = "official-code-lce-star35-v1"
TOKEN_CACHE_POLICY = "copynce-matcher-token-width-slot-f32-v1"

# The verified public checkpoint has model + epoch. Its model contains exactly
# 157 architecture tensors and NO training-only tensors to discard. Keep this
# explicit empty whitelist; unexpected model keys must fail strict loading.
TRAINING_ONLY_STATE_KEYS = frozenset()
TRAINING_METADATA_KEYS = frozenset({"epoch"})


class _MatcherEncoder(nn.Module):
    def __init__(self):
        super().__init__()
        self.patch_embed = _PatchEmbedding()
        self.cls_token = nn.Parameter(torch.zeros(1, 1, 384))
        self.pos_embed = nn.Parameter(torch.zeros(1, 197, 384))
        self.blocks = nn.Sequential(*(_Block() for _ in range(8)))

    def forward(self, images):
        patches = self.patch_embed(images)
        prefix = self.cls_token.expand(images.shape[0], -1, -1)
        return self.blocks(torch.cat((prefix, patches), dim=1) + self.pos_embed)


class _MatcherFusion(nn.Module):
    def __init__(self):
        super().__init__()
        self.cls_token = nn.Parameter(torch.zeros(1, 1, 384))
        self.blocks = nn.Sequential(*(_Block() for _ in range(4)))
        self.norm = nn.LayerNorm(384, eps=1e-4)

    def forward(self, query_tokens, reference_tokens):
        prefix = self.cls_token.expand(query_tokens.shape[0], -1, -1)
        tokens = torch.cat((prefix, query_tokens, reference_tokens), dim=1)
        return self.norm(self.blocks(tokens))[:, 0]


class _MatcherClassificationHead(nn.Module):
    def __init__(self):
        super().__init__()
        # This second normalization is present in the official depth=0 head.
        self.norm = nn.LayerNorm(384, eps=1e-4)
        self.classifier = nn.Linear(384, 1)

    def forward(self, fused_cls):
        return torch.sigmoid(self.classifier(self.norm(fused_cls))).squeeze(-1)


class _MatcherAuxiliaryHead(nn.Module):
    """Retain the published auxiliary head tensors for fully strict loading."""
    def __init__(self):
        super().__init__()
        self.norm = nn.LayerNorm(384, eps=1e-4)


class _CopyMatcher(nn.Module):
    def __init__(self):
        super().__init__()
        self.encoder = _MatcherEncoder()
        self.fusion = _MatcherFusion()
        self.heads = nn.ModuleDict({"cls_head": _MatcherClassificationHead(),
                                    "fusion_head": _MatcherAuxiliaryHead()})

    def forward(self, queries, references):
        fused_cls = self.fusion(self.encoder(queries), self.encoder(references))
        return self.heads["cls_head"](fused_cls)


def _checkpoint_state(weights):
    path = Path(weights).expanduser()
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != OFFICIAL_BYTES:
            raise ValueError("CopyNCE Matcher checkpoint size mismatch.")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        if digest.hexdigest() != OFFICIAL_SHA256:
            raise ValueError("CopyNCE Matcher checkpoint SHA-256 mismatch.")
        stream.seek(0)
        record = torch.load(stream, map_location="cpu", weights_only=True)
        after = os.fstat(stream.fileno())
        stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if stamp(before) != stamp(after):
            raise ValueError("CopyNCE Matcher checkpoint changed while loading.")
    if not isinstance(record, Mapping) or not isinstance(record.get("model"), Mapping):
        raise ValueError("Expected the published CopyNCE Matcher checkpoint dictionary.")
    if set(record) - {"model"} - TRAINING_METADATA_KEYS:
        raise ValueError("Unexpected metadata in the CopyNCE Matcher checkpoint.")
    cleaned = {}
    for key, value in record["model"].items():
        if not isinstance(key, str) or not isinstance(value, torch.Tensor):
            raise ValueError("Expected Matcher model tensors with string keys.")
        key = key.removeprefix("module.")
        if key in TRAINING_ONLY_STATE_KEYS:
            continue
        if key in cleaned:
            raise ValueError("Duplicate Matcher state key after prefix normalization.")
        cleaned[key] = value
    return cleaned, digest.hexdigest()


def _rgb_array(image):
    if isinstance(image, Image.Image):
        if image.mode != "RGB" or min(image.size) < 1:
            raise ValueError("Matcher inputs must be decoded RGB images.")
        if image.size == (224, 224):
            return np.asarray(image)
        with image.resize((224, 224), Image.Resampling.BICUBIC) as resized:
            return np.asarray(resized).copy()
    array = np.asarray(image)
    if array.dtype != np.uint8 or array.ndim != 3 or array.shape[2] != 3 or min(array.shape[:2]) < 1:
        raise ValueError("Matcher inputs must be RGB uint8 images.")
    if array.shape[:2] == (224, 224):
        return array
    with Image.fromarray(array) as source, source.resize((224, 224), Image.Resampling.BICUBIC) as resized:
        return np.asarray(resized).copy()


@dataclass(frozen=True)
class MatcherView:
    image: Image.Image
    box: tuple[int, int, int, int]
    rotation: int = 0


def official_lce_views(image, role="query", *, resize_to_224=False):
    """Return owned full-resolution views; caller must close every view.image.

    This reproduces core.data.datasets.copy_dataset, including duplicate
    center crops and query-only rotations. Box coordinates precede rotation.
    Set resize_to_224 to resize and release each native view immediately rather
    than retaining many full-resolution rotations for a large source image.
    """
    if role not in {"query", "reference"}:
        raise ValueError("LCE role must be query or reference.")
    if type(resize_to_224) is not bool:
        raise ValueError("resize_to_224 must be a boolean.")
    if not isinstance(image, Image.Image) or image.mode != "RGB" or min(image.size) < 3:
        raise ValueError("Official LCE requires an RGB image at least 3 by 3 pixels.")
    w, h = image.size
    boxes = [(0, 0, w, h),
             (w // 3, h // 3, 2 * w // 3, 2 * h // 3),
             (w // 6, h // 6, w // 2, h // 2),
             (w // 2, h // 6, 5 * w // 6, h // 2),
             (w // 6, h // 2, w // 2, 5 * h // 6),
             (w // 2, h // 2, 5 * w // 6, 5 * h // 6),
             (0, 0, w // 2, h // 2), (w // 2, 0, w, h // 2),
             (0, h // 2, w // 2, h), (w // 2, h // 2, w, h)]
    if role == "query":
        boxes.extend((col * w // 3, row * h // 3, (col + 1) * w // 3, (row + 1) * h // 3)
                     for row in range(3) for col in range(3))
    views = []

    def append_view(native, box, rotation=0):
        try:
            if resize_to_224:
                resized = native.resize((224, 224), Image.Resampling.BICUBIC)
                native.close()
                native = resized
            views.append(MatcherView(native, box, rotation))
        except BaseException:
            native.close()
            raise

    try:
        for box in boxes:
            append_view(image.crop(box), box)
        if role == "query":
            for degrees in range(45, 360, 45):
                rotated = image.rotate(degrees, expand=True, resample=Image.Resampling.BICUBIC)
                append_view(rotated, (0, 0, w, h), degrees)
        return views
    except BaseException:
        for view in views:
            view.image.close()
        raise


def official_lce_pair_indices(query_count=26, reference_count=10):
    if any(type(n) is not int or n < 1 for n in (query_count, reference_count)):
        raise ValueError("LCE view counts must be positive integers.")
    return tuple((i, 0) for i in range(query_count)) + tuple((0, j) for j in range(1, reference_count))


class Matcher:
    precision = "float32"
    model_id = MODEL_ID
    preprocess_id = PREPROCESS_ID

    def __init__(self, weights, device="auto"):
        self.requested_device = device
        if device == "auto":
            if torch.cuda.is_available():
                device = "cuda"
            elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                device = "mps"
            else:
                device = "cpu"
        target = torch.device(device)
        if target.type not in {"cpu", "cuda", "mps"}:
            raise ValueError("Choose auto, cpu, mps or a CUDA device.")
        state, self.weights_id = _checkpoint_state(weights)
        self._net = _CopyMatcher().to(dtype=torch.float32).eval()
        self._net.load_state_dict(state, strict=True)
        self._mean = torch.tensor([0.485, 0.456, 0.406], dtype=torch.float32).view(1, 3, 1, 1)
        self._std = torch.tensor([0.229, 0.224, 0.225], dtype=torch.float32).view(1, 3, 1, 1)
        self.device = "cpu"
        self.fallbacks = []
        self.max_batch = 8
        self._token_cache = None  # Opt-in, run-local; checkpoint/model state is unchanged.
        self._token_cache_cuda_auto = False
        if target.type != "cpu":
            try:
                self._move(target)
                dummy = np.zeros((224, 224, 3), dtype=np.uint8)
                with torch.inference_mode():
                    score = self._forward([dummy], [dummy])
                if score.shape != (1,) or not np.isfinite(score).all() or not (0 <= score[0] <= 1):
                    raise RuntimeError("Invalid Matcher output from the device probe.")
            except (RuntimeError, AssertionError, NotImplementedError) as exc:
                exc.__traceback__ = None
                self._move("cpu")
                self.fallbacks.append(f"{target} probe failed; using CPU")
                self._release_accelerator(str(target))

    def _move(self, device):
        if getattr(self, "_token_cache_cuda_auto", False) and torch.device(device).type != "cuda":
            self.disable_token_cache()
        else:
            self.clear_token_cache("device_move")
        self._net.to(device)
        self._mean = self._mean.to(device)
        self._std = self._std.to(device)
        self.device = str(device)

    _release_accelerator = staticmethod(Descriptor._release_accelerator)

    def enable_token_cache(self, max_bytes=128 * 1024 * 1024, storage="device"):
        """Cache FP32 encoder tokens, preserving actual batch width and slot.

        This is single-owner inference state, disabled by default. A partial
        miss still encodes the entire original batch; only all-hit batches
        skip encoding. CPU storage copies bits without quantization.
        """
        if type(max_bytes) is not int or max_bytes < 1 or storage not in {"device", "cpu"}:
            raise ValueError("Use a positive token-cache byte limit and device or cpu storage.")
        self.clear_token_cache("reconfigured")
        self._token_cache = OrderedDict()
        self._token_cache_max_bytes = max_bytes
        self._token_cache_storage = storage
        self._token_cache_bytes = 0
        self._token_cache_context = None
        self._token_cache_stats = dict(batch_hits=0, batch_misses=0, rows_reused=0,
                                       rows_encoded=0, evictions=0, clears=0)
        self._token_cache_last_clear = None
        self._token_cache_cuda_auto = False  # Explicit calls are a manual opt-in.

    def clear_token_cache(self, reason="explicit"):
        cache = getattr(self, "_token_cache", None)
        if cache is not None:
            cache.clear()
        self._token_cache_bytes = 0
        self._token_cache_context = None
        stats = getattr(self, "_token_cache_stats", None)
        if stats is not None:
            stats["clears"] += 1
            self._token_cache_last_clear = reason

    def disable_token_cache(self):
        self.clear_token_cache("disabled")
        self._token_cache = None
        self._token_cache_cuda_auto = False

    @property
    def token_cache_info(self):
        return dict(enabled=getattr(self, "_token_cache", None) is not None,
                    cuda_auto=getattr(self, "_token_cache_cuda_auto", False),
                    policy=TOKEN_CACHE_POLICY, run_local=True,
                    storage=getattr(self, "_token_cache_storage", None),
                    max_bytes=getattr(self, "_token_cache_max_bytes", 0),
                    current_bytes=getattr(self, "_token_cache_bytes", 0),
                    entries=len(getattr(self, "_token_cache", None) or ()),
                    last_clear=getattr(self, "_token_cache_last_clear", None),
                    **getattr(self, "_token_cache_stats", {}))

    def _encode_cached(self, batch, images):
        cache = self._token_cache
        context = (self.weights_id, self.preprocess_id, self.precision, self.device,
                   str(batch.device), str(batch.dtype), len(images), self.max_batch)
        if context != self._token_cache_context:
            self.clear_token_cache("inference_context_changed")
            self._token_cache_context = context
        if batch.dtype != torch.float32 or self._net.training or self._net.encoder.training:
            self.clear_token_cache("non_fp32_or_training")
            return self._net.encoder(batch)
        keys = [(context, slot, hashlib.sha256(_rgb_array(image).tobytes(order="C")).digest())
                for slot, image in enumerate(images)]
        if all(key in cache for key in keys):
            cached = [cache[key] for key in keys]
            for key in keys:
                cache.move_to_end(key)
            self._token_cache_stats["batch_hits"] += 1
            self._token_cache_stats["rows_reused"] += len(images)
            return torch.stack(cached).to(device=batch.device, dtype=torch.float32)
        # Never encode a width-1/2 subset of an original width-3 batch.
        encoded = self._net.encoder(batch)
        self._token_cache_stats["batch_misses"] += 1
        self._token_cache_stats["rows_encoded"] += len(images)
        if encoded.dtype != torch.float32 or encoded.shape[0] != len(images):
            self.clear_token_cache("unexpected_encoder_layout")
            return encoded
        row_bytes = encoded[0].numel() * encoded.element_size()
        if row_bytes > self._token_cache_max_bytes:
            return encoded
        for key, token in zip(keys, encoded):
            previous = cache.pop(key, None)
            if previous is not None:
                self._token_cache_bytes -= previous.numel() * previous.element_size()
                del previous
            while cache and self._token_cache_bytes + row_bytes > self._token_cache_max_bytes:
                _, evicted = cache.popitem(last=False)
                self._token_cache_bytes -= evicted.numel() * evicted.element_size()
                self._token_cache_stats["evictions"] += 1
                del evicted
            target = "cpu" if self._token_cache_storage == "cpu" else batch.device
            # Independent row storage makes the byte limit bound retained tensors.
            cache[key] = token.detach().to(device=target, dtype=torch.float32).clone()
            self._token_cache_bytes += row_bytes
        return encoded

    def _batch(self, images):
        batch = torch.from_numpy(np.stack([_rgb_array(image) for image in images]))
        batch = batch.permute(0, 3, 1, 2).to(device=self.device, dtype=torch.float32)
        return batch.div_(255.0).sub_(self._mean).div_(self._std)

    def _forward(self, queries, references):
        query_batch, reference_batch = self._batch(queries), self._batch(references)
        with _full_precision(self.device):
            if getattr(self, "_token_cache", None) is None:
                scores = self._net(query_batch, reference_batch)
            else:
                query_tokens = self._encode_cached(query_batch, queries)
                reference_tokens = self._encode_cached(reference_batch, references)
                scores = self._net.heads["cls_head"](self._net.fusion(query_tokens, reference_tokens))
            return scores.to(device="cpu", dtype=torch.float32).numpy()

    @torch.inference_mode()
    def score_pairs(self, query_images, reference_images):
        if len(query_images) != len(reference_images):
            raise ValueError("Matcher query and reference batches must have equal lengths.")
        if not len(query_images):
            return np.empty((0,), dtype=np.float32)
        offset, chunks = 0, []
        while offset < len(query_images):
            width = min(self.max_batch or len(query_images), len(query_images) - offset)
            try:
                scores = self._forward(query_images[offset:offset + width], reference_images[offset:offset + width])
            except (RuntimeError, NotImplementedError) as exc:
                self.clear_token_cache("inference_exception")
                if self.device == "cpu":
                    raise
                previous = self.device
                message = str(exc).lower()
                exc.__traceback__ = None
                if width > 1 and ("out of memory" in message or "outofmemory" in message):
                    self.max_batch = max(1, width // 2)
                    self.fallbacks.append(f"{self.device} out of memory; batch size {self.max_batch}")
                else:
                    self._move("cpu")
                    self.fallbacks.append(f"{previous} inference failed; using CPU")
                self._release_accelerator(previous)
                continue
            except BaseException:
                self.clear_token_cache("inference_exception")
                raise
            if scores.shape != (width,) or scores.dtype != np.float32 or not np.isfinite(scores).all() or np.any((scores < 0) | (scores > 1)):
                self.clear_token_cache("invalid_score")
                raise ValueError("Matcher returned invalid classification scores.")
            chunks.append(scores)
            offset += width
        return np.concatenate(chunks)

    def score_pairs_lce(self, query_images, reference_images):
        """Apply the directed official-code Q26/R10 star ensemble to each pair."""
        if len(query_images) != len(reference_images):
            raise ValueError("Matcher query and reference batches must have equal lengths.")
        output = []
        for query, reference in zip(query_images, reference_images):
            q_views, r_views = [], []
            try:
                q_views = official_lce_views(query, "query", resize_to_224=True)
                r_views = official_lce_views(reference, "reference", resize_to_224=True)
                pairs = official_lce_pair_indices(len(q_views), len(r_views))
                scores = self.score_pairs([q_views[i].image for i, _ in pairs],
                                          [r_views[j].image for _, j in pairs])
                output.append(float(scores.max()))
            finally:
                for view in (*q_views, *r_views):
                    view.image.close()
        return np.asarray(output, dtype=np.float32)
