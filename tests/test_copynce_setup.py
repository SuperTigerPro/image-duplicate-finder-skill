"""Deterministic, offline checks for the optional model bootstrap."""
import contextlib
import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import copynce_setup as setup


class WeightPreparationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.temp = directory.name
        self.base = Path(self.temp)
        self.payload = b"synthetic test content"
        self.mocking = contextlib.ExitStack()
        self.addCleanup(self.mocking.close)
        self.mocking.enter_context(mock.patch.dict(os.environ, {setup.CACHE_ENV: self.temp, "COPYNCE_WEIGHTS": ""}))
        self.mocking.enter_context(mock.patch.object(setup, "WEIGHT_BYTES", len(self.payload)))
        self.mocking.enter_context(mock.patch.object(setup, "WEIGHT_SHA256", hashlib.sha256(self.payload).hexdigest()))

    def test_existing_or_explicit_weight_offline(self):
        explicit = self.base / "external.pth"
        explicit.write_bytes(self.payload)
        with mock.patch.object(setup, "_download", side_effect=AssertionError):
            self.assertEqual(setup._weights(explicit, True), explicit.resolve())
            with self.assertRaises(FileNotFoundError):
                setup._weights(None, True)

    def test_bad_download_preserves_old_copy_and_removes_partial(self):
        destination = self.base / "weights" / setup.CHECKPOINT
        destination.parent.mkdir()
        destination.write_bytes(b"old")
        with mock.patch.object(setup, "_download", lambda p: p.write_bytes(b"bad")):
            with self.assertRaises(ValueError):
                setup._weights(None, False)
        self.assertEqual(destination.read_bytes(), b"old")
        self.assertEqual(list(destination.parent.glob(".descriptor-*")), [])

    def test_first_download_then_offline_reuse(self):
        with mock.patch.object(setup, "_download", lambda p: p.write_bytes(self.payload)):
            result = setup._weights(None, False)
        with mock.patch.object(setup, "_download", side_effect=AssertionError):
            self.assertEqual(setup._weights(None, True), result)

    def test_html_form_accepts_only_original_file_and_download_host(self):
        form = ('<form id="download-form" action="https://drive.usercontent.google.com/download">'
                '<input name="id" value="{}"><input name="confirm" value="t"></form>').format(setup.FILE_ID)
        self.assertIn("id=" + setup.FILE_ID, setup._confirmation(form.encode()))
        with self.assertRaises(ValueError):
            setup._confirmation(form.replace("drive.usercontent.google.com", "example.org").encode())
        with self.assertRaises(ValueError):
            setup._confirmation(form.replace(setup.FILE_ID, "different").encode())

    def test_no_gpu_when_explicitly_disabled(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": ""}):
            self.assertEqual(setup._wheel_flavor(), "cpu")

    def test_repository_private_runtime(self):
        checkout = Path(setup.__file__).resolve().parents[1]
        with mock.patch.dict(os.environ, {setup.CACHE_ENV: str(checkout / "runtime")}):
            with self.assertRaises(ValueError):
                setup.user_cache()

    def test_cache_rejects_another_git_worktree(self):
        project = self.base / "other-project"
        project.mkdir()
        (project / ".git").write_text("gitdir: fixture")
        with mock.patch.dict(os.environ, {setup.CACHE_ENV: str(project / "cache")}):
            with self.assertRaises(ValueError):
                setup.user_cache()

    def test_cuda_wheel_matches_driver_limit(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "0"}), \
                mock.patch.object(setup.shutil, "which", return_value="/fixture/nvidia-smi"):
            for version, expected in [("12.9", "cu128"), ("12.6", "cu126"), ("12.0", "cu118"), ("11.7", "cpu")]:
                result = setup.subprocess.CompletedProcess([], 0, stdout=f"CUDA Version: {version}", stderr="")
                with mock.patch.object(setup.subprocess, "run", return_value=result):
                    self.assertEqual(setup._wheel_flavor(), expected)

    def test_offline_can_reuse_cached_runtime_without_bootstrap(self):
        cached = setup._venv_python(self.base.resolve() / "runtime" / "py311-fixture-cpu-torch2.7.1")
        cached.parent.mkdir(parents=True)
        cached.touch()
        with mock.patch.dict(os.environ, {setup.PYTHON_ENV: ""}), \
                mock.patch.object(setup, "_probe", side_effect=lambda path: "cpu" if path == cached else None), \
                mock.patch.object(setup, "_bootstrap_python", side_effect=AssertionError), \
                mock.patch.object(setup, "_pip", side_effect=AssertionError):
            self.assertEqual(setup._environment(True), cached)

    def test_offline_missing_runtime_never_installs(self):
        with mock.patch.dict(os.environ, {setup.PYTHON_ENV: ""}), \
                mock.patch.object(setup, "_probe", return_value=None), \
                mock.patch.object(setup, "_pip", side_effect=AssertionError):
            with self.assertRaises(RuntimeError):
                setup._environment(True)

    def test_cpu_choice_does_not_prepare_cuda_environment(self):
        with mock.patch.dict(os.environ, {setup.PYTHON_ENV: ""}), \
                mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(setup, "_probe", return_value="cpu"), \
                mock.patch.object(setup, "_wheel_flavor", side_effect=AssertionError):
            self.assertEqual(setup._environment(False, device="cpu"), Path(sys.executable))

    def test_bootstrap_finds_compatible_python_without_modifying_system(self):
        found = Path(self.temp) / "python3.11"
        with mock.patch.object(setup.shutil, "which", side_effect=lambda name: str(found) if name == "python3.11" else None), \
                mock.patch.object(setup, "_python_version", side_effect=lambda path: (3, 11) if path == found else (3, 9)):
            self.assertEqual(setup._bootstrap_python(), (found, (3, 11)))

    def test_matcher_download_is_separately_verified_and_reused_offline(self):
        with mock.patch.object(setup, "MATCHER_BYTES", len(self.payload)), \
                mock.patch.object(setup, "MATCHER_SHA256", hashlib.sha256(self.payload).hexdigest()), \
                mock.patch.dict(os.environ, {"COPYNCE_MATCHER_WEIGHTS":""}):
            def download(path, **kw):
                self.assertEqual(kw["file_id"], setup.MATCHER_FILE_ID)
                path.write_bytes(self.payload)
            with mock.patch.object(setup, "_download", side_effect=download):
                path = setup._matcher_weights(None, False)
            with mock.patch.object(setup, "_download", side_effect=AssertionError("offline download")):
                self.assertEqual(setup._matcher_weights(None, True), path)
                path.write_bytes(b"tampered")
                with self.assertRaises(ValueError):
                    setup._matcher_weights(None, True)

    def test_matcher_confirmation_cannot_switch_to_descriptor_or_another_file(self):
        form = ('<form id="download-form" action="https://drive.usercontent.google.com/download">'
                '<input name="id" value="{}"><input name="confirm" value="t"></form>')
        self.assertIn(setup.MATCHER_FILE_ID, setup._confirmation(form.format(setup.MATCHER_FILE_ID).encode(), setup.MATCHER_FILE_ID))
        with self.assertRaises(ValueError):
            setup._confirmation(form.format(setup.FILE_ID).encode(), setup.MATCHER_FILE_ID)

    def test_online_reuses_probed_cached_runtime_before_bootstrap_or_install(self):
        cached = setup._venv_python(self.base.resolve() / "runtime" / "py311-fixture-cpu-torch2.7.1")
        cached.parent.mkdir(parents=True)
        cached.touch()
        with mock.patch.dict(os.environ, {setup.PYTHON_ENV: ""}), \
                mock.patch.object(setup, "_probe", side_effect=lambda path: "cpu" if path == cached else None), \
                mock.patch.object(setup, "_bootstrap_candidates", side_effect=AssertionError("unnecessary discovery")), \
                mock.patch.object(setup, "_pip", side_effect=AssertionError("unnecessary installation")):
            self.assertEqual(setup._environment(False, device="cpu"), cached)

    def test_existing_alternate_python_is_probed_before_any_new_environment(self):
        first, second = self.base / "signed-host", self.base / "alternate-python"
        with mock.patch.dict(os.environ, {setup.PYTHON_ENV: ""}), \
                mock.patch.object(setup, "_bootstrap_candidates", return_value=[(first,(3,11)),(second,(3,11))]), \
                mock.patch.object(setup, "_probe", side_effect=lambda path: "cpu" if path == second else None), \
                mock.patch.object(setup, "_pip", side_effect=AssertionError("unnecessary installation")):
            self.assertEqual(setup._environment(False, device="cpu"), second)

    def test_failed_actual_probe_tries_an_isolated_second_python_of_same_version(self):
        first, second = self.base / "signed-host", self.base / "alternate-python"
        created = []
        output = io.StringIO()
        def create(command, **kwargs):
            prefix = Path(command[-1])
            executable = setup._venv_python(prefix)
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.touch()
            created.append((Path(command[0]), executable))
            return setup.subprocess.CompletedProcess(command, 0)
        def probe(path):
            # Both base interpreters have a compatible version but lack a ready
            # runtime. Only the second venv can actually import native modules.
            return "cpu" if any(source == second and path == executable for source,executable in created) else None
        with mock.patch.dict(os.environ, {setup.PYTHON_ENV: ""}), \
                mock.patch.object(setup, "_bootstrap_candidates", return_value=[(first,(3,11)),(second,(3,11))]), \
                mock.patch.object(setup, "_probe", side_effect=probe), \
                mock.patch.object(setup.subprocess, "run", side_effect=create), \
                mock.patch.object(setup, "_pip") as install, contextlib.redirect_stderr(output):
            result = setup._environment(False, device="cpu")
        self.assertEqual([source for source,_ in created], [first,second])
        self.assertNotEqual(created[0][1], created[1][1])
        self.assertEqual(result, created[1][1])
        self.assertGreaterEqual(install.call_count, 5)
        self.assertTrue(all(call.kwargs.get("_quiet") for call in install.call_args_list))
        self.assertIn("尝试下一个", output.getvalue())
        self.assertNotIn(str(first), output.getvalue())
        self.assertNotIn(str(second), output.getvalue())
        self.assertNotIn(str(self.base), output.getvalue())

    def test_candidate_discovery_keeps_later_path_installation_of_same_version(self):
        directories = [self.base / "signed-bin", self.base / "ordinary-bin"]
        names = []
        for directory in directories:
            directory.mkdir()
            executable = directory / ("python3.11.exe" if os.name == "nt" else "python3.11")
            executable.touch()
            executable.chmod(0o700)
            names.append(executable)
        with mock.patch.dict(os.environ, {"PATH":os.pathsep.join(map(str,directories))}), \
                mock.patch.object(setup.shutil, "which", side_effect=lambda name: str(names[0]) if name == "python3.11" else None), \
                mock.patch.object(setup, "_python_version", side_effect=lambda path: (3,11) if path in names else (3,9)):
            self.assertEqual(setup._bootstrap_candidates(), [(names[0],(3,11)),(names[1],(3,11))])

    def test_explicit_incompatible_python_does_not_silently_switch(self):
        with mock.patch.dict(os.environ, {setup.PYTHON_ENV:str(self.base / "explicit-python")}), \
                mock.patch.object(setup, "_probe", return_value=None), \
                mock.patch.object(setup, "_bootstrap_candidates", side_effect=AssertionError("explicit selection")):
            with self.assertRaisesRegex(RuntimeError, "指定的模型 Python"):
                setup._environment(False)

    def test_cache_permission_failure_is_generic_and_does_not_retry_installs(self):
        first, second = self.base / "signed-host", self.base / "alternate-python"
        with mock.patch.dict(os.environ, {setup.PYTHON_ENV:""}), \
                mock.patch.object(setup, "_probe", return_value=None), \
                mock.patch.object(setup, "_bootstrap_candidates", return_value=[(first,(3,11)),(second,(3,11))]), \
                mock.patch.object(setup, "locked", side_effect=PermissionError("fixture private location")), \
                mock.patch.object(setup, "_pip", side_effect=AssertionError("permission failure must stop")):
            with self.assertRaisesRegex(RuntimeError, "模型缓存不可写") as error:
                setup._environment(False, device="cpu")
        self.assertNotIn("fixture private location", str(error.exception))


if __name__ == "__main__":
    unittest.main()

