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
from types import SimpleNamespace

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


    def test_enhanced_info_verifies_models_and_hides_auxiliary_paths(self):
        matcher=self.home/"matcher"
        matcher.write_bytes(b"fixture")
        background=self.home/"background-index"
        background.mkdir()
        self.write_config(matcher_weights=str(matcher),sn_index=str(background))
        manifest=dict(fingerprint="a"*64,schema_version=1,dimension=512,images=5,pipeline=cloud.SN_GLOBAL_PIPELINE)
        modules={"copynce_matcher":SimpleNamespace(OFFICIAL_SHA256=cloud.MATCHER_WEIGHTS_SHA256),
                 "copynce_sn":SimpleNamespace(GLOBAL_PIPELINE=cloud.SN_GLOBAL_PIPELINE,inspect_index=lambda *a,**kw:manifest,load_index=lambda *a,**kw:SimpleNamespace(manifest=manifest))}
        with mock.patch.dict(sys.modules,modules),mock.patch.object(server,"_hash_matches",return_value=True):
            info=server.get_info(server.load_settings(self.config))
        self.assertEqual(info["supported_profiles"],["fast","enhanced","deep"])
        self.assertEqual(info["enhancement"]["sn_index_fingerprint"],"a"*64)
        self.assertNotIn(str(background),json.dumps(info))
        self.assertNotIn(str(matcher),json.dumps(info))
        self.assertEqual(info["image_roots"],[str(self.images)])

    def test_enhanced_profile_fails_without_resources_and_never_downgrades(self):
        settings=server.load_settings(self.config)
        with self.assertRaisesRegex(ValueError,"verified Matcher"):
            server.scan_sync(settings,".",profile="enhanced")
        with self.assertRaises(ValueError):
            server.require_profile(settings,"unknown")

    def test_upload_job_resources_come_only_from_server_settings(self):
        import copynce_remote_jobs as jobs
        settings=server.Settings(self.weights,(self.images,),self.cache,"cpu",8,40000000,
                                 self.home/"matcher",self.home/"background-index")
        with mock.patch.object(server,"require_profile") as validate, \
             mock.patch.object(jobs,"create_job",return_value={"directory":"/path/to/job/images"}) as create:
            server.prepare_upload_job(settings,"a"*32,"enhanced","b"*64)
        validate.assert_called_once_with(settings,"enhanced","b"*64)
        cfg=create.call_args.args[1]
        self.assertEqual(cfg["sn_index"],str(settings.sn_index))
        self.assertEqual(cfg["matcher_weights"],str(settings.matcher_weights))
        self.assertNotIn("image_roots",cfg)
        self.assertTrue(create.call_args.kwargs["allow_auxiliary"])
        with self.assertRaises(TypeError):
            server.prepare_upload_job(settings,"a"*32,"enhanced","b"*64,sn_index="/path/to/unapproved")


    def test_stage_budgets_and_indexed_mode_rejected_before_resource_loading(self):
        settings=server.load_settings(self.config)
        with mock.patch.object(server,"require_profile") as resources:
            for opts in ({"top_k":20001},{"candidate_k":257},{"matcher_budget":100001},
                         {"local_view_pairs":9},{"sn_extra_budget":10001},{"mode":"indexed"}):
                with self.subTest(opts=opts),self.assertRaises(ValueError):
                    server.scan_sync(settings,".",profile="enhanced",**opts)
            resources.assert_not_called()

    def test_new_sn_fingerprint_requires_full_arrays_validation_before_ready(self):
        settings=server.Settings(self.weights,(self.images,),self.cache,matcher_weights=self.home/"matcher",sn_index=self.home/"sn")
        manifest=dict(fingerprint="f"*64,schema_version=1,dimension=512,images=5,pipeline=cloud.SN_GLOBAL_PIPELINE)
        loader=mock.Mock(side_effect=ValueError("invalid array data"))
        module=SimpleNamespace(GLOBAL_PIPELINE=cloud.SN_GLOBAL_PIPELINE,inspect_index=lambda *a,**kw:manifest,load_index=loader)
        with mock.patch.dict(sys.modules,{"copynce_sn":module,"copynce_matcher":SimpleNamespace(OFFICIAL_SHA256=cloud.MATCHER_WEIGHTS_SHA256)}), \
             mock.patch.object(server,"_hash_matches",return_value=True),mock.patch.object(server,"VALIDATED_SN",set()):
            info=server.get_info(settings)
        loader.assert_called_once()
        self.assertFalse(info["enhancement"]["ready"])
        self.assertEqual(info["supported_profiles"],["fast"])


    def test_deep_whole_only_rejected_before_runtime_resources(self):
        with mock.patch.object(server,"require_profile") as resources,self.assertRaisesRegex(ValueError,"local comparison"):
            server.scan_sync(server.load_settings(self.config),".",profile="deep",tiles=False)
        resources.assert_not_called()
