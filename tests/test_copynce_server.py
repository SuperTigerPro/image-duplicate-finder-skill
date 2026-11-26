"""Exercise root isolation, real stdio negotiation and worker cancellation locally."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import copynce_cloud as cloud
import copynce_server as server


class ServerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.images = self.home / "images"
        self.images.mkdir()
        self.weights = self.home / "weights"
        self.weights.write_bytes(b"fixture")
        self.cache = self.home / "cache.sqlite3"
        self.config = self.home / "config.json"
        self.write_config()

    def write_config(self, **changes):
        obj = dict(weights=str(self.weights), image_roots=[str(self.images)], cache=str(self.cache), device="cpu")
        obj.update(changes)
        self.config.write_text(json.dumps(obj))
        self.config.chmod(0o600)

    def test_info_is_strict_and_never_exposes_network_listener(self):
        cfg = server.load_settings(self.config)
        info = server.get_info(cfg)
        self.assertTrue(info["strict_image_roots"])
        self.assertEqual(info["transport"], "stdio")
        self.assertTrue(info["weights_present"])
        self.assertEqual(info["dimension"], 512)
        self.assertEqual(server.resolve_directory(cfg, "."), self.images)

    def test_directory_scope_and_symlink_components(self):
        cfg = server.load_settings(self.config)
        for value in ("..", "../images", str(self.home), str(self.weights)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                server.resolve_directory(cfg, value)
        if os.name == "posix":
            (self.images / "link").symlink_to(self.home)
            with self.assertRaisesRegex(ValueError, "Symbolic"):
                server.resolve_directory(cfg, "link")
            self.images.rename(self.home / "original")
            self.images.symlink_to(self.home / "original")
            with self.assertRaisesRegex(ValueError, "Symbolic"):
                server.resolve_directory(cfg, ".")

    def test_private_configuration_and_explicit_roots(self):
        for obj in ({"image_roots": []}, {"extra": True}, {"batch_size": True}, {"max_pixels": 0}):
            self.write_config(**obj)
            with self.subTest(obj=obj), self.assertRaises(ValueError):
                server.load_settings(self.config)
        self.write_config()
        if os.name == "posix":
            self.config.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "chmod"):
                server.load_settings(self.config)
        self.config.chmod(0o600)
        (self.home / ".git").mkdir()
        with self.assertRaisesRegex(ValueError, "Git worktrees"):
            server.load_settings(self.config)

    @unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL"), "scan needs image dependencies")
    def test_scan_freezes_image_selection_and_uses_shared_engine(self):
        import numpy as np
        from PIL import Image
        import copynce_engine as engine
        class Descriptor:
            dimension = 512
            device = "cpu"
            weights_id = server.WEIGHT_SHA256
            fallbacks = ()
            def encode(self, images):
                return np.ones((len(images), self.dimension), dtype=np.float32)
        for name in ("a.png", "b.png"):
            Image.new("RGB", (8, 8), "red").save(self.images / name)
        (self.images / ".hidden.png").write_bytes(b"not selected")
        if os.name == "posix":
            (self.images / "link.png").symlink_to(self.images / "a.png")
        with mock.patch.object(engine, "_load_descriptor", return_value=Descriptor()):
            result = server.scan_sync(server.load_settings(self.config), ".", tiles=False)
        self.assertEqual(result["image_count"], 2)
        self.assertEqual(result["pairs_returned"], 1)
        self.assertEqual(result["cache_misses"], 2)
        self.assertEqual(result["inputs"], [str(self.images)])
        self.assertEqual(result["skipped"]["hidden_entries"], 1)
        self.assertTrue(self.cache.exists())
        self.assertEqual(result["weights_sha256"], server.WEIGHT_SHA256)

    def test_cancel_only_terminates_this_worker(self):
        real_spawn = asyncio.create_subprocess_exec
        observed = []
        async def spawn(*args, **kwargs):
            proc = await real_spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
            observed.append(proc)
            return proc
        async def exercise():
            with mock.patch.object(server.asyncio, "create_subprocess_exec", side_effect=spawn):
                task = asyncio.create_task(server.run_scan(self.config, "."))
                for _ in range(100):
                    if server.WORKERS:
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(server.WORKERS)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertFalse(server.WORKERS)
            self.assertIsNotNone(observed[0].returncode)
        asyncio.run(exercise())

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "stdio integration needs optional mcp runtime")
    def test_real_mcp_stdio_client_handshake_info_and_scope_failure(self):
        launcher = Path(__file__).resolve().parents[1] / "start-mcp"
        profile = cloud.Profile("server.example.org", str(launcher), ())
        async def exercise():
            with mock.patch.object(cloud, "_ssh", side_effect=lambda conn, args: args):
                async with cloud._session(profile, str(self.config)) as call:
                    result = await call("get_image_dedup_info")
                    self.assertEqual(result["image_roots"], [str(self.images)])
                    self.assertTrue(result["strict_image_roots"])
                    with self.assertRaisesRegex(RuntimeError, "Remote find_similar_images failed"):
                        await call("find_similar_images", {"directory": "../outside"})
        with mock.patch.dict(os.environ, {"COPYNCE_PYTHON": sys.executable}):
            asyncio.run(exercise())
