"""Materialize the reviewed public SN background in a private runtime cache.

This module needs only the standard library. It never downloads images, loads
model weights, or treats a user-supplied background as a bundled resource.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

if __package__:
    from . import copynce_setup as setup
else:
    import copynce_setup as setup


DEFAULT_FINGERPRINT = "9cfd63553927874550cca87651b6013f4c0972ca3b5863b7c612272bbc572316"
DEFAULT_NAME = "sn-openimages-1000-" + DEFAULT_FINGERPRINT[:12]
BUNDLED_INDEX = Path(__file__).resolve().parents[1] / "assets" / "sn-openimages-1000"
DEFAULT_FILES = {
    "manifest.json": (898, "5d3d5a688181528a698ce9b4f58dd6d3565e964df1fb570b5a56e3390adb9c76"),
    "features.npz": (2817052, "15d75894446c9a701606e6514434232941fb49101a58aa0ab39b4f0912dcbf6c"),
}


def _verify(directory):
    for name, (size, digest) in DEFAULT_FILES.items():
        path = directory / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise ValueError("默认 SN 资源缺失或大小不符，请重新获取完整 Skill")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("默认 SN 资源校验失败，请重新获取完整 Skill")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("fingerprint") != DEFAULT_FINGERPRINT:
        raise ValueError("默认 SN 背景版本不符")


def _cache_root(value):
    requested = Path(value).expanduser().absolute()
    # Reject links before resolve() so a configured cache cannot redirect writes.
    if any(p.is_symlink() for p in (requested, *requested.parents)):
        raise ValueError("默认 SN 缓存路径不能经过符号链接")
    root = requested.resolve()
    skill = Path(__file__).resolve().parents[1]
    if root == skill or skill in root.parents or any((p / ".git").exists() for p in (root, *root.parents)):
        raise ValueError("默认 SN 缓存必须位于 Skill 及所有 Git 工作树之外")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.stat()
    if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise ValueError("默认 SN 缓存根目录不能由其他用户写入")
    return root


def _private_index(directory):
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("默认 SN 缓存必须是普通目录")
    for path in (directory, *(directory / name for name in DEFAULT_FILES)):
        info = path.lstat()
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("默认 SN 运行索引必须为当前用户私有")
    _verify(directory)


def prepare_default_sn(cache_dir=None):
    """Return a verified private index; cache_dir is its parent cache directory.

    Invalid cached bytes are repaired from the pinned bundle under an OS lock.
    Explicit custom indexes are handled by the caller and never enter this path.
    """
    root = _cache_root(cache_dir if cache_dir is not None else setup.user_cache() / "backgrounds")
    target = root / DEFAULT_NAME
    lock_path = root / ".default-sn.lock"
    if lock_path.is_symlink() or lock_path.exists() and not lock_path.is_file():
        raise ValueError("默认 SN 缓存锁必须是普通文件")
    with setup.locked(lock_path):
        if target.is_symlink():
            raise ValueError("默认 SN 索引不能是符号链接")
        if target.exists():
            try:
                _private_index(target)
                return target
            except (OSError, ValueError):
                # Only this versioned default resource is repairable. An unsafe
                # existing filesystem object is never recursively removed.
                if not target.is_dir() or any(p.is_symlink() or not p.is_file() for p in target.iterdir()):
                    raise ValueError("默认 SN 缓存包含意外内容，请选择其他私有缓存目录") from None
                if set(p.name for p in target.iterdir()) - DEFAULT_FILES.keys():
                    raise ValueError("默认 SN 缓存包含意外文件，请选择其他私有缓存目录") from None
        _verify(BUNDLED_INDEX)
        temporary = Path(tempfile.mkdtemp(prefix=".default-sn-", dir=root))
        try:
            for name in DEFAULT_FILES:
                shutil.copyfile(BUNDLED_INDEX / name, temporary / name)
                (temporary / name).chmod(0o600)
            _private_index(temporary)
            if target.exists():
                # Keep the directory stable for other readers and replace each
                # pinned resource atomically while concurrent writers hold lock.
                for name in DEFAULT_FILES:
                    os.replace(temporary / name, target / name)
                target.chmod(0o700)
            else:
                os.replace(temporary, target)
            _private_index(target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return target
