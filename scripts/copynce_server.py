"""Strict-root CopyNCE MCP service over SSH stdio; never opens a network listener."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from contextlib import nullcontext
import hashlib
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
VALIDATED_SN = set()
BUDGET_LIMITS = dict(candidate_k=256, matcher_budget=100000, local_view_pairs=8, sn_extra_budget=10000, top_k=20000)


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
    matcher_weights: Path | None = None
    sn_index: Path | None = None


def _default_sn_index(cache):
    """Materialize the public background beside the server's private cache."""
    if __package__:
        from .copynce_default_sn import prepare_default_sn
    else:
        from copynce_default_sn import prepare_default_sn
    return safe_path(str(prepare_default_sn(cache_dir=cache.parent / "sn-backgrounds")))


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
    allowed = {"weights", "image_roots", "cache", "device", "batch_size", "max_pixels", "matcher_weights", "sn_index"}
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
    matcher = safe_path(obj["matcher_weights"], exists=False) if "matcher_weights" in obj else None
    sn_index = safe_path(obj["sn_index"], exists=False) if "sn_index" in obj else None
    if matcher is not None and "sn_index" not in obj:
        sn_index = _default_sn_index(cache)
    for resource in (matcher, sn_index):
        if resource is not None:
            outside_worktrees(resource)
    if matcher is not None and matcher.exists() and not matcher.is_file():
        raise ValueError("Expected a regular Matcher checkpoint")
    return Settings(weights, roots, cache, device, batch, pixels, matcher, sn_index)


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


def _hash_matches(path, expected):
    try:
        if __package__:
            from . import find_duplicates as core
        else:
            import find_duplicates as core
        path = safe_path(str(path))
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return False
        record = core.ImageFile(path, info.st_size, core.signature(info))
        with core.checked_stream(record) as source:
            digest = hashlib.sha256()
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest() == expected
    except (OSError, ValueError):
        return False


def _enhancement_info(settings):
    summary = dict(ready=False, matcher_weights_verified=False, sn_index_ready=False,
                   matcher_weights_sha256=None, sn_index_fingerprint=None,
                   sn_implementation="copynce-official-query-bias-v1")
    if settings.matcher_weights is None or settings.sn_index is None:
        return summary
    try:
        # Match the inference worker's dependency order: scanner/SQLite before
        # Torch. Preflight must not create a different native-library context.
        if __package__:
            from . import copynce_engine
            from . import copynce_sn
            from .copynce_matcher import OFFICIAL_SHA256
        else:
            import copynce_engine
            import copynce_sn
            from copynce_matcher import OFFICIAL_SHA256
        summary["matcher_weights_sha256"] = OFFICIAL_SHA256
        summary["matcher_weights_verified"] = _hash_matches(settings.matcher_weights, OFFICIAL_SHA256)
        descriptor_ok = _hash_matches(settings.weights, WEIGHT_SHA256)
        if not descriptor_ok or not summary["matcher_weights_verified"]:
            return summary
        inspect = getattr(copynce_sn, "inspect_index", None)
        if inspect is not None:
            manifest = inspect(settings.sn_index, expected_weights_sha256=WEIGHT_SHA256,
                               expected_pipeline=copynce_sn.GLOBAL_PIPELINE)
        else:
            index = copynce_sn.load_index(settings.sn_index, expected_weights_sha256=WEIGHT_SHA256,
                                         expected_pipeline=copynce_sn.GLOBAL_PIPELINE)
            manifest = index.manifest
            VALIDATED_SN.add(manifest["fingerprint"])
        # Checksum inspection runs on every negotiation. A new fingerprint also
        # receives full array/shape/finite/norm validation before any upload.
        if manifest["fingerprint"] not in VALIDATED_SN:
            index = copynce_sn.load_index(settings.sn_index, expected_weights_sha256=WEIGHT_SHA256,
                                         expected_pipeline=copynce_sn.GLOBAL_PIPELINE)
            if index.manifest["fingerprint"] != manifest["fingerprint"]:
                raise ValueError("The auxiliary index changed during validation")
            if len(VALIDATED_SN) >= 16:
                VALIDATED_SN.clear()
            VALIDATED_SN.add(manifest["fingerprint"])
        if manifest.get("dimension") != 512 or manifest.get("images", 0) < 5:
            return summary
        summary.update(sn_index_ready=True, sn_index_fingerprint=manifest["fingerprint"],
                       sn_index_schema_version=manifest["schema_version"],
                       sn_dimension=manifest["dimension"], sn_images=manifest["images"],
                       sn_pipeline=manifest["pipeline"], ready=True)
    except (OSError, ValueError, KeyError, ImportError, RuntimeError):
        # Capability responses never include checkpoint or index paths/errors.
        pass
    return summary


def get_info(settings):
    enhancement = _enhancement_info(settings)
    profiles = ["fast", "enhanced", "deep"] if enhancement["ready"] else ["fast"]
    return dict(weights_present=settings.weights.is_file(), weights_path=str(settings.weights),
                weights_sha256=WEIGHT_SHA256, dimension=512, device=settings.device,
                batch_size=settings.batch_size, max_pixels=settings.max_pixels,
                image_roots=list(map(str, settings.image_roots)), cache_path=str(settings.cache),
                strict_image_roots=True, rejects_symlinks=True, transport="stdio",
                api_version=2, report_schema_versions=[3, 4], supported_profiles=profiles,
                private_upload_api_version=2, strict_auxiliary_resources=True,
                budget_limits=dict(BUDGET_LIMITS), enhancement=enhancement)


def require_profile(settings, profile, expected_sn_fingerprint=None):
    if profile not in ("fast", "enhanced", "deep"):
        raise ValueError("Unsupported model quality profile")
    if profile == "fast":
        return None
    info = _enhancement_info(settings)
    if not info["ready"]:
        raise ValueError("Enhanced profiles require verified Matcher weights and a compatible private SN index")
    if expected_sn_fingerprint is not None and expected_sn_fingerprint != info["sn_index_fingerprint"]:
        raise ValueError("The private SN resource changed; inspect capabilities again")
    return info


def prepare_upload_job(settings, job_id, profile, expected_sn_fingerprint):
    if __package__:
        from . import copynce_remote_jobs as jobs
    else:
        import copynce_remote_jobs as jobs
    if profile != "fast" and (not isinstance(expected_sn_fingerprint, str)
            or len(expected_sn_fingerprint) != 64
            or any(c not in "0123456789abcdef" for c in expected_sn_fingerprint)):
        raise ValueError("An enhanced upload requires its negotiated SN fingerprint")
    require_profile(settings, profile, expected_sn_fingerprint)
    cfg = dict(weights=str(settings.weights), device=settings.device,
               batch_size=settings.batch_size, max_pixels=settings.max_pixels)
    if profile != "fast":
        cfg.update(matcher_weights=str(settings.matcher_weights), sn_index=str(settings.sn_index))
    return jobs.create_job(job_id, cfg, allow_auxiliary=(profile != "fast"))


def validate_request(profile, tiles, threshold, top_k, mode, recursive, candidate_k,
                     matcher_budget, local_view_pairs, sn_extra_budget, matcher_threshold):
    if profile not in ("fast", "enhanced", "deep"):
        raise ValueError("Unsupported model quality profile")
    if type(tiles) is not bool or type(recursive) is not bool:
        raise ValueError("tiles and recursive must be booleans")
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not -1 <= threshold <= 1:
        raise ValueError("Invalid cosine threshold")
    if type(top_k) is not int or not 1 <= top_k <= BUDGET_LIMITS["top_k"] or mode not in ("auto", "exact", "indexed"):
        raise ValueError("Invalid retrieval limits")
    if profile != "fast" and mode == "indexed":
        raise ValueError("Enhanced profiles do not support indexed retrieval")
    if profile == "deep" and not tiles:
        raise ValueError("Deep profile requires its local comparison views")
    for name, value, minimum in (("candidate_k", candidate_k, 1), ("matcher_budget", matcher_budget, 1),
                                 ("local_view_pairs", local_view_pairs, 0), ("sn_extra_budget", sn_extra_budget, 0)):
        if type(value) is not int or not minimum <= value <= BUDGET_LIMITS[name]:
            raise ValueError("Invalid enhanced stage budget")
    if (type(matcher_threshold) not in (int, float) or not math.isfinite(matcher_threshold)
            or not 0 <= matcher_threshold <= 1):
        raise ValueError("Invalid Matcher threshold")


def scan_sync(settings, directory, *, tiles=True, threshold=.75, top_k=100, mode="auto", recursive=True,
              profile="fast", candidate_k=32, matcher_budget=10000, local_view_pairs=2,
              sn_extra_budget=1000, matcher_threshold=.5):
    """Freeze selected regular-file identities before the shared engine runs."""
    if __package__:
        from . import copynce_engine as engine
    else:
        import copynce_engine as engine
    validate_request(profile, tiles, threshold, top_k, mode, recursive, candidate_k,
                     matcher_budget, local_view_pairs, sn_extra_budget, matcher_threshold)
    require_profile(settings, profile)
    root = resolve_directory(settings, directory)
    safe_path(str(settings.weights))
    safe_path(str(settings.cache), exists=False)
    selection = engine._collect([root], recursive, False, engine.core.DEFAULT_EXTENSIONS, None)
    stamps = {}
    for record in selection.files:
        safe_path(str(record.path))
        stamps[str(record.path)] = record.stamp
    extra = {}
    if profile != "fast":
        extra = dict(profile=profile, matcher_weights=settings.matcher_weights, sn_index=settings.sn_index,
                     candidate_k=candidate_k, matcher_budget=matcher_budget, local_view_pairs=local_view_pairs,
                     sn_extra_budget=sn_extra_budget, matcher_threshold=matcher_threshold)
    raw = engine.scan(list(map(Path, stamps)), weights=settings.weights, cache=settings.cache,
                      device=settings.device, batch_size=settings.batch_size, tiles=tiles,
                      threshold=threshold, top_k=top_k, mode=mode, recursive=recursive,
                      max_pixels=settings.max_pixels, file_stamps=stamps, **extra)
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
    def create_image_dedup_upload_job(job_id: str, profile: str = "fast",
                                      expected_sn_fingerprint: str | None = None) -> dict:
        """Create an isolated job from server-bound resources, never caller paths."""
        return prepare_upload_job(settings, job_id, profile, expected_sn_fingerprint)

    @server.tool()
    async def find_similar_images(directory: str, tiles: bool = True, threshold: float = .75,
                                  top_k: int = 100, mode: str = "auto", recursive: bool = True,
                                  profile: str = "fast", candidate_k: int = 32, matcher_budget: int = 10000,
                                  local_view_pairs: int = 2, sn_extra_budget: int = 1000,
                                  matcher_threshold: float = .5) -> dict:
        """Find CopyNCE candidates inside the configured image roots only."""
        validate_request(profile, tiles, threshold, top_k, mode, recursive, candidate_k,
                         matcher_budget, local_view_pairs, sn_extra_budget, matcher_threshold)
        resolve_directory(settings, directory)
        require_profile(settings, profile)
        return await run_scan(raw, directory, tiles=tiles, threshold=threshold,
                              top_k=top_k, mode=mode, recursive=recursive, profile=profile,
                              candidate_k=candidate_k, matcher_budget=matcher_budget,
                              local_view_pairs=local_view_pairs, sn_extra_budget=sn_extra_budget,
                              matcher_threshold=matcher_threshold)

    return server


def main():
    os.umask(0o077)
    if sys.argv[1:] == ["--scan-worker"]:
        req = json.load(sys.stdin)
        if __package__:
            from . import copynce_remote_jobs as jobs
        else:
            import copynce_remote_jobs as jobs
        config = safe_path(req["config"])
        job = config.parent
        guard = jobs.active_job(job) if job.parent == jobs.BASE and jobs.ID.fullmatch(job.name) else nullcontext()
        with guard:
            result = scan_sync(load_settings(config), req["directory"], **req["options"])
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
