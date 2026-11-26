"""Audit the Git index or a committed tree and create/verify a public Skill ZIP."""
import argparse
import io
import ipaddress
import os
from pathlib import Path
import re
import subprocess
import sys
import stat
from urllib.parse import urlsplit
import zipfile


# A new public file requires review. Runtime caches, weights and reports never belong here.
PUBLIC_FILES = set("""
.gitattributes .gitignore .github/workflows/tests.yml .githooks/pre-commit .githooks/pre-push
LICENSE README.md SKILL.md TESTING.md start-mcp agents/openai.yaml
adapters/chat-completions-tools.json
references/client-compatibility.md references/report-format.md references/similarity.md
references/universal-agent-prompt.md references/copynce.md requirements-pixels.txt requirements-similar.txt requirements-copynce.txt
scripts/check_public_files.py scripts/find_duplicates.py scripts/image_similarity.py scripts/tool_adapter.py
scripts/copynce_cli.py scripts/copynce_cloud.py scripts/copynce_engine.py scripts/copynce_model.py
scripts/copynce_remote_jobs.py scripts/copynce_report.py scripts/copynce_server.py scripts/copynce_setup.py
scripts/copynce_matcher.py scripts/copynce_sn.py scripts/copynce_pipeline.py
tests/test_check_public_files.py tests/test_copynce_cli.py tests/test_copynce_cloud.py
tests/test_copynce_engine.py tests/test_copynce_server.py tests/test_copynce_setup.py tests/test_copynce_model.py
tests/test_copynce_matcher.py tests/test_copynce_sn.py tests/test_copynce_pipeline.py tests/test_copynce_remote_jobs.py
tests/test_find_duplicates.py tests/test_image_similarity.py tests/test_skill_package.py
tests/test_tool_adapter.py third_party/CopyNCE-LICENSE
""".split())
EXECUTABLES = {"start-mcp", ".githooks/pre-commit", ".githooks/pre-push"}
MAX_BYTES = 1024 * 1024
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

# Reviewed upstream services for fetching public code, weights and dependencies.
PUBLIC_DOMAINS = {
    "github.com", "raw.githubusercontent.com", "img.shields.io", "agentskills.io",
    "www.apache.org", "www.w3.org", "pypi.org", "files.pythonhosted.org",
    "pytorch.org", "docs.pytorch.org", "download.pytorch.org",
    "drive.google.com", "drive.usercontent.google.com", "docs.google.com",
    "modelcontextprotocol.io", "man.openbsd.org", "arxiv.org",
}
EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net", "example", "invalid", "test")
CHECKS = {
    "private key": re.compile(r"-----BEGIN (?:[\w ]+ )?PRIVATE KEY-----"),
    "embedded key or binary": re.compile(r"(?i)(?:data:[a-z]+/[^,\s]{1,100};base64,|(?:ssh-(?:ed25519|rsa)|ecdsa-sha2-[\w-]+)\s+[A-Za-z0-9+/]{32,}|(?<![\w/])[A-Za-z0-9+/]{128,}={0,2})"),
    "credential token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|sk-(?:proj-)?[A-Za-z0-9_-]{24,})"),
    "bearer credential": re.compile(r"(?i)\bBearer\s+[a-z0-9._~+/-]{16,}={0,2}"),
    "JWT credential": re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{16,}\b"),
    "literal password or secret": re.compile(r"""(?ix)\b(?:password|passwd|passphrase|api[_-]?key|access[_-]?token|client[_-]?secret)\b["']?\s*[:=]\s*["'][^"'\s][^"'\r\n]{0,255}["']"""),
    "home directory": re.compile(r"""(?ix)(?:/(?:Users|home)/|/(?:root|var/root)/|[a-z]:(?:\\+|/)Users(?:\\+|/))[\w.\-\u0080-\uffff]+"""),
    "embedded login": re.compile(r"""(?i)(?<![\w.])[a-z_][a-z0-9._-]*@[a-z0-9][a-z0-9.-]*(?!\w)"""),
    "literal connection setting": re.compile(r"""(?ix)["'](?:host|hostname|user|username|identity_file|jump_host)["']\s*:\s*["']([^"'\r\n]+)["']"""),
    "literal connection assignment": re.compile(r"""(?ix)\b(?:host|hostname|user|username|login_user|ssh_host|server_ip|ip_address|identity_file|jump_host)\b\s*=\s*["']([^"'\r\n]+)["']"""),
    "unquoted connection setting": re.compile(r"(?im)^\s*(?:host|hostname|user|username|login_user|ssh_host|server_ip|ip_address|identity_file|jump_host)\s*:\s*([^\s\"'#{}]+)\s*(?:#.*)?$"),
    "private data directory": re.compile(r"""(?ix)(?:/(?:DataWarehouse\d*|Volumes|mnt|media|srv|scratch|private|data)/|[a-z]:(?:\\+|/)(?:data|private|projects)(?:\\+|/))[\w.\-\u0080-\uffff]+"""),
    "shell credential": re.compile(r"""(?i)\b(?:password|passwd|passphrase|api[_-]?key|access[_-]?token|client[_-]?secret)=(?!["'\s$<{])[-a-z0-9._+/=]{4,}"""),
    "SSH config value": re.compile(r"(?im)^\s*(?:HostName|IdentityFile|ProxyJump|User)\s+([^\s#]+)\s*(?:#.*)?$"),
    "internal DNS name": re.compile(r"(?i)\b[a-z][a-z0-9.-]*\.(?:internal|corp|lan|local)\b"),
    "literal SSH destination": re.compile(r"""(?im)(?:^|[\x60$>])\s*ssh\s+([\w.][\w.-]*)(?=\s|[\x60'"]|$)"""),
}
URL = re.compile(r"""(?i)(?:https?|ssh|sftp|ftp)://[^\s)"'<>\x60\]]+""")
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
IPV6 = re.compile(r"(?<![\w:])(?:[a-fA-F0-9]{0,4}:){2,7}[a-fA-F0-9]{0,4}(?![\w:])")


def example_host(value):
    return any(value == suffix or value.endswith("." + suffix) for suffix in EXAMPLE_DOMAINS)


def finding(line, private_terms):
    if any(term in line.casefold() for term in private_terms):
        return "site-specific value"
    for kind, pattern in CHECKS.items():
        for match in pattern.finditer(line):
            if kind == "embedded login":
                account, host = match.group(0).lower().split("@")
                if host.startswith("v") and host[1:].isdigit() and "uses:" in line:
                    continue
                if (account in {"user", "username", "researcher"}
                        and (host in {"host", "server"} or example_host(host))):
                    continue
            if kind in {"literal connection setting", "literal connection assignment", "unquoted connection setting",
                        "literal SSH destination", "SSH config value"}:
                value = match.group(1)
                if kind == "unquoted connection setting" and value in {"str", "int", "Path", "None"}:
                    continue  # Reviewed Python type annotations, not configured destinations.
                if (example_host(value.lower()) or value.startswith("<") and value.endswith(">")
                        or value in {"USER", "HOST", "SERVER", "USER@SERVER"}):
                    continue
            return kind
    for match in URL.finditer(line):
        url = urlsplit(match.group())
        if url.username or url.password or (url.hostname not in PUBLIC_DOMAINS and
                                           not example_host(url.hostname or "")):
            return "unreviewed network address"
    for pattern in (IPV4, IPV6):
        for match in pattern.finditer(line):
            try:
                ipaddress.ip_address(match.group())
            except ValueError:
                continue
            return "IP address"
    return None


def git(*args):
    try:
        return subprocess.run(["git", *args], capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        raise ValueError("Git source query failed; inspect the repository locally (details withheld)") from None


def commit_id(revision):
    result = git("rev-parse", "--verify", "--end-of-options", revision + "^{commit}").strip().decode("ascii")
    if not OID.fullmatch(result):
        raise ValueError("Invalid Git commit identity (value withheld)")
    return result


def snapshot(commit, working_tree=False):
    if working_tree:
        # Git excludes disposable local artifacts. Nothing outside the reviewed source list is packaged.
        records = git("ls-files", "--cached", "--others", "--exclude-standard", "--deduplicate", "-z")
        files = {}
        for raw_name in records.split(b"\0"):
            if not raw_name:
                continue
            try:
                name = raw_name.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("Non-UTF-8 publication path (filename withheld)") from None
            if name not in PUBLIC_FILES:
                raise ValueError("Unapproved publication path (filename withheld); inspect the file list locally")
            path = Path(name)
            if path.is_symlink() or not path.is_file() or any(p.is_symlink() for p in path.parents):
                raise ValueError(f"{name}: expected a regular source file")
            info = path.stat()
            if info.st_size > MAX_BYTES:
                raise ValueError(f"{name}: source file too large")
            if os.name != "nt" and name in EXECUTABLES and not info.st_mode & stat.S_IXUSR:
                raise ValueError(f"{name}: missing executable permission")
            files[name] = path.read_bytes()
        if "SKILL.md" not in files:
            raise ValueError("Missing SKILL.md")
        return files
    if commit is None:
        records = git("ls-files", "--stage", "-z")
    else:
        records = git("ls-tree", "--full-tree", "-rz", commit)
    files = {}
    for record in records.split(b"\0"):
        if not record:
            continue
        header, filename = record.split(b"\t", 1)
        try:
            name = filename.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Non-UTF-8 publication path (filename withheld)") from None
        mode, kind_or_oid, oid_or_stage = header.split()
        if name not in PUBLIC_FILES:
            raise ValueError("unapproved publication path (filename withheld); inspect the file list locally")
        if mode not in {b"100644", b"100755"} or (commit is None and oid_or_stage != b"0"):
            raise ValueError(f"{name}: expected a regular, merged source file")
        if name in EXECUTABLES and mode != b"100755":
            raise ValueError(f"{name}: missing executable permission")
        oid = kind_or_oid if commit is None else oid_or_stage
        if int(git("cat-file", "-s", oid)) > MAX_BYTES:
            raise ValueError(f"{name}: source file too large")
        files[name] = git("cat-file", "blob", oid)
    if "SKILL.md" not in files:
        raise ValueError("missing SKILL.md")
    return files


def audit(files, private_terms):
    for name, data in files.items():
        if name not in PUBLIC_FILES:
            raise ValueError("Unapproved publication path (filename withheld)")
        if len(data) > MAX_BYTES:
            raise ValueError(f"{name}: source file too large")
        if b"\0" in data:
            raise ValueError(f"{name}: binary payload")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"{name}: expected UTF-8 source") from None
        for number, line in enumerate(text.splitlines(), 1):
            kind = finding(line, private_terms)
            if kind:
                raise ValueError(f"{name}:{number}: {kind} (value withheld)")


def require_release(files):
    missing = PUBLIC_FILES - files.keys()
    if missing:
        # These are reviewed public names, never arbitrary filenames from a user's computer.
        raise ValueError("Incomplete release; missing public resources: " + ", ".join(sorted(missing)))


def audit_metadata(revision, private_terms):
    metadata = git("show", "-s", "--format=%an%n%ae%n%cn%n%ce%n%B", revision).decode("utf-8")
    if any(term in metadata.casefold() for term in private_terms):
        raise ValueError("Commit metadata contains a site-specific value (withheld)")
    message = git("show", "-s", "--format=%B", revision).decode("utf-8")
    if any(finding(line, private_terms) for line in message.splitlines()):
        raise ValueError("Commit message contains a sensitive value (withheld)")


def audit_push(updates, private_terms):
    reviewed = set()
    for line in updates:
        fields = line.split()
        if len(fields) != 4:
            raise ValueError("Invalid push update record (values withheld)")
        local_ref, local_oid, remote_ref, remote_oid = fields
        if not OID.fullmatch(local_oid) or not OID.fullmatch(remote_oid):
            raise ValueError("Invalid push object identity (values withheld)")
        if any(finding(ref, private_terms) for ref in (local_ref, remote_ref)):
            raise ValueError("Push ref contains a sensitive value (withheld)")
        if set(local_oid) == {"0"}:
            continue
        object_oid = local_oid
        kind = git("cat-file", "-t", object_oid).strip()
        tags_seen = set()
        while kind == b"tag":
            if object_oid in tags_seen:
                raise ValueError("Invalid nested tag reference")
            tags_seen.add(object_oid)
            metadata = git("cat-file", "-p", object_oid).decode("utf-8")
            if any(term in metadata.casefold() for term in private_terms):
                raise ValueError("Tag metadata contains a site-specific value (withheld)")
            message = metadata.partition("\n\n")[2]
            if any(finding(row, private_terms) for row in message.splitlines()):
                raise ValueError("Tag message contains a sensitive value (withheld)")
            object_oid = metadata.splitlines()[0].removeprefix("object ")
            if not OID.fullmatch(object_oid):
                raise ValueError("Invalid nested tag identity (value withheld)")
            kind = git("cat-file", "-t", object_oid).strip()
        if kind != b"commit":
            raise ValueError("Only commit and annotated-tag updates are supported")
        head = commit_id(local_oid)
        rev_args = [head]
        if set(remote_oid) != {"0"}:
            # An unavailable remote base fails closed instead of silently checking no commits.
            rev_args.append("^" + commit_id(remote_oid))
        revisions = git("rev-list", *rev_args).decode("ascii").splitlines()
        for revision in revisions:
            if revision not in reviewed:
                audit_metadata(revision, private_terms)
                audit(snapshot(revision), private_terms)
                reviewed.add(revision)
    return len(reviewed)


def archive_bytes(files):
    # One canonical stored representation excludes extra data, symlinks and workstation metadata.
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(name)
            entry.create_system = 3
            entry.external_attr = (0o100755 if name in EXECUTABLES else 0o100644) << 16
            archive.writestr(entry, data)
    return stream.getvalue()


def verify_zip(path, files):
    require_release(files)
    audit(files, [])
    expected = archive_bytes(files)
    with path.open("rb") as stream:
        if stream.read(len(expected) + 1) != expected:
            raise ValueError("archive bytes differ from the reviewed public package")


def write_zip(path, files):
    require_release(files)
    audit(files, [])
    with path.open("xb") as stream:
        stream.write(archive_bytes(files))
    verify_zip(path, files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--commit", metavar="REV", help="audit a full committed tree")
    sources.add_argument("--staged", action="store_true", help="audit the index (default)")
    sources.add_argument("--working-tree", action="store_true", help="audit current source, including untracked public files")
    sources.add_argument("--pre-push", action="store_true", help="audit all outgoing commits from Git's hook stdin")
    parser.add_argument("--private-terms-file", type=Path, default=os.environ.get("IMAGE_SKILL_PRIVATE_TERMS_FILE"),
                        help="outside repository; one private identifier per line")
    archive = parser.add_mutually_exclusive_group()
    archive.add_argument("--write-archive", type=Path)
    archive.add_argument("--archive", type=Path, help="verify a public ZIP against source")
    args = parser.parse_args()
    dest = args.archive or args.write_archive
    dest = dest.resolve() if dest else None
    private_file = args.private_terms_file.resolve() if args.private_terms_file else None
    root = Path(os.fsdecode(git("rev-parse", "--show-toplevel").rstrip(b"\n"))).resolve()
    os.chdir(root)
    terms = []
    if private_file:
        if private_file.is_relative_to(root) or any((p / ".git").exists() for p in private_file.parents):
            raise ValueError("Private terms file must be outside all Git worktrees")
        info = private_file.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES or
                (os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077))):
            raise ValueError("Private terms file must be a small, user-owned private regular file")
        terms = [line.strip().casefold() for line in private_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.pre_push:
        if dest:
            raise ValueError("Archive operations cannot be combined with a push audit")
        count = audit_push(sys.stdin, terms)
        print(f"Public push verified: {count} outgoing commits.")
        return
    revision = commit_id(args.commit) if args.commit else None
    if revision:
        audit_metadata(revision, terms)
    files = snapshot(revision, working_tree=args.working_tree)
    audit(files, terms)
    if args.write_archive:
        if dest.is_relative_to(root):
            raise ValueError("Store public release archives outside the checkout")
        write_zip(dest, files)
    elif args.archive:
        require_release(files)
        verify_zip(dest, files)
    print(f"Public tree verified: {len(files)} source files.")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"Privacy check failed: {error}", file=sys.stderr)
        sys.exit(1)
    except (OSError, UnicodeError, zipfile.BadZipFile):
        print("Privacy check failed: source or archive could not be safely read (details withheld)", file=sys.stderr)
        sys.exit(1)
