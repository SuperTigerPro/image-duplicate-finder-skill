# SPDX-License-Identifier: Apache-2.0
# Adapted from https://github.com/eddielyc/CopyNCE. See third_party/CopyNCE-LICENSE.
"""Portable inference for the released ViT-S/16 CopyNCE Descriptor."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F

OFFICIAL_SHA256 = "829fdf4ae032bac24b4fcc60535cfc3b53026af083b33a0b8c71f1973f2166db"
OFFICIAL_BYTES = 87_514_773


@contextmanager
def _full_precision(device):
    """Keep CUDA TF32/ambient autocast from changing the fixed FP32 pipeline."""
    if str(device).startswith("cuda"):
        matmul = torch.backends.cuda.matmul.allow_tf32
        convolution = torch.backends.cudnn.allow_tf32
        try:
            torch.backends.cuda.matmul.allow_tf32 = False
            torch.backends.cudnn.allow_tf32 = False
            with torch.autocast(device_type="cuda", enabled=False):
                yield
        finally:
            torch.backends.cuda.matmul.allow_tf32 = matmul
            torch.backends.cudnn.allow_tf32 = convolution
    elif str(device) in {"cpu", "mps"}:
        with torch.autocast(device_type=str(device), enabled=False):
            yield
    else:
        yield


class _PatchEmbedding(nn.Module):
    def __init__(self):
        super().__init__()
        self.proj = nn.Conv2d(3, 384, kernel_size=16, stride=16)

    def forward(self, images):
        return self.proj(images).flatten(2).transpose(1, 2)


class _Attention(nn.Module):
    def __init__(self):
        super().__init__()
        self.qkv = nn.Linear(384, 3 * 384)
        self.proj = nn.Linear(384, 384)

    def forward(self, tokens):
        batch, count, width = tokens.shape
        qkv = self.qkv(tokens).reshape(batch, count, 3, 6, 64)
        query, key, value = qkv.permute(2, 0, 3, 1, 4).unbind(0)
        scores = (query @ key.transpose(-2, -1)) * (64 ** -0.5)
        features = scores.softmax(dim=-1) @ value
        return self.proj(features.transpose(1, 2).reshape(batch, count, width))


class _MLP(nn.Module):
    def __init__(self):
        super().__init__()
        self.fc1 = nn.Linear(384, 1536)
        self.fc2 = nn.Linear(1536, 384)

    def forward(self, tokens):
        return self.fc2(F.gelu(self.fc1(tokens)))


class _Block(nn.Module):
    def __init__(self):
        super().__init__()
        self.norm1 = nn.LayerNorm(384, eps=1e-4)
        self.attn = _Attention()
        self.norm2 = nn.LayerNorm(384, eps=1e-4)
        self.mlp = _MLP()

    def forward(self, tokens):
        tokens = tokens + self.attn(self.norm1(tokens))
        return tokens + self.mlp(self.norm2(tokens))


class _Encoder(nn.Module):
    def __init__(self):
        super().__init__()
        self.patch_embed = _PatchEmbedding()
        self.cls_token = nn.Parameter(torch.zeros(1, 1, 384))
        self.pos_embed = nn.Parameter(torch.zeros(1, 197, 384))
        self.blocks = nn.Sequential(*(_Block() for _ in range(12)))

    def forward(self, images):
        patches = self.patch_embed(images)
        prefix = self.cls_token.expand(images.shape[0], -1, -1)
        tokens = torch.cat((prefix, patches), dim=1) + self.pos_embed
        # The released encoder has last_norm=False.
        return self.blocks(tokens)[:, 0]


class _LinearHead(nn.Module):
    def __init__(self):
        super().__init__()
        self.fc1 = nn.Linear(384, 512, bias=False)
        self.bn = nn.BatchNorm1d(512)

    def forward(self, cls):
        return F.normalize(self.bn(self.fc1(cls)), p=2, dim=1)


class _CopyDescriptor(nn.Module):
    def __init__(self):
        super().__init__()
        self.encoder = _Encoder()
        self.heads = nn.ModuleDict({"linear_head": _LinearHead()})

    def forward(self, images):
        return self.heads["linear_head"](self.encoder(images))


class Descriptor:
    dimension = 512
    precision = "float32"

    def __init__(self, weights: str, device: str = "auto"):
        self.requested_device = device
        if device == "auto":
            if torch.cuda.is_available():
                device = "cuda"
            elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                device = "mps"
            else:
                device = "cpu"
        target = torch.device(device)
        if target.type not in ("cuda", "mps", "cpu"):
            raise ValueError("Choose auto, cpu, mps or a CUDA device.")
        self.fallbacks = []
        self.max_batch = None
        digest = hashlib.sha256()
        path = Path(weights).expanduser()
        if path.stat().st_size != OFFICIAL_BYTES:
            raise ValueError("CopyNCE Descriptor checkpoint size mismatch.")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            if digest.hexdigest() != OFFICIAL_SHA256:
                raise ValueError("CopyNCE Descriptor checkpoint SHA-256 mismatch.")
            stream.seek(0)
            record = torch.load(stream, map_location="cpu", weights_only=True)
        if not isinstance(record, Mapping):
            raise ValueError("Expected the published CopyNCE checkpoint dictionary.")
        state = record.get("model", record.get("state_dict", record))
        if not isinstance(state, Mapping):
            raise ValueError("Expected the published CopyNCE tensor state dictionary.")
        cleaned = {key.removeprefix("module."): value for key, value in state.items()}
        # Training-only temperature in the released checkpoint.
        cleaned.pop("infonce_log_scale", None)
        self._net = _CopyDescriptor().to(dtype=torch.float32).eval()
        self._net.load_state_dict(cleaned, strict=True)
        self.weights_id = digest.hexdigest()
        self.device = "cpu"
        self._mean = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
        self._std = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)
        if target.type != "cpu":
            try:
                self._move(target)
                with torch.inference_mode():
                    vector = self._forward([np.zeros((224, 224, 3), dtype=np.uint8)])
                    if vector.shape != (1, self.dimension) or not np.isfinite(vector).all():
                        raise RuntimeError("Non-finite output from the device probe")
            except (RuntimeError, AssertionError, NotImplementedError) as exc:
                exc.__traceback__ = None
                self._move("cpu")
                self.fallbacks.append(f"{target} probe failed; using CPU")
                self._release_accelerator(str(target))

    def _move(self, device):
        self._net.to(device)
        self._mean = self._mean.to(device)
        self._std = self._std.to(device)
        self.device = str(device)

    @staticmethod
    def _release_accelerator(device):
        try:
            if str(device).startswith("cuda"):
                torch.cuda.empty_cache()
            elif str(device) == "mps" and getattr(torch, "mps", None):
                torch.mps.empty_cache()
        except (RuntimeError, AssertionError, NotImplementedError):
            pass

    def _forward(self, images):
        # Inputs are already 224x224 RGB crops from the scanner.
        batch = torch.from_numpy(np.stack([np.asarray(image) for image in images]))
        if batch.dtype != torch.uint8 or tuple(batch.shape[1:]) != (224, 224, 3):
            raise ValueError("Descriptor inputs must be 224x224 RGB uint8 images.")
        batch = batch.permute(0, 3, 1, 2).to(device=self.device, dtype=torch.float32)
        batch = batch.div_(255.0).sub_(self._mean).div_(self._std)
        with _full_precision(self.device):
            return self._net(batch).to(device="cpu", dtype=torch.float32).numpy()

    @torch.inference_mode()
    def encode(self, images) -> np.ndarray:
        if not images:
            return np.empty((0, self.dimension), dtype=np.float32)
        offset = 0
        chunks = []
        while offset < len(images):
            width = min(self.max_batch or len(images), len(images) - offset)
            try:
                vectors = self._forward(images[offset:offset + width])
            except (RuntimeError, NotImplementedError) as exc:
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
            if vectors.shape != (width, self.dimension) or not np.isfinite(vectors).all():
                raise ValueError("Descriptor returned invalid feature vectors.")
            chunks.append(vectors)
            offset += width
        return np.concatenate(chunks)
