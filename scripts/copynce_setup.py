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


def _confirmation(html: bytes) -> str:
    form = _DriveForm()
    form.feed(html.decode("utf-8", errors="replace"))
    action = urllib.parse.urlparse(form.action)
    if ((action.scheme, action.netloc, action.path) != ("https", "drive.usercontent.google.com", "/download")
            or form.fields.get("id") != FILE_ID or not form.fields.get("confirm")):
        raise ValueError("Google Drive 未返回公开的 Descriptor 权重，可使用离线权重文件")
    return DRIVE + "?" + urllib.parse.urlencode(form.fields)


def _download(destination: Path):
    url = DRIVE + "?" + urllib.parse.urlencode({"id": FILE_ID, "export": "download", "confirm": "t"})
    # A cookie jar permits Drive's optional public-file confirmation challenge.
    import http.cookiejar
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    for attempt in range(2):
        request = urllib.request.Request(url, headers={"User-Agent": "image-duplicate-finder/CopyNCE"})
        with opener.open(request, timeout=60) as response:
            if response.headers.get_content_type() == "text/html":
                if attempt:
                    raise ValueError("Google Drive 当前无法提供权重，可使用离线权重文件")
                url = _confirmation(response.read(512 * 1024))
                continue
            total = 0
            with destination.open("wb") as stream:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > WEIGHT_BYTES:
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


def _bootstrap_python():
    """Reuse a compatible interpreter; do not install or modify system Python."""
    maximum = (3, 12) if sys.platform == "darwin" and platform.machine() == "x86_64" else (3, 13)
    candidates = [Path(sys.executable)]
    for minor in range(maximum[1], 9, -1):
        found = shutil.which(f"python3.{minor}")
        if found:
            candidates.append(Path(found))
    for candidate in dict.fromkeys(candidates):
        version = _python_version(candidate)
        if version and (3, 10) <= version <= maximum:
            return candidate, version
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


def _pip(python, index, *packages):
    subprocess.run([str(python), "-m", "pip", "--isolated", "--disable-pip-version-check",
                    "--cache-dir", str(user_cache() / "packages"),
                    "install", "--only-binary=:all:", "--index-url", index, *packages],
                   stdout=sys.stderr, check=True)


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
    if local_device and (offline or local_device != "cpu" or flavor in {"cpu", "macos"}):
        return present
    if offline:
        runtime = user_cache() / "runtime"
        for prefix in sorted(runtime.glob("py*-torch*")):
            cached = _venv_python(prefix)
            if _probe(cached):
                return cached
        raise RuntimeError(f"离线模式缺少兼容环境，请通过 {PYTHON_ENV} 选择已装好依赖的 Python")
    bootstrap, version = _bootstrap_python()
    old_mac = flavor == "macos" and platform.machine() == "x86_64"
    torch_version = "2.2.2" if old_mac else TORCH
    key = f"py{version[0]}{version[1]}-{platform.machine()}-{flavor}-torch{torch_version}"
    prefix = user_cache() / "runtime" / key
    candidate = _venv_python(prefix)
    with locked(prefix.parent / (key + ".lock")):
        if _probe(candidate):
            return candidate
        if offline:
            raise RuntimeError(f"离线模式缺少兼容环境，请通过 {PYTHON_ENV} 选择已装好依赖的 Python")
        subprocess.run([str(bootstrap), "-I", "-m", "venv", str(prefix)], check=True,
                       stdout=sys.stderr)
        index = PYPI if flavor == "macos" else "https://download.pytorch.org/whl/" + flavor
        if sys.platform == "linux" and platform.machine().lower() in {"arm64", "aarch64"} and flavor == "cpu":
            index = PYPI
        _pip(candidate, PYPI, "numpy==1.26.4" if old_mac else f"numpy=={NUMPY}", f"Pillow=={PILLOW}")
        try:
            _pip(candidate, index, f"torch=={torch_version}")
        except subprocess.CalledProcessError:
            if not flavor.startswith("cu"):
                raise
            if local_device:
                print("CUDA 环境准备失败，使用现有 CPU 环境", file=sys.stderr)
                return present
            print("CUDA 运行包不可用，尝试官方 CPU 运行包", file=sys.stderr)
            _pip(candidate, "https://download.pytorch.org/whl/cpu", f"torch=={TORCH}")
        if not _probe(candidate):
            raise RuntimeError(f"自动准备的环境未通过推理运行测试，可通过 {PYTHON_ENV} 指定兼容解释器")
        try:
            _pip(candidate, PYPI, "faiss-cpu==1.11.0.post1")
        except subprocess.CalledProcessError:
            print("此平台缺少 FAISS 预编译包，精确检索仍可用", file=sys.stderr)
    return candidate


def prepare(weights=None, offline=False, device="auto") -> tuple[Path, Path]:
    if device not in {"auto", "cpu", "cuda", "mps"}:
        raise ValueError("设备须为 auto、cpu、cuda 或 mps")
    if os.environ.get("COPYNCE_OFFLINE", "").strip().lower() in {"1", "true", "yes"}:
        offline = True
    weight_file = _weights(weights, offline)
    return _environment(offline, device=device), weight_file


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda", "mps"), default="auto")
    args = parser.parse_args()
    python, weights = prepare(args.weights, args.offline, args.device)
    print(json.dumps({"python": str(python), "weights": str(weights)}, ensure_ascii=False))

