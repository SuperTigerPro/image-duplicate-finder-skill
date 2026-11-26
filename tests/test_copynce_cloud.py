from contextlib import asynccontextmanager
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zlib


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import copynce_cloud as cloud
import copynce_remote_jobs as jobs


def png_fixture():
    def chunk(tag, value):
        return (struct.pack(">I", len(value)) + tag + value
                + struct.pack(">I", zlib.crc32(tag + value) & 0xffffffff))
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\0\x12\xde\x89")) + chunk(b"IEND", b"")


class CloudTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.profile = self.home / "profile.json"
        self.profile.write_text(json.dumps({"host": "demo.example.org", "launcher": "/opt/copy check/start-mcp", "port": 2233}))
        self.profile.chmod(0o600)
        self.src = self.home / "a b[1].png"
        self.src.write_bytes(png_fixture())

    def test_private_profile_and_arguments(self):
        cfg = cloud.load_profile(self.profile)
        self.assertEqual(cloud._ssh(cfg, [cfg.launcher])[-2:], ["demo.example.org", "'/opt/copy check/start-mcp'"])
        self.assertIn("Port=2233", cfg.options)
        self.assertIn("StrictHostKeyChecking=yes", cfg.options)
        self.profile.write_text(json.dumps({"pass"+"word": "example"}))
        with self.assertRaisesRegex(ValueError, "Unknown SSH profile field"):
            cloud.load_profile(self.profile)
        if os.name == "posix":
            self.profile.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "chmod"):
                cloud.load_profile(self.profile)

    def test_selection_gate(self):
        with mock.patch.object(cloud, "_info", new=mock.AsyncMock()) as info:
            with self.assertRaisesRegex(ValueError, "authorization"):
                cloud.cloud_scan(self.profile, paths=[self.src])
            with self.assertRaisesRegex(ValueError, "one remote"):
                cloud.cloud_scan(self.profile, paths=[self.src], remote_directory="gallery")
            info.assert_not_called()

    def test_snapshot_symlink_and_selection_stamp(self):
        copy = self.home / "snapshot"
        cloud._snapshot(self.src, copy)
        self.assertEqual(copy.read_bytes(), self.src.read_bytes())
        if os.name == "posix":
            link = self.home / "link.png"
            link.symlink_to(self.src)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                cloud._snapshot(link, self.home / "second")
        with self.assertRaisesRegex(OSError, "after approval"):
            cloud._snapshot(self.src, self.home / "changed", (0, 0, 0, 0, 0))

    def test_image_extensions_checked_before_ssh(self):
        with mock.patch.object(cloud, "_info", new=mock.AsyncMock()) as info:
            for name, content in (("bad.png", b"not an image"), ("renamed.txt", png_fixture()), ("renamed.jpg", png_fixture())):
                path = self.home / name
                path.write_bytes(content)
                with self.subTest(name=name), self.assertRaises(ValueError):
                    cloud.cloud_scan(self.profile, paths=[path], allow_upload=True)
            info.assert_not_called()
        image = io.BytesIO(self.src.read_bytes())
        image.seek(8)
        self.assertEqual(cloud.validate_upload_image(image, ".PNG"), "png")
        self.assertEqual(image.tell(), 8)

    def test_sftp_reconnect(self):
        stage = self.home
        for name in ["a.png", "b.png", "c.png"]:
            (stage / name).write_bytes(png_fixture())
        size = len(png_fixture())
        conn = cloud.load_profile(self.profile)
        attempts = [subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)]
        with mock.patch.object(cloud.subprocess, "run", side_effect=attempts) as run, \
             mock.patch.object(cloud, "_job", return_value={"a.png": size, "b.png": 3}) as inventory:
            cloud._upload(conn, stage, "a"*32, ["a.png", "b.png", "c.png"])
        self.assertEqual(inventory.call_args.args[1], "inventory")
        second = run.call_args.kwargs["input"].decode()
        self.assertNotIn("a.png", second)
        self.assertIn("reput b.png", second)
        self.assertIn("put c.png", second)

    def test_map_only_uploaded_paths(self):
        root = "/tmp/example/images"
        mapping = {"01.png": self.src, "02.png": self.home/"second.png"}
        result = {"directory": root, "matches": [{"path_a": root+"/01.png", "path_b": root+"/02.png"}],
                  "errors": [{"path": root+"/01.png", "message": "bad "+root+"/01.png"}], "inputs": [root]}
        cloud._map_result(result, mapping, root)
        self.assertEqual(result["matches"][0]["path_a"], str(self.src))
        self.assertNotIn(root, json.dumps(result))
        result["matches"][0]["path_a"] = "/unknown"
        with self.assertRaisesRegex(ValueError, "outside"):
            cloud._map_result(result, mapping, root)

    def test_private_cache_and_cleanup_before_upload_or_after_cancel(self):
        job = {"config": "/tmp/example/config", "cache": "/tmp/example/cache", "directory": "/tmp/example/images"}
        info = {"weights_present": True, "weights_path": "/opt/weights", "device": "cpu"}
        for isolated, exc in [(False, ValueError), (True, KeyboardInterrupt)]:
            with self.subTest(isolated=isolated), \
                 mock.patch.object(cloud, "_info", new=mock.AsyncMock(return_value=info)), \
                 mock.patch.object(cloud, "_job", side_effect=[job, isolated, None]) as control, \
                 mock.patch.object(cloud, "_scan", new=mock.AsyncMock(return_value={"image_count": 0})) as scan, \
                 mock.patch.object(cloud, "_upload", side_effect=KeyboardInterrupt) as upload:
                with self.assertRaises(exc):
                    cloud.scan_upload(self.profile, [self.src], allow_upload=True)
                self.assertEqual(scan.await_args.args[1], ".")
                self.assertEqual([c.args[1] for c in control.call_args_list], ["create", "cache-ready", "remove"])
                self.assertEqual(upload.called, isolated)

    def test_successful_upload_maps_random_names_and_removes_job(self):
        other = self.home / "second.png"
        other.write_bytes(png_fixture())
        job = {"config": "/tmp/example/config", "cache": "/tmp/example/cache", "directory": "/tmp/example/images"}
        info = {"weights_present": True, "weights_path": "/opt/weights"}
        names = []

        def transfer(conn, stage, job_id, mapping, remote_directory):
            self.assertEqual(remote_directory, job["directory"])
            names.extend(mapping)
            self.assertEqual(set(mapping.values()), {self.src, other})
            self.assertTrue(all((stage/name).read_bytes() == png_fixture() for name in mapping))

        async def scan(conn, directory, options, config, isolated):
            if not names:
                return {"image_count": 0}
            return {"matches": [{"path_a": job["directory"] + "/" + names[0],
                                 "path_b": job["directory"] + "/" + names[1]}], "errors": []}

        with mock.patch.object(cloud, "_info", new=mock.AsyncMock(return_value=info)), \
             mock.patch.object(cloud, "_job", side_effect=[job, True, None]) as control, \
             mock.patch.object(cloud, "_scan", new=scan), \
             mock.patch.object(cloud, "_upload", side_effect=transfer):
            raw = cloud.scan_upload(self.profile, [other, self.src], allow_upload=True,
                                    file_stamps={str(p): cloud.signature(p.stat()) for p in [other, self.src]})
        self.assertEqual(raw["matches"][0], {"path_a": str(self.src), "path_b": str(other)})
        self.assertEqual([c.args[1] for c in control.call_args_list], ["create", "cache-ready", "remove"])

    def test_incompatible_weights_prevent_upload(self):
        job = {"config": "/tmp/example/config", "cache": "/tmp/example/cache", "directory": "/tmp/example/images"}
        info = {"weights_present": True, "weights_path": "/opt/weights"}
        with mock.patch.object(cloud, "_info", new=mock.AsyncMock(return_value=info)), \
             mock.patch.object(cloud, "_job", side_effect=[job, None]) as control, \
             mock.patch.object(cloud, "_scan", new=mock.AsyncMock(side_effect=ValueError("Unexpected remote descriptor"))), \
             mock.patch.object(cloud, "_upload") as upload:
            with self.assertRaisesRegex(ValueError, "Unexpected remote"):
                cloud.scan_upload(self.profile, [self.src], allow_upload=True)
            upload.assert_not_called()
            self.assertEqual([c.args[1] for c in control.call_args_list], ["create", "remove"])

    @unittest.skipUnless(os.name == "posix", "remote job service is POSIX")
    def test_expiry_only_removes_owned_private_jobs(self):
        with mock.patch.object(jobs, "BASE", self.home/"jobs"):
            jobs.private_base()
            old = jobs.target("1"*32)
            fresh = jobs.target("2"*32)
            foreign = jobs.target("3"*32)
            for job in [old, fresh, foreign]:
                job.mkdir(mode=0o700)
                (job/"owner").write_text(job.name if job != foreign else "another owner")
                (job/"owner").chmod(0o600)
                (job/"cache.sqlite3").write_bytes(b"cached test features")
            expired = time.time()-jobs.TTL-1
            for job in (old, foreign):
                os.utime(job/"owner", (expired, expired))
            self.assertEqual(jobs.reap(time.time()), 1)
            self.assertFalse(old.exists())
            self.assertTrue((fresh/"cache.sqlite3").exists())
            self.assertTrue((foreign/"cache.sqlite3").exists())
            with self.assertRaises(ValueError):
                jobs.target("../unexpected")

    def test_restricted_remote_mode_rejects_legacy_before_scan(self):
        scans = []
        @asynccontextmanager
        async def fake_session(*args):
            async def call(name, params=None):
                if name == "get_image_dedup_info":
                    return {"weights_present": True}
                scans.append(name)
                return {"weights_sha256": "unexpected", "dimension": 512}
            yield call
        with mock.patch.object(cloud, "_session", fake_session):
            with self.assertRaisesRegex(ValueError, "strict image roots"):
                cloud.scan_remote(self.profile, "gallery")
            self.assertEqual(scans, [])
            with self.assertRaisesRegex(ValueError, "Unexpected remote descriptor"):
                cloud.scan_remote(self.profile, "gallery", require_strict_remote=False)
            self.assertEqual(scans, ["find_similar_images"])

    def test_sanitized_upload_removes_metadata_and_orients_pixels(self):
        try:
            from PIL import Image, PngImagePlugin
        except ImportError:
            self.skipTest("Sanitized upload needs Pillow")
        source = self.home / "metadata.png"
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("private-note", "fixture-metadata")
        image = Image.new("RGB", (3, 2), "red")
        exif = Image.Exif()
        exif[274] = 6
        image.save(source, pnginfo=metadata, exif=exif)
        destination = self.home / "sanitized.png"
        cloud._snapshot(source, destination)
        with Image.open(destination) as clean:
            self.assertEqual(clean.size, (2, 3))
            self.assertEqual(clean.mode, "RGB")
            self.assertEqual(clean.info, {})
        self.assertNotIn(b"fixture-metadata", destination.read_bytes())
        self.assertIn(b"fixture-metadata", source.read_bytes())

    def test_original_upload_is_explicit_and_missing_pillow_never_falls_back(self):
        original = self.home / "original.png"
        with mock.patch.dict(sys.modules, {"PIL": None}):
            cloud._snapshot(self.src, original, upload_original=True)
            with self.assertRaisesRegex(RuntimeError, "requires Pillow"):
                cloud._snapshot(self.src, self.home / "should-not-exist.png")
        self.assertEqual(original.read_bytes(), self.src.read_bytes())
        self.assertFalse((self.home / "should-not-exist.png").exists())

    def test_sanitized_upload_rejects_multiframe_high_depth_and_truncated_files(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Sanitized upload needs Pillow")
        gif = self.home / "animated.gif"
        Image.new("RGB", (3, 3), "red").save(gif, save_all=True,
                    append_images=[Image.new("RGB", (3, 3), "green")])
        deep = self.home / "depth.png"
        Image.new("I;16", (3, 3)).save(deep)
        broken = self.home / "broken.png"
        broken.write_bytes(png_fixture()[:40])
        for i, path in enumerate((gif, deep, broken)):
            with self.subTest(name=path.name), self.assertRaises((OSError, ValueError, SyntaxError)):
                cloud._snapshot(path, self.home / (str(i) + "-out.png"))

    def test_remote_jobs_reject_symlink_roots_and_cleanup_foreign_paths(self):
        base = self.home / "jobs"
        foreign = self.home / ("f"*32)
        foreign.mkdir(mode=0o700)
        marker = foreign / "owner"
        marker.write_text(foreign.name)
        marker.chmod(0o600)
        with mock.patch.object(jobs, "BASE", base):
            jobs.private_base()
            self.assertFalse(jobs.remove_job(foreign))
            self.assertTrue(foreign.exists())
        if os.name == "posix":
            linked = self.home / "linked"
            linked.symlink_to(base)
            with mock.patch.object(jobs, "BASE", linked), self.assertRaisesRegex(ValueError, "symbolic"):
                jobs.private_base()


    def test_private_profile_failures_never_echo_paths_or_contents(self):
        marker = "fixture-private-location"
        missing = self.home / marker / "profile.json"
        for path, content in ((missing, None), (self.profile, '{"host": bad'),
                              (self.profile, b"\xff\xfe")):
            if content is not None:
                self.profile.write_bytes(content if isinstance(content, bytes) else content.encode())
            with self.subTest(kind=type(content).__name__), self.assertRaisesRegex(ValueError, "Cannot read private SSH profile") as ctx:
                cloud.load_profile(path)
            self.assertNotIn(marker, str(ctx.exception))
            self.assertNotIn(str(self.home), str(ctx.exception))
            self.assertNotIn("bad", str(ctx.exception))
        with mock.patch.object(cloud, "_load_profile", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            cloud.load_profile(self.profile)

    def test_remote_error_fields_are_rebuilt_without_server_details(self):
        private = "fixture-private-server-detail"
        remote = "/path/to/private-gallery"
        names = {"01.png": self.src}
        result = {"matches": [], "errors": [
            {"path": remote+"/01.png", "message": private, "operation": private,
             "error_type": private, "traceback": private},
            {"path": private, "message": private, "operation": "descriptor", "error_type": "ValueError"}]}
        cloud._map_result(result, names, remote)
        public_text = json.dumps(result)
        self.assertNotIn(private, public_text)
        self.assertNotIn(remote, public_text)
        self.assertEqual(result["errors"][0]["operation"], "remote")
        self.assertEqual(result["errors"][1]["operation"], "descriptor")
        self.assertEqual(result["errors"][0]["path"], str(self.src))
        self.assertEqual(result["errors"][1]["path"], "")

    def test_cli_remote_default_requires_strict_server_without_hidden_flag(self):
        import copynce_cli as cli
        with mock.patch.object(cloud, "cloud_scan", return_value=cli.empty_result()) as scan, \
             mock.patch.object(cli, "report_lines", return_value=[]):
            code = cli.main(["--backend", "cloud", "--profile", str(self.profile), "--remote-directory", "gallery"])
        self.assertEqual(code, 0)
        self.assertTrue(scan.call_args.kwargs["require_strict_remote"])


    def test_exact_job_cleanup_validates_id_and_uses_owned_job_helper(self):
        job_id = "d" * 32
        with mock.patch.object(cloud, "_job", return_value=None) as remove:
            self.assertTrue(cloud.cleanup_job(self.profile, job_id))
        self.assertEqual(remove.call_args.args[1:], ("remove", job_id))
        with mock.patch.object(cloud, "load_profile") as load, mock.patch.object(cloud, "_job") as remove:
            for invalid in (None, "../other", "a"*31, "a"*33, "g"*32, "A"*32):
                with self.subTest(kind=type(invalid).__name__), self.assertRaises(ValueError):
                    cloud.cleanup_job(self.profile, invalid)
            load.assert_not_called()
            remove.assert_not_called()
        with mock.patch.object(cloud, "_job", side_effect=RuntimeError("Owned job verification failed")):
            with self.assertRaises(RuntimeError):
                cloud.cleanup_job(self.profile, job_id)

    def test_interrupted_cleanup_reports_only_anonymous_recoverable_job_id(self):
        job_id = "c"*32
        job = {"config": "/path/to/config", "cache": "/path/to/cache", "directory": "/path/to/images"}
        info = {"weights_present": True, "weights_path": "/path/to/weights"}
        private = "fixture-sensitive-server-detail"
        with mock.patch.object(cloud.uuid, "uuid4", return_value=mock.Mock(hex=job_id)), \
             mock.patch.object(cloud, "_info", new=mock.AsyncMock(return_value=info)), \
             mock.patch.object(cloud, "_job", side_effect=[job, True, RuntimeError(private)]), \
             mock.patch.object(cloud, "_scan", new=mock.AsyncMock(return_value={"image_count": 0})), \
             mock.patch.object(cloud, "_upload", side_effect=ConnectionError(private)):
            with self.assertRaises(RuntimeError) as ctx:
                cloud.scan_upload(self.profile, [self.src], allow_upload=True)
        message = str(ctx.exception)
        self.assertIn("--cleanup-job " + job_id, message)
        self.assertNotIn(private, message)
        self.assertNotIn("/path/to", message)
        self.assertIsNone(ctx.exception.__cause__)
