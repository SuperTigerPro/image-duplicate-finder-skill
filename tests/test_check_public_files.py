import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_public_files.py"
SPEC = importlib.util.spec_from_file_location("public_guard", SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class PublicPackageTests(unittest.TestCase):
    def test_connection_and_credential_patterns(self):
        dotted = ".".join(["203", "0", "113", "17"])
        examples = [
            dotted, ":".join(["2001", "db8", "", "1"]),
            "ssh " + "private-fixture", "analyst" + "@" + "private-fixture",
            "https" + "://" + "lab-fixture" + ".internal/uploads",
            "/" + "home/" + "test-account/private",
            "C:" + "\\Users\\" + "test-account\\.ssh",
            json.dumps({"ho" + "st": "internal-fixture"}),
            '"password": ' + '"fixture-value"',
            "-----BEGIN " + "OPENSSH " + "PRIVATE KEY-----",
            "user" + ": " + "private-fixture",
            "username" + " = " + '"private-fixture"',
            "/" + "data/" + "private-fixture/images",
            "sftp" + "://" + "private-fixture/files",
            "Bearer " + "synthetic-credential-value",
            "eyJ" + "a" * 16 + "." + "b" * 16 + "." + "c" * 20,
        ]
        for value in examples:
            with self.subTest(value=value):
                self.assertIsNotNone(guard.finding(value, []))
        self.assertIsNone(guard.finding('{"host":"server.example.org"}', []))
        self.assertIsNone(guard.finding("host: str", []))
        self.assertEqual(guard.finding("Internal ID: HELLO-FIXTURE", ["hello-fixture"]), "site-specific value")

    def test_staged_and_committed_bytes_and_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            root.mkdir()
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
            # Git exports its own worktree/index variables inside hooks; this fixture uses its own repository.
            for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "IMAGE_SKILL_PRIVATE_TERMS_FILE"):
                env.pop(key, None)

            def git(*args):
                subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, check=True)

            def audit(*args):
                return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=root, env=env,
                                      capture_output=True, text=True)

            git("init", "-q")
            git("config", "user.name", "Example")
            git("config", "user.email", "fixture" + "@" + "example.org")
            for name in guard.PUBLIC_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("public source\n")
                if name in guard.EXECUTABLES:
                    path.chmod(0o755)
            skill = root / "SKILL.md"
            git("add", ".")
            git("update-index", "--chmod=+x", *sorted(guard.EXECUTABLES))
            git("commit", "-qm", "Public baseline")
            archive = base / "public.zip"
            self.assertEqual(audit("--staged", "--write-archive", archive).returncode, 0)
            skill.write_text("private network " + ".".join(["203", "0", "113", "19"]))
            self.assertEqual(audit("--staged", "--archive", archive).returncode, 0)
            git("add", "SKILL.md")
            skill.write_text("public again")
            blocked = audit("--staged")
            self.assertNotEqual(blocked.returncode, 0)
            self.assertNotIn("113", blocked.stderr)
            self.assertEqual(audit("--commit", "HEAD", "--archive", archive).returncode, 0)
            with archive.open("ab") as stream:
                stream.write(b"unexpected bytes")
            self.assertNotEqual(audit("--commit", "HEAD", "--archive", archive).returncode, 0)
            (root / "profile.json").write_text("{}")
            git("add", ".")
            self.assertNotEqual(audit().returncode, 0)

    def test_binary_unknown_and_incomplete_packages_fail_without_values(self):
        invalid = [
            {"SKILL.md": b"\xff"},
            {"SKILL.md": b"source\0payload"},
            {"SKILL.md": b"public", "private-fixture.json": b"{}"},
        ]
        for files in invalid:
            with self.subTest(paths=len(files)), self.assertRaises(ValueError) as raised:
                guard.audit(files, [])
            self.assertNotIn("private-fixture", str(raised.exception))
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "public.zip"
            with self.assertRaisesRegex(ValueError, "Incomplete release"):
                guard.write_zip(destination, {"SKILL.md": b"public"})
            self.assertFalse(destination.exists())

    def test_worktree_checks_untracked_files_and_symlinks(self):
        with self.repository() as (root, git, audit, env):
            (root / "SKILL.md").write_text("public\n")
            git("add", ".")
            (root / "README.md").write_text("public\n")
            self.assertEqual(audit("--working-tree").returncode, 0)
            (root / "private-fixture.json").write_text("{}")
            denied = audit("--working-tree")
            self.assertNotEqual(denied.returncode, 0)
            self.assertNotIn("private-fixture", denied.stderr)
            (root / "private-fixture.json").unlink()
            (root / "README.md").unlink()
            try:
                (root / "README.md").symlink_to("SKILL.md")
            except (OSError, NotImplementedError):
                return
            self.assertNotEqual(audit("--working-tree").returncode, 0)

    def test_push_checks_intermediate_commits_and_tag_messages(self):
        with self.repository() as (root, git, audit, env):
            skill = root / "SKILL.md"
            skill.write_text("public\n")
            git("add", ".")
            git("commit", "-qm", "Public baseline")
            base = git("rev-parse", "HEAD").stdout.strip()
            skill.write_text("private network " + ".".join(["203", "0", "113", "19"]))
            git("add", ".")
            git("commit", "-qm", "Temporary source")
            skill.write_text("public again\n")
            git("add", ".")
            git("commit", "-qm", "Restore source")
            head = git("rev-parse", "HEAD").stdout.strip()
            self.assertEqual(audit("--commit", head).returncode, 0)
            update = f"refs/heads/main {head} refs/heads/main {base}\n"
            result = audit("--pre-push", input=update)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("113", result.stderr)
            git("tag", "-a", "public-tag", "-m", "server " + ".".join(["203", "0", "113", "20"]))
            tag = git("rev-parse", "public-tag").stdout.strip()
            result = audit("--pre-push", input=f"refs/tags/public-tag {tag} refs/tags/public-tag {'0'*40}\n")
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("113", result.stderr)
            git("tag", "-a", "wrapper-tag", "public-tag", "-m", "Public wrapper")
            wrapper = git("rev-parse", "wrapper-tag").stdout.strip()
            result = audit("--pre-push", input=f"refs/tags/wrapper-tag {wrapper} refs/tags/wrapper-tag {'0'*40}\n")
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("113", result.stderr)

    def test_push_missing_remote_base_fails_closed(self):
        with self.repository() as (root, git, audit, env):
            (root / "SKILL.md").write_text("public\n")
            git("add", ".")
            git("commit", "-qm", "Public baseline")
            head = git("rev-parse", "HEAD").stdout.strip()
            self.assertEqual(audit("--pre-push", input=f"refs/heads/main {head} refs/heads/main {'0'*40}\n").returncode, 0)
            self.assertNotEqual(audit("--pre-push", input=f"refs/heads/main {head} refs/heads/main {'f'*40}\n").returncode, 0)

    def test_commit_message_and_local_identifier_file(self):
        with self.repository() as (root, git, audit, env):
            (root / "SKILL.md").write_text("public\n")
            git("add", ".")
            git("commit", "-qm", "review hello-fixture")
            terms = root.parent / "private-terms.txt"
            terms.write_text("HELLO-FIXTURE\n")
            terms.chmod(0o600)
            denied = audit("--commit", "HEAD", "--private-terms-file", str(terms))
            self.assertNotEqual(denied.returncode, 0)
            self.assertNotIn("hello-fixture", denied.stderr)

    @staticmethod
    def repository():
        import contextlib

        @contextlib.contextmanager
        def create():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "repo"
                root.mkdir()
                env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
                for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "IMAGE_SKILL_PRIVATE_TERMS_FILE"):
                    env.pop(key, None)

                def git(*args):
                    return subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, text=True, check=True)

                def audit(*args, input=None):
                    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], cwd=root, env=env,
                                          input=input, capture_output=True, text=True)

                git("init", "-q")
                git("config", "user.name", "Example")
                git("config", "user.email", "fixture" + "@" + "example.org")
                yield root, git, audit, env
        return create()

