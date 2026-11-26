"""Strict-root CopyNCE MCP service over SSH stdio; never opens a network listener."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import signal
import stat
import sys

if __package__:
    from .copynce_setup import WEIGHT_SHA256
else:
    from copynce_setup import WEIGHT_SHA256

REPO = Path(__file__).resolve().parents[1]
MAX_CONFIG_BYTES = 65536
WORKERS = set()


def safe_path(value, *, exists=True):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError("Expected an absolute filesystem path")
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Expected an absolute path without parent traversal")
    # These two system-owned Darwin aliases are not user-selected symlinks.
    if sys.platform == "darwin" and path.parts[1:2] in (("tmp",), ("var",)):
        alias = Path("/", path.parts[1])
        real = Path("/private", path.parts[1])
        if alias.is_symlink() and alias.resolve() == real:
            path = real.joinpath(*path.parts[2:])
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Symbolic links are not allowed in server paths")
    if exists:
        path.stat()
    return path


def outside_worktrees(path):
    if path == REPO or REPO in path.parents or any((p / ".git").exists() for p in (path, *path.parents)):
        raise ValueError("Private runtime files must stay outside Git worktrees")


@dataclass(frozen=True)
class Settings:
    weights: Path
    image_roots: tuple[Path, ...]
    cache: Path
    device: str = "auto"
    batch_size: int = 8
    max_pixels: int = 40_000_000


def load_settings(config=None):
    raw = config or os.environ.get("COPYNCE_CONFIG") or str(Path.home() / ".config" / "image-duplicate-finder" / "server.json")
    path = safe_path(str(raw))
    outside_worktrees(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
        raise ValueError("Expected a small regular server configuration")
    if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("Server configuration must be private and owned by this account (chmod 600)")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "r", encoding="utf-8") as source:
        obj = json.load(source)
    allowed = {"weights", "image_roots", "cache", "device", "batch_size", "max_pixels"}
    if not isinstance(obj, dict) or set(obj) - allowed:
        raise ValueError("Unsupported server configuration field")
    roots = obj.get("image_roots")
    if not isinstance(roots, list) or not roots:
        raise ValueError("Explicit approved image_roots are required")
    roots = tuple(safe_path(root) for root in roots)
    if any(not root.is_dir() for root in roots):
        raise ValueError("Approved image roots must be directories")
    weights = safe_path(obj.get("weights"), exists=False)
    cache = safe_path(obj.get("cache"), exists=False)
    outside_worktrees(cache)
    if weights.exists() and not weights.is_file():
        raise ValueError("Expected a regular weights file")
    if cache.exists() and not cache.is_file():
        raise ValueError("Expected a regular descriptor cache")
    device = obj.get("device", "auto")
    if device not in ("auto", "cpu", "cuda", "mps"):
        raise ValueError("Invalid server device")
    batch = obj.get("batch_size", 8)
    pixels = obj.get("max_pixels", 40_000_000)
    if type(batch) is not int or not 1 <= batch <= 256 or type(pixels) is not int or pixels < 1:
        raise ValueError("Invalid server resource limits")
    return Settings(weights, roots, cache, device, batch, pixels)


def resolve_directory(settings, directory):
    if not isinstance(directory, str) or not directory or any(ord(c) < 32 for c in directory):
        raise ValueError("Select an approved image directory")
    selected = Path(directory)
    if ".." in selected.parts:
        raise ValueError("Parent traversal is not allowed")
    if not selected.is_absolute():
        selected = settings.image_roots[0] / selected
    selected = safe_path(str(selected))
    if not any(selected == root or root in selected.parents for root in settings.image_roots):
        raise ValueError("The selected directory is outside approved image roots")
    if not selected.is_dir():
        raise ValueError("Select an image directory")
    # Revalidate configured root components too, rather than trusting startup state.
    for root in settings.image_roots:
        safe_path(str(root))
    return selected


def get_info(settings):
    return dict(weights_present=settings.weights.is_file(), weights_path=str(settings.weights),
                weights_sha256=WEIGHT_SHA256, dimension=512, device=settings.device,
                batch_size=settings.batch_size, max_pixels=settings.max_pixels,
                image_roots=list(map(str, settings.image_roots)), cache_path=str(settings.cache),
                strict_image_roots=True, rejects_symlinks=True, transport="stdio")


def scan_sync(settings, directory, *, tiles=True, threshold=.75, top_k=100, mode="auto", recursive=True):
    """Freeze selected regular-file identities before the shared engine runs."""
    if __package__:
        from . import copynce_engine as engine
    else:
        import copynce_engine as engine
    if type(tiles) is not bool or type(recursive) is not bool:
        raise ValueError("tiles and recursive must be booleans")
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not -1 <= threshold <= 1:
        raise ValueError("Invalid cosine threshold")
    if type(top_k) is not int or not 1 <= top_k <= 100000 or mode not in ("auto", "exact", "indexed"):
        raise ValueError("Invalid retrieval limits")
    root = resolve_directory(settings, directory)
    safe_path(str(settings.weights))
    safe_path(str(settings.cache), exists=False)
    selection = engine._collect([root], recursive, False, engine.core.DEFAULT_EXTENSIONS, None)
    stamps = {}
    for record in selection.files:
        safe_path(str(record.path))
        stamps[str(record.path)] = record.stamp
    raw = engine.scan(list(map(Path, stamps)), weights=settings.weights, cache=settings.cache,
                      device=settings.device, batch_size=settings.batch_size, tiles=tiles,
                      threshold=threshold, top_k=top_k, mode=mode, recursive=recursive,
                      max_pixels=settings.max_pixels, file_stamps=stamps)
    raw["inputs"] = [str(root)]
    raw["directory"] = str(root)
    raw["errors"] = list(selection.errors) + raw.get("errors", [])
    raw["error_count"] = len(raw["errors"])
    raw["skipped"] = dict(selection.skipped)
    return raw


async def stop_worker(proc):
    if proc.returncode is None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except asyncio.TimeoutError:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


async def run_scan(config, directory, **options):
    proc = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()), "--scan-worker",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=(os.name == "posix"))
    WORKERS.add(proc)
    try:
        payload = json.dumps(dict(config=str(config), directory=directory, options=options)).encode()
        out, _ = await proc.communicate(payload)
        if proc.returncode:
            raise RuntimeError("CopyNCE scan failed; check model, image roots and dependencies locally on the server")
        return json.loads(out)
    finally:
        try:
            # MCP uses AnyIO level cancellation; shield cleanup so an EOF or
            # cancellation notification still reaps the owned process group.
            try:
                import anyio
            except ImportError:
                await stop_worker(proc)
            else:
                with anyio.CancelScope(shield=True):
                    await stop_worker(proc)
        finally:
            WORKERS.discard(proc)


def create_server(config=None):
    from mcp.server.fastmcp import FastMCP
    raw = config or os.environ.get("COPYNCE_CONFIG") or str(Path.home() / ".config" / "image-duplicate-finder" / "server.json")
    settings = load_settings(raw)
    server = FastMCP("image-duplicate-finder", log_level="ERROR")

    @server.tool()
    def get_image_dedup_info() -> dict:
        """Describe the explicitly configured private CopyNCE runtime."""
        return get_info(settings)

    @server.tool()
    async def find_similar_images(directory: str, tiles: bool = True, threshold: float = .75,
                                  top_k: int = 100, mode: str = "auto", recursive: bool = True) -> dict:
        """Find CopyNCE candidates inside the configured image roots only."""
        resolve_directory(settings, directory)
        return await run_scan(raw, directory, tiles=tiles, threshold=threshold,
                              top_k=top_k, mode=mode, recursive=recursive)

    return server


def main():
    os.umask(0o077)
    if sys.argv[1:] == ["--scan-worker"]:
        req = json.load(sys.stdin)
        result = scan_sync(load_settings(req["config"]), req["directory"], **req["options"])
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0
    if sys.argv[1:]:
        raise ValueError("This server supports SSH stdio only; no listen address is accepted")
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    if os.name == "posix":
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
    try:
        create_server().run(transport="stdio")
    finally:
        # Async cancellation normally reaps the worker. This also handles a
        # session-ending signal while the MCP runtime is shutting down.
        for proc in tuple(WORKERS):
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM) if os.name == "posix" else proc.terminate()
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        print("CopyNCE stdio server failed; verify its private configuration and dependencies locally", file=sys.stderr)
        sys.exit(2)
