"""Prepare the optional local CopyNCE runtime and verify its official weights."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request


FILE_ID = "1fWChrrOovX8JOcNNCgrj6liXR2L8yKEu"
CHECKPOINT = "descriptor_vits_224.pth.tar"
WEIGHT_SHA256 = "829fdf4ae032bac24b4fcc60535cfc3b53026af083b33a0b8c71f1973f2166db"
WEIGHT_BYTES = 87_514_773
MATCHER_FILE_ID = "1g6bAUTfyEXR20bUbEtHH_i5NBfrbDYHq"
MATCHER_CHECKPOINT = "matcher_vits_224.pth.tar"
MATCHER_SHA256 = "dd8dea1c345039bbda303c42457aa3a0389d46fb37ea39506bfb1faddedda1df"
MATCHER_BYTES = 86_733_215
DRIVE = "https://drive.usercontent.google.com/download"
TORCH = "2.7.1"
PYPI = "https://pypi.org/simple"
CACHE_ENV = "COPYNCE_CACHE_DIR"
PYTHON_ENV = "COPYNCE_PYTHON"
NUMPY = "2.2.6"
PILLOW = "11.3.0"


def user_cache() -> Path:
    configured = os.environ.get(CACHE_ENV)
    if configured:
        base = Path(configured).expanduser()
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "image-duplicate-finder"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "image-duplicate-finder"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "image-duplicate-finder"
    base = base.resolve()
    repo = Path(__file__).resolve().parents[1]
    if base == repo or repo in base.parents:
        raise ValueError("模型缓存须位于 Skill 仓库之外")
    if any((parent / ".git").exists() for parent in (base, *base.parents)):
        raise ValueError("模型缓存须位于 Git 工作区之外")
    return base


@contextlib.contextmanager
def locked(path: Path, timeout: float = 1800):
    """A process crash releases the kernel lock; keep the lock file for concurrent waiters."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
        else:
            import fcntl
        started = time.monotonic()
        while True:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() - started >= timeout:
                    raise TimeoutError("另一模型准备任务仍占用此缓存，请稍后重试")
                time.sleep(0.25)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def verify(path: Path) -> Path:
    if not path.is_file() or path.stat().st_size != WEIGHT_BYTES:
        raise ValueError("Descriptor 权重文件大小不正确，请选择完整的官方权重")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != WEIGHT_SHA256:
        raise ValueError("Descriptor 权重的 SHA-256 校验失败")
    return path


def verify_matcher(path: Path) -> Path:
    if not path.is_file() or path.stat().st_size != MATCHER_BYTES:
        raise ValueError("Matcher 权重文件大小不正确，请选择完整的官方权重")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != MATCHER_SHA256:
        raise ValueError("Matcher 权重的 SHA-256 校验失败")
    return path


class _DriveForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_form = False
        self.action = ""
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.in_form = attrs.get("id") == "download-form"
            if self.in_form:
                self.action = attrs.get("action", "")
        if self.in_form and tag == "input" and attrs.get("name") in {"id", "export", "confirm", "uuid", "at"}:
            self.fields[attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_form = False


def _confirmation(html: bytes, file_id=FILE_ID) -> str:
    form = _DriveForm()
    form.feed(html.decode("utf-8", errors="replace"))
    action = urllib.parse.urlparse(form.action)
    if ((action.scheme, action.netloc, action.path) != ("https", "drive.usercontent.google.com", "/download")
            or form.fields.get("id") != file_id or not form.fields.get("confirm")):
        raise ValueError("Google Drive 未返回指定的公开权重，可使用离线权重文件")
    return DRIVE + "?" + urllib.parse.urlencode(form.fields)


def _download(destination: Path, file_id=FILE_ID, expected_bytes=None):
    limit = WEIGHT_BYTES if expected_bytes is None else expected_bytes
    url = DRIVE + "?" + urllib.parse.urlencode({"id": file_id, "export": "download", "confirm": "t"})
    # A cookie jar permits Drive's optional public-file confirmation challenge.
    import http.cookiejar
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    for attempt in range(2):
        request = urllib.request.Request(url, headers={"User-Agent": "image-duplicate-finder/CopyNCE"})
        with opener.open(request, timeout=60) as response:
            if response.headers.get_content_type() == "text/html":
                if attempt:
                    raise ValueError("Google Drive 当前无法提供权重，可使用离线权重文件")
                url = _confirmation(response.read(512 * 1024), file_id=file_id)
                continue
            total = 0
            with destination.open("wb") as stream:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > limit:
                        raise ValueError("下载响应超出已知权重文件大小")
                    stream.write(chunk)
            return


def _weights(explicit, offline):
    requested = explicit or os.environ.get("COPYNCE_WEIGHTS")
    if requested:
        return verify(Path(requested).expanduser().resolve())
    target = user_cache() / "weights" / CHECKPOINT
    with locked(target.parent / ".download.lock"):
        if target.exists():
            try:
                return verify(target)
            except ValueError:
                if offline:
                    raise
        if offline:
            raise FileNotFoundError("离线模式未找到已校验的权重；请指定 --weights")
        fd, temporary = tempfile.mkstemp(prefix=".descriptor-", dir=target.parent)
        os.close(fd)
        temporary = Path(temporary)
        try:
            _download(temporary)
            verify(temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def _matcher_weights(explicit, offline):
    requested = explicit or os.environ.get("COPYNCE_MATCHER_WEIGHTS")
    if requested:
        return verify_matcher(Path(requested).expanduser().resolve())
    target = user_cache() / "weights" / MATCHER_CHECKPOINT
    with locked(target.parent / ".matcher-download.lock"):
        if target.exists():
            try:
                return verify_matcher(target)
            except ValueError:
                if offline:
                    raise
        if offline:
            raise FileNotFoundError("离线模式未找到 Matcher 权重，请指定 --matcher-weights")
        fd, temporary = tempfile.mkstemp(prefix=".matcher-", dir=target.parent)
        os.close(fd)
        temporary = Path(temporary)
        try:
            _download(temporary, file_id=MATCHER_FILE_ID, expected_bytes=MATCHER_BYTES)
            verify_matcher(temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def _wheel_flavor():
    if sys.platform == "darwin":
        return "macos"
    if os.environ.get("CUDA_VISIBLE_DEVICES", "auto").strip() in {"", "-1"}:
        return "cpu"
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            result = subprocess.run([smi], capture_output=True, text=True, timeout=8, errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            return "cpu"
        m = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", result.stdout)
        if result.returncode == 0 and m:
            version = tuple(map(int, m.groups()))
            for minimum, tag in [((12, 8), "cu128"), ((12, 6), "cu126"), ((11, 8), "cu118")]:
                if version >= minimum:
                    return tag
    return "cpu"


def _python_version(executable):
    try:
        result = subprocess.run(
            [str(executable), "-I", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=15, errors="replace",
        )
        if result.returncode == 0:
            return tuple(map(int, result.stdout.strip().split(".")))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


def _bootstrap_candidates():
    """Find installed CPythons, keeping separate executable identities."""
    maximum = (3, 12) if sys.platform == "darwin" and platform.machine() == "x86_64" else (3, 13)
    candidates = [Path(sys.executable)]
    names = [f"python3.{minor}" for minor in range(maximum[1], 9, -1)] + ["python3", "python"]
    for name in names:
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    # which() only finds the first executable with a given name. Another
    # installation of the same Python version may have different library-load
    # permissions, so also consider later absolute PATH entries.
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(entry)
        if not entry or not directory.is_absolute():
            continue
        for name in names:
            candidate = directory / (name + ".exe" if os.name == "nt" else name)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                candidates.append(candidate)
    available, seen = [], set()
    for candidate in dict.fromkeys(candidates):
        try:
            identity = str(candidate.resolve())
        except (OSError, RuntimeError):
            continue
        if identity in seen:
            continue
        seen.add(identity)
        version = _python_version(candidate)
        if version and (3, 10) <= version <= maximum:
            available.append((candidate, version))
        if len(available) == 8:
            break
    return available


def _bootstrap_python():
    """Compatibility helper returning the first version-compatible candidate."""
    candidates = _bootstrap_candidates()
    if candidates:
        return candidates[0]
    maximum = (3, 12) if sys.platform == "darwin" and platform.machine() == "x86_64" else (3, 13)
    raise RuntimeError(f"自动安装需要兼容的 Python 3.10–3.{maximum[1]}；可通过 {PYTHON_ENV} 指定已有模型环境")


_PROBE = """
import json
import numpy, torch
from PIL import Image
from inspect import signature
assert 'weights_only' in signature(torch.load).parameters
assert tuple(__import__('sys').version_info[:2]) >= (3, 10)
image = numpy.asarray(Image.new('RGB', (4, 4)), dtype=numpy.float32)
a = torch.from_numpy(image.copy()).permute(2, 0, 1).reshape(3, -1)
assert float((a @ a.T).sum().numpy()) == 0.0
accel = 'cpu'
if torch.cuda.is_available():
    try:
        b = a.to('cuda')
        (b @ b.T).cpu().numpy()
        accel = 'cuda'
    except (RuntimeError, NotImplementedError):
        pass
if accel == 'cpu' and getattr(torch.backends, 'mps', None) and torch.backends.mps.is_available():
    try:
        b = a.to('mps')
        (b @ b.T).cpu().numpy()
        accel = 'mps'
    except (RuntimeError, NotImplementedError):
        pass
print(json.dumps(accel))
"""


def _probe(executable: Path):
    if not executable.is_file():
        return None
    try:
        run = subprocess.run([str(executable), "-I", "-c", _PROBE], capture_output=True,
                             text=True, timeout=90, errors="replace")
        if run.returncode == 0:
            device = json.loads(run.stdout.strip().splitlines()[-1])
            if device in {"cpu", "cuda", "mps"}:
                return device
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    return None


def _venv_python(path: Path) -> Path:
    return path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _pip(python, index, *packages, _quiet=False):
    subprocess.run([str(python), "-m", "pip", "--isolated", "--disable-pip-version-check",
                    "--cache-dir", str(user_cache() / "packages"),
                    "install", "--only-binary=:all:", "--index-url", index, *packages],
                   stdout=subprocess.DEVNULL if _quiet else sys.stderr,
                   stderr=subprocess.DEVNULL if _quiet else None, check=True)


def _runtime_key(bootstrap, version, flavor, torch_version):
    # A signed/hardened host Python and a normal Python may share a version.
    # Do not reuse or overwrite one interpreter's broken venv for the other.
    identity = hashlib.sha256(os.fsencode(str(bootstrap.resolve()))).hexdigest()[:12]
    return f"py{version[0]}{version[1]}-{platform.machine()}-{flavor}-torch{torch_version}-{identity}"


def _environment(offline, device="auto"):
    supplied = os.environ.get(PYTHON_ENV)
    if supplied:
        executable = Path(supplied).expanduser().absolute()
        if _probe(executable) is None:
            raise RuntimeError("指定的模型 Python 未通过 PyTorch、NumPy 和 Pillow 运行测试")
        return executable
    present = Path(sys.executable)
    local_device = _probe(present)
    flavor = "cpu" if device in {"cpu", "mps"} and sys.platform != "darwin" else _wheel_flavor()
    acceptable = lambda result: result and (offline or result != "cpu" or flavor in {"cpu", "macos"})
    if acceptable(local_device):
        return present
    runtime = user_cache() / "runtime"
    fallback = present if local_device else None
    # Reuse a successfully probed cache even online, rather than rebuilding it
    # with a currently selected but incompatible application-hosted Python.
    for prefix in sorted(runtime.glob("py*-torch*")):
        if not prefix.is_dir():
            continue
        cached = _venv_python(prefix)
        result = _probe(cached)
        if acceptable(result):
            return cached
        if result and fallback is None:
            fallback = cached
    if offline:
        raise RuntimeError(f"离线模式缺少兼容环境，请通过 {PYTHON_ENV} 选择已装好依赖的 Python")
    candidates = _bootstrap_candidates()
    # Probe already installed alternatives before downloading any packages.
    for bootstrap, _ in candidates:
        if bootstrap == present:
            continue
        result = _probe(bootstrap)
        if acceptable(result):
            return bootstrap
        if result and fallback is None:
            fallback = bootstrap
    old_mac = flavor == "macos" and platform.machine() == "x86_64"
    torch_version = "2.2.2" if old_mac else TORCH
    for number, (bootstrap, version) in enumerate(candidates, 1):
        key = _runtime_key(bootstrap, version, flavor, torch_version)
        prefix = runtime / key
        candidate = _venv_python(prefix)
        try:
            with locked(prefix.parent / (key + ".lock")):
                if _probe(candidate):
                    return candidate
                print(f"正在准备候选 Python {number} 的隔离模型环境", file=sys.stderr)
                subprocess.run([str(bootstrap), "-I", "-m", "venv", str(prefix)], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                index = PYPI if flavor == "macos" else "https://download.pytorch.org/whl/" + flavor
                if sys.platform == "linux" and platform.machine().lower() in {"arm64", "aarch64"} and flavor == "cpu":
                    index = PYPI
                _pip(candidate, PYPI, "numpy==1.26.4" if old_mac else f"numpy=={NUMPY}", f"Pillow=={PILLOW}", _quiet=True)
                try:
                    _pip(candidate, index, f"torch=={torch_version}", _quiet=True)
                except subprocess.CalledProcessError:
                    if not flavor.startswith("cu"):
                        raise
                    print("候选 CUDA 运行包不可用，尝试官方 CPU 运行包", file=sys.stderr)
                    _pip(candidate, "https://download.pytorch.org/whl/cpu", f"torch=={TORCH}", _quiet=True)
                if not _probe(candidate):
                    raise RuntimeError("Candidate model runtime probe failed")
                try:
                    _pip(candidate, PYPI, "faiss-cpu==1.11.0.post1", _quiet=True)
                except subprocess.CalledProcessError:
                    print("此候选缺少可用的 FAISS 预编译包，精确检索仍可用", file=sys.stderr)
                return candidate
        except PermissionError:
            raise RuntimeError("模型缓存不可写；请使用获准的缓存目录") from None
        except (OSError, subprocess.SubprocessError, RuntimeError):
            # Never print a failed command, interpreter path, package diagnostics
            # or application signing identifiers. No system settings are changed.
            print(f"候选 Python {number} 的安装或实际运行检查失败，尝试下一个兼容解释器", file=sys.stderr)
    if fallback is not None:
        print("加速环境未能准备，使用已通过运行检查的 CPU 环境", file=sys.stderr)
        return fallback
    raise RuntimeError(f"可用 Python 候选均未通过模型环境检查；请通过 {PYTHON_ENV} 指定兼容环境")


def prepare(weights=None, offline=False, device="auto") -> tuple[Path, Path]:
    if device not in {"auto", "cpu", "cuda", "mps"}:
        raise ValueError("设备须为 auto、cpu、cuda 或 mps")
    if os.environ.get("COPYNCE_OFFLINE", "").strip().lower() in {"1", "true", "yes"}:
        offline = True
    weight_file = _weights(weights, offline)
    return _environment(offline, device=device), weight_file


def prepare_enhanced(weights=None, matcher_weights=None, offline=False, device="auto"):
    if os.environ.get("COPYNCE_OFFLINE", "").strip().lower() in {"1", "true", "yes"}:
        offline = True
    python, descriptor = prepare(weights, offline=offline, device=device)
    return python, descriptor, _matcher_weights(matcher_weights, offline)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", type=Path)
    parser.add_argument("--matcher-weights", type=Path)
    parser.add_argument("--quality", choices=("fast", "enhanced", "deep"), default="fast")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda", "mps"), default="auto")
    parser.add_argument("--build-sn-index", type=Path, metavar="NEW_DIR",
                        help="从明确选定的独立背景图片建立私有SN索引")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--provenance", default="user-selected-independent-background")
    parser.add_argument("paths", type=Path, nargs="*")
    args = parser.parse_args()
    if args.build_sn_index:
        if not args.paths:
            parser.error("建立SN索引需要明确选择背景图片或目录")
        if args.quality != "fast" or args.matcher_weights:
            parser.error("建立背景索引只使用Descriptor，不需要Matcher参数")
        python, weights = prepare(args.weights, args.offline, args.device)
        req = dict(paths=list(map(str, args.paths)), weights=str(weights), output_dir=str(args.build_sn_index),
                   device=args.device, batch_size=args.batch_size, provenance=args.provenance)
        code = ("import json,sys;from pathlib import Path;"
                "sys.path.insert(0,sys.argv[1]);from copynce_sn import build_index;"
                "r=json.load(sys.stdin);r['paths']=list(map(Path,r['paths']));"
                "r['weights']=Path(r['weights']);r['output_dir']=Path(r['output_dir']);"
                "m=build_index(**r);print(json.dumps({'background_index_created':True,"
                "'image_count':m.get('image_count',m.get('count'))}))")
        result = subprocess.run([str(python), "-c", code, str(Path(__file__).resolve().parent)],
                                input=json.dumps(req), capture_output=True, text=True, encoding="utf-8")
        if result.returncode:
            print("背景索引建立失败；检查独立背景图片、输出位置与模型环境。", file=sys.stderr)
            return 2
        print(result.stdout.strip())
        return 0
    if args.paths:
        parser.error("图片路径仅用于 --build-sn-index")
    if args.quality == "fast":
        if args.matcher_weights:
            parser.error("--matcher-weights 需要 --quality enhanced 或 deep")
        python, weights = prepare(args.weights, args.offline, args.device)
        result = {"python": str(python), "weights": str(weights)}
    else:
        if __package__:
            from .copynce_default_sn import prepare_default_sn
        else:
            from copynce_default_sn import prepare_default_sn
        sn_index = prepare_default_sn()
        python, weights, matcher = prepare_enhanced(args.weights, args.matcher_weights, args.offline, args.device)
        result = {"python": str(python), "weights": str(weights), "matcher_weights": str(matcher),
                  "sn_index": str(sn_index)}
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

