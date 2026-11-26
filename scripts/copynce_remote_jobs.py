"""Private job lifecycle helper, sent on stdin to a POSIX SSH server."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time


BASE = Path.home() / ".cache" / "image-duplicate-finder" / "jobs-v1"
TTL = 24 * 60 * 60
ID = re.compile(r"[a-f0-9]{32}\Z")


def no_links(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Private jobs cannot use symbolic-link paths")


def private_base():
    no_links(BASE)
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = BASE.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Job root must be a private directory owned by this account")


def target(job_id):
    if not isinstance(job_id, str) or not ID.fullmatch(job_id):
        raise ValueError("Invalid job ID")
    return BASE / job_id


def verified_job(job):
    no_links(BASE)
    if job.parent != BASE or not ID.fullmatch(job.name) or job.is_symlink():
        return False
    try:
        info = job.lstat()
        marker = job / "owner"
        mark = marker.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or not stat.S_ISREG(mark.st_mode)
                or mark.st_uid != os.getuid() or mark.st_size != 32 or mark.st_mode & 0o077):
            return False
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(marker, flags), "r") as source:
            return source.read(33) == job.name
    except FileNotFoundError:
        return False


@contextmanager
def active_job(job):
    """Hold the owned job marker while a multi-stage worker is using its files."""
    if not verified_job(job):
        raise ValueError("Unrecognized private job")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(job / "owner", flags), "rb") as handle:
        if os.name == "posix":
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_SH)
        try:
            yield
        finally:
            if os.name == "posix":
                fcntl.flock(handle, fcntl.LOCK_UN)


def remove_job(job):
    if not verified_job(job):
        return False
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        with os.fdopen(os.open(job / "owner", flags), "rb") as handle:
            if os.name == "posix":
                import fcntl
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
            if not verified_job(job):
                return False
            shutil.rmtree(job)
        return True
    except FileNotFoundError:
        return False


def reap(now):
    count = 0
    for job in BASE.iterdir():
        if not ID.fullmatch(job.name) or job.is_symlink() or not job.is_dir():
            continue
        try:
            if now - (job / "owner").lstat().st_mtime >= TTL:
                count += int(remove_job(job))
        except FileNotFoundError:
            continue
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["create", "inventory", "cache-ready", "remove", "reap"])
    parser.add_argument("job_id", nargs="?")
    parser.add_argument("settings", nargs="?")
    args = parser.parse_args()
    os.umask(0o077)
    private_base()
    if args.operation == "reap":
        return reap(time.time())
    job = target(args.job_id)
    if args.operation == "remove":
        remove_job(job)
        if os.path.lexists(job):
            raise ValueError("Could not verify and remove private job")
        return None
    if args.operation == "cache-ready":
        cache = job / "cache.sqlite3"
        return verified_job(job) and cache.is_file() and not cache.is_symlink() and cache.stat().st_size > 0
    images = job / "images"
    if args.operation == "inventory":
        if not verified_job(job):
            raise ValueError("Unrecognized private job")
        no_links(images)
        return {f.name: f.stat().st_size for f in images.iterdir() if not f.is_symlink() and f.is_file()}
    return create_job(args.job_id, json.loads(args.settings))


def create_job(job_id, settings, *, allow_auxiliary=False):
    """Only the trusted server supplies persistent read-only auxiliary resources."""
    os.umask(0o077)
    private_base()
    reap(time.time())
    job = target(job_id)
    images = job / "images"
    allowed = {"weights", "device", "batch_size", "max_pixels"}
    if allow_auxiliary:
        allowed |= {"matcher_weights", "sn_index"}
    cfg = dict(settings)
    if (not isinstance(cfg, dict) or set(cfg) - allowed
            or not isinstance(cfg.get("weights"), str) or not Path(cfg["weights"]).is_absolute()):
        raise ValueError("Invalid private runtime settings")
    for key in ("weights", "matcher_weights", "sn_index"):
        if key in cfg:
            if not isinstance(cfg[key], str) or not Path(cfg[key]).is_absolute():
                raise ValueError("Invalid private model resource")
            no_links(Path(cfg[key]))
    job.mkdir(mode=0o700)
    try:
        (job / "owner").write_text(job.name)
        (job / "owner").chmod(0o600)
        images.mkdir(mode=0o700)
        config = job / "config.json"
        cfg.update(image_roots=[str(images)], cache=str(job / "cache.sqlite3"))
        config.write_text(json.dumps(cfg))
        config.chmod(0o600)
        # The watchdog owns only this random job. Reaping on future operations
        # also covers a machine reboot in which the OS terminated the watchdog.
        watchdog = """import fcntl, pathlib, shutil, sys, time
job = pathlib.Path(sys.argv[1])
marker = job / 'owner'
deadline = time.monotonic() + int(sys.argv[2])
while marker.is_file() and not job.is_symlink():
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(min(60, remaining))
        continue
    if any(p.is_symlink() for p in (job, *job.parents)) or marker.is_symlink():
        break
    try:
        with marker.open('rb') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                time.sleep(60)
                continue
            if marker.stat().st_size == 32 and marker.read_text() == job.name:
                shutil.rmtree(job)
        break
    except FileNotFoundError:
        break
"""
        subprocess.Popen([sys.executable, "-c", watchdog, str(job), str(TTL)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        return {"directory": str(images), "config": str(config), "cache": cfg["cache"]}
    except BaseException:
        shutil.rmtree(job, ignore_errors=True)
        raise


if __name__ == "__main__":
    print(json.dumps(main()))
