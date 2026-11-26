"""Call a user-provisioned CopyNCE MCP server over authenticated OpenSSH."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import warnings

if __package__:
    from .copynce_setup import WEIGHT_SHA256 as WEIGHTS_SHA256
    from .find_duplicates import signature, ImageFile, checked_stream, NORMALIZE_MODES
else:
    from copynce_setup import WEIGHT_SHA256 as WEIGHTS_SHA256
    from find_duplicates import signature, ImageFile, checked_stream, NORMALIZE_MODES


HELPER = Path(__file__).with_name("copynce_remote_jobs.py")
REPO = Path(__file__).resolve().parent.parent
LOGIN = re.compile(r"[\w.][\w.-]*\Z", re.ASCII)
HOSTNAME = re.compile(r"[\w][\w.-]*\Z", re.ASCII)
IMAGE_SUFFIXES = {
    "jpeg": {".jpg", ".jpeg", ".jfif"}, "png": {".png"}, "gif": {".gif"},
    "webp": {".webp"}, "bmp": {".bmp"}, "tiff": {".tif", ".tiff"},
    "pbm": {".pbm", ".pnm"}, "pgm": {".pgm", ".pnm"}, "ppm": {".ppm", ".pnm"},
}
UPLOAD_EXTENSIONS = frozenset().union(*IMAGE_SUFFIXES.values())


def validate_upload_image(stream, suffix=None):
    """Recognize supported image headers without decoding; restore stream position."""
    offset = stream.tell()
    try:
        stream.seek(0)
        h = stream.read(64)
    finally:
        stream.seek(offset)
    fmt = None
    if h.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR") and len(h) >= 33:
        fmt = "png"
    elif len(h) >= 4 and h.startswith(b"\xff\xd8\xff") and h[3] not in (0, 255):
        fmt = "jpeg"
    elif h.startswith((b"GIF87a", b"GIF89a")) and len(h) >= 13:
        fmt = "gif"
    elif h[:4] == b"RIFF" and h[8:12] == b"WEBP" and h[12:16] in (b"VP8 ", b"VP8L", b"VP8X"):
        fmt = "webp"
    elif h.startswith(b"BM") and len(h) >= 26 and int.from_bytes(h[14:18], "little") in (12, 40, 52, 56, 64, 108, 124):
        fmt = "bmp"
    elif h.startswith((b"II*\0", b"MM\0*", b"II+\0", b"MM\0+")) and len(h) >= 8:
        fmt = "tiff"
    elif re.match(rb"P[1-6]\s+(?:#[^\r\n]*[\r\n]+\s*)?\d+\s+\d+", h):
        fmt = {"1": "pbm", "4": "pbm", "2": "pgm", "5": "pgm", "3": "ppm", "6": "ppm"}[chr(h[1])]
    if fmt is None or (suffix is not None and str(suffix).lower() not in IMAGE_SUFFIXES[fmt]):
        raise ValueError("Upload requires a supported image header and a matching image extension")
    return fmt


@dataclass(frozen=True)
class Profile:
    host: str
    launcher: str
    options: tuple


def _text(value):
    return isinstance(value, str) and value and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _destination(text, for_sftp=False):
    if not _text(text):
        return None
    user, sep, host = text.rpartition("@")
    if sep and not LOGIN.fullmatch(user):
        return None
    if HOSTNAME.fullmatch(host):
        return text
    plain = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if "%" in plain and not re.fullmatch(r"[A-Za-z0-9_.-]+", plain.partition("%")[2]):
        return None
    try:
        ipaddress.IPv6Address(plain)
    except ValueError:
        return None
    return user + sep + (f"[{plain}]" if for_sftp else plain)


def _jump(text):
    if _destination(text):
        return True
    host, sep, port = text.rpartition(":")
    return bool(sep and port.isascii() and port.isdigit() and 1 <= int(port) <= 65535 and _destination(host))


def load_profile(path):
    try:
        return _load_profile(path)
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError):
        raise ValueError("Cannot read private SSH profile; check its location, JSON encoding and permissions locally") from None


def _load_profile(path):
    path = Path(path).expanduser()
    resolved = path.resolve()
    if (path.is_symlink() or resolved.is_relative_to(REPO)
            or any((parent / ".git").exists() for parent in resolved.parents)):
        raise ValueError("Store your private connection profile outside Git worktrees")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        raise ValueError("Invalid SSH profile")
    if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("The SSH profile must belong to you and be private (chmod 600)")
    obj = json.loads(path.read_text(encoding="utf-8"))
    allowed = {"host", "launcher", "port", "identity_file", "jump_host", "ssh_config", "control_path"}
    if not isinstance(obj, dict) or set(obj) - allowed:
        raise ValueError("Unknown SSH profile field; authenticate using your local SSH client")
    host, launcher = obj.get("host"), obj.get("launcher")
    if _destination(host) is None:
        raise ValueError("Provide an SSH alias or a user@host destination")
    if not _text(launcher) or not PurePosixPath(launcher).is_absolute():
        raise ValueError("Set the absolute path of the remote MCP launcher")
    options = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "ForwardAgent=no",
               "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes", "-o", "PermitLocalCommand=no"]
    if "port" in obj:
        port = obj["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid SSH port")
        options.extend(["-o", f"Port={port}"])
    for key, flag in (("ssh_config", "-F"), ("identity_file", "-i")):
        if key in obj:
            if not _text(obj[key]):
                raise ValueError(f"Invalid {key} path")
            options.extend([flag, str(Path(obj[key]).expanduser().absolute())])
    if "control_path" in obj:
        if not _text(obj["control_path"]):
            raise ValueError("Invalid control_path")
        options.extend(["-o", "ControlPath=" + str(Path(obj["control_path"]).expanduser().absolute())])
    if "jump_host" in obj:
        if not _text(obj["jump_host"]) or not all(_jump(h) for h in obj["jump_host"].split(",")):
            raise ValueError("Invalid jump_host")
        options.extend(["-J", obj["jump_host"]])
    return Profile(_destination(host), launcher, tuple(options))


def _ssh(profile, argv):
    return ["ssh", "-T", *profile.options, "--", profile.host, shlex.join(argv)]


def _job(profile, action, *args):
    try:
        result = subprocess.run(_ssh(profile, ["python3", "-", action, *args]),
                                input=HELPER.read_bytes(), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=90, check=False)
    except subprocess.TimeoutExpired:
        raise TimeoutError("The private-job operation timed out; check your SSH connection") from None
    if result.returncode:
        raise RuntimeError("Remote private-job operation failed; check SSH authentication, Python 3 and free disk")
    return json.loads(result.stdout)


def cleanup(profile):
    """Remove expired jobs left by disconnected sessions or a server restart."""
    return int(_job(load_profile(profile), "reap"))


def cleanup_job(profile, job_id):
    """Confirm removal of one owned job after reconnecting, without waiting for expiry."""
    if not isinstance(job_id, str) or re.fullmatch(r"[a-f0-9]{32}", job_id) is None:
        raise ValueError("Provide the 32-character hexadecimal private job ID")
    result = _job(load_profile(profile), "remove", job_id)
    if result is not None:
        raise RuntimeError("The server did not confirm private job removal")
    return True


@asynccontextmanager
async def _session(profile, config=None):
    argv = [profile.launcher]
    if config:
        argv = ["env", "COPYNCE_CONFIG=" + config, *argv]
    proc = await asyncio.create_subprocess_exec(*_ssh(profile, argv), stdin=asyncio.subprocess.PIPE,
                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=64*1024*1024)
    seq = 0

    async def send(obj):
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", **obj}) + "\n").encode())
        await proc.stdin.drain()

    async def request(method, params, timeout):
        nonlocal seq
        seq += 1
        rid = seq
        await send({"method": method, "id": rid, "params": params})
        async def wait_response():
            while True:
                line = await proc.stdout.readline()
                if not line:
                    raise ConnectionError("SSH/MCP disconnected; verify local authentication and remote launcher")
                msg = json.loads(line)
                if msg.get("id") == rid and ("result" in msg or "error" in msg):
                    if "error" in msg:
                        raise RuntimeError(f"Remote MCP rejected {method}")
                    return msg["result"]
        try:
            return await asyncio.wait_for(wait_response(), timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            try:
                await send({"method": "notifications/cancelled", "params": {"requestId": rid}})
            except (OSError, ConnectionError):
                pass
            raise

    async def call(name, args=None):
        env = await request("tools/call", {"name": name, "arguments": args or {}}, 12*3600)
        if env.get("isError"):
            raise RuntimeError(f"Remote {name} failed. Check server image roots, dependencies and model configuration")
        data = env.get("structuredContent")
        if data is None:
            data = json.loads(env["content"][0]["text"])
        data = data.get("result", data)
        if not isinstance(data, dict):
            raise ValueError("Incompatible remote MCP result")
        return data

    try:
        await request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                      "clientInfo": {"name": "image-duplicate-finder", "version": "1"}}, 60)
        await send({"method": "notifications/initialized"})
        resp = await request("tools/list", {}, 60)
        names = {t["name"] for t in resp["tools"]}
        if not {"get_image_dedup_info", "find_similar_images"} <= names:
            raise ValueError("The remote MCP server does not implement the CopyNCE tools")
        yield call
    finally:
        if proc.stdin:
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


async def _info(profile):
    async with _session(profile) as call:
        return await call("get_image_dedup_info")


async def _scan(profile, directory, options, config=None, job=None, require_strict_remote=False):
    async with _session(profile, config) as call:
        info = await call("get_image_dedup_info")
        if not info.get("weights_present"):
            raise ValueError("The remote CopyNCE weights are missing")
        if require_strict_remote and info.get("strict_image_roots") is not True:
            raise ValueError("The host requires a server enforcing strict image roots and rejecting symlinks")
        if job:
            if info.get("image_roots") != [job["directory"]]:
                raise ValueError("The remote launcher did not isolate the temporary image directory")
            if info.get("cache_path") not in (None, job["cache"]):
                raise ValueError("The remote descriptor cache is outside this upload job")
        result = await call("find_similar_images", dict(directory=directory, **options))
        if result.get("weights_sha256") != WEIGHTS_SHA256 or result.get("dimension") != 512:
            raise ValueError("Unexpected remote descriptor weights or dimensions")
        if not result.get("device"):
            result["device"] = (await call("get_image_dedup_info")).get("device", "remote")
        return result


def _snapshot(source, destination, expected=None, upload_original=False):
    path = Path(source).absolute()
    # Darwin's system-owned /tmp and /var are aliases for /private siblings.
    if sys.platform == "darwin" and len(path.parts) > 1 and path.parts[1] in ("tmp", "var"):
        alias = Path("/", path.parts[1])
        canonical = Path("/private", path.parts[1])
        if alias.is_symlink() and alias.resolve() == canonical:
            path = canonical.joinpath(*path.parts[2:])
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError("Refusing a symbolic link in the upload selection")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or path.suffix.lower() not in UPLOAD_EXTENSIONS:
        raise ValueError("Select regular files with supported image extensions for upload")
    if expected is not None and tuple(expected) != signature(before):
        raise OSError("An upload source changed after approval; inspect a new upload plan")
    record = ImageFile(path, before.st_size, signature(before))
    with checked_stream(record) as src:
        validate_upload_image(src, path.suffix)
        if upload_original:
            with destination.open("xb") as dst:
                shutil.copyfileobj(src, dst, 1024*1024)
        else:
            try:
                from PIL import Image, ImageOps
            except ImportError:
                raise RuntimeError("Sanitized upload requires Pillow; install requirements-pixels.txt before retrying") from None
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                prefix = src.read(26)
                src.seek(0)
                with Image.open(src) as image:
                    if image.width * image.height > 40_000_000:
                        raise ValueError("Upload image exceeds the 40-million-pixel limit")
                    if getattr(image, "n_frames", 1) != 1:
                        raise ValueError("Sanitized upload requires a single-frame static image")
                    bits = getattr(image, "tag_v2", {}).get(258, ())
                    if isinstance(bits, int):
                        bits = (bits,)
                    deep_png = prefix.startswith(b"\x89PNG\r\n\x1a\n") and prefix[24:25] == b"\x10"
                    if image.mode not in NORMALIZE_MODES or deep_png or bits and max(bits) > 8:
                        raise ValueError("High bit-depth or special color format cannot use sanitized CopyNCE upload")
                    if image.format == "ICO" and len(image.ico.sizes()) != 1:
                        raise ValueError("Multi-resolution images cannot use sanitized upload")
                    with ImageOps.exif_transpose(image) as oriented, oriented.convert("RGB") as rgb:
                        # Conversion can preserve info; erase it explicitly so PNG
                        # receives no EXIF, ICC, text, original paths or comments.
                        rgb.info.clear()
                        with destination.open("xb") as dst:
                            rgb.save(dst, format="PNG")
    with destination.open("rb") as staged:
        validate_upload_image(staged, None if upload_original else ".png")


def _sftp_quote(value):
    if not _text(value):
        raise ValueError("Invalid SFTP path")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _upload(profile, stage, job_id, names, remote_directory=None):
    target = remote_directory or f".cache/image-duplicate-finder/jobs-v1/{job_id}/images"
    inventory = {}
    for attempt in range(2):
        if attempt:
            inventory = _job(profile, "inventory", job_id)
        commands = []
        for name in names:
            size = (stage / name).stat().st_size
            if inventory.get(name) == size:
                continue
            verb = "reput" if 0 < inventory.get(name, 0) < size else "put"
            commands.append(f"{verb} {name} {_sftp_quote(target + '/' + name)}")
        if not commands:
            return
        result = subprocess.run(["sftp", "-q", "-b", "-", *profile.options, "--", _destination(profile.host, for_sftp=True)],
                                input=("\n".join(commands) + "\n").encode("utf-8"), cwd=stage,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if result.returncode == 0:
            return
    raise ConnectionError("SFTP transfer did not complete after reconnecting")


def _map_result(result, names, remote_dir):
    paths = {str(PurePosixPath(remote_dir) / name): str(path) for name, path in names.items()}
    for row in result["matches"]:
        for key in ("path_a", "path_b"):
            if row.get(key) not in paths:
                raise ValueError("The server returned a path outside this upload job")
            row[key] = paths[row[key]]
    errors = []
    operations = {"stat", "scan", "verify", "descriptor", "decode", "remote"}
    error_types = {"OSError", "ValueError", "SyntaxError", "PermissionError", "FileNotFoundError",
                   "UnidentifiedImageError", "DecompressionBombError", "DecompressionBombWarning", "RuntimeError"}
    for item in result.get("errors", []):
        raw = item.get("path")
        errors.append(dict(path=paths.get(raw, ""),
                           operation=item.get("operation") if item.get("operation") in operations else "remote",
                           error_type=item.get("error_type") if item.get("error_type") in error_types else "RemoteError",
                           message="The remote server could not process this image" if raw in paths
                                   else "The remote server could not read part of this job"))
    result["errors"] = errors
    result["directory"] = ""
    if "inputs" in result:
        result["inputs"] = sorted(map(str, names.values()))
    return result


def cloud_scan(profile, *, paths=(), remote_directory=None, allow_upload=False, tiles=True,
               threshold=.75, top_k=100, mode="auto", recursive=True, require_strict_remote=True,
               file_stamps=None, upload_original=False):
    """Run in a remote directory or explicitly transfer an approved local selection."""
    conn = load_profile(profile)
    options = dict(tiles=tiles, threshold=threshold, top_k=top_k, mode=mode, recursive=recursive)
    if remote_directory is not None:
        if paths or allow_upload or not isinstance(remote_directory, str) or not remote_directory.strip():
            raise ValueError("Select one remote directory without local upload inputs")
        return asyncio.run(_scan(conn, remote_directory, options, require_strict_remote=require_strict_remote))
    if not allow_upload:
        raise ValueError("Explicit authorization is required to upload local images")
    if not paths:
        raise ValueError("No local images were selected")
    job_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="image-check-upload-") as private:
        stage = Path(private)
        names = {}
        ordered = sorted({Path(value).absolute() for value in paths}, key=str)
        for i, path in enumerate(ordered):
            # A common recognized suffix supports older remote extension filters.
            # Decoders detect the unchanged original format from the file header.
            name = f"{i:016x}-{uuid.uuid4().hex}.png"
            stamp = None
            if file_stamps is not None:
                stamp = file_stamps[str(path)]
            _snapshot(path, stage / name, stamp, upload_original=upload_original)
            names[name] = path
        info = asyncio.run(_info(conn))
        weights = info.get("weights_path")
        if not info.get("weights_present") or not isinstance(weights, str) or not PurePosixPath(weights).is_absolute():
            raise ValueError("The remote server must provide its installed CopyNCE weights path")
        cfg = {"weights": weights, "device": info.get("device", "auto"), "batch_size": info.get("batch_size", 8), "max_pixels": info.get("max_pixels", 40_000_000)}
        try:
            job = _job(conn, "create", job_id, json.dumps(cfg))
            # An empty-gallery scan checks the actual loaded model hash and verifies
            # the private SQLite cache before any source image is sent, also on legacy servers.
            probe = dict(options, tiles=False, top_k=1, mode="exact")
            check = asyncio.run(_scan(conn, ".", probe, job["config"], job))
            if check.get("image_count") != 0 or not _job(conn, "cache-ready", job_id):
                raise ValueError("The remote server did not initialize an empty gallery with a private descriptor cache")
            _upload(conn, stage, job_id, names, job["directory"])
            for attempt in range(2):
                try:
                    result = asyncio.run(_scan(conn, ".", options, job["config"], job))
                    return _map_result(result, names, job["directory"])
                except ConnectionError:
                    if attempt:
                        raise
        finally:
            try:
                _job(conn, "remove", job_id)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                raise RuntimeError(f"Remote cleanup could not be confirmed for job {job_id}; reconnect and use "
                                   f"--cleanup-job {job_id} to remove this job. Expired jobs are also eligible "
                                   "for --cleanup after 24 hours") from None


def scan_remote(profile, directory, **options):
    return cloud_scan(profile, remote_directory=directory, **options)


def scan_upload(profile, files, *, allow_upload=False, **options):
    return cloud_scan(profile, paths=files, allow_upload=allow_upload, **options)
