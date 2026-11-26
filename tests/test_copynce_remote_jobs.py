"""Private upload jobs isolate primary files from shared read-only model resources."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import copynce_remote_jobs as jobs

@unittest.skipUnless(os.name=="posix","SSH job helper requires POSIX")
class RemoteJobTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve()
        patch=mock.patch.object(jobs,"BASE",self.root/"jobs")
        patch.start()
        self.addCleanup(patch.stop)
        patch=mock.patch.object(jobs.subprocess,"Popen")
        patch.start()
        self.addCleanup(patch.stop)
        self.weights=self.root/"weights"
        self.matcher=self.root/"matcher"
        self.background=self.root/"background"
        self.weights.write_bytes(b"descriptor fixture")
        self.matcher.write_bytes(b"matcher fixture")
        self.background.mkdir()
        (self.background/"manifest.json").write_text("{}")
        self.settings=dict(weights=str(self.weights),matcher_weights=str(self.matcher),sn_index=str(self.background))

    def test_enhanced_job_keeps_auxiliary_out_of_image_roots_and_cleanup(self):
        result=jobs.create_job("a"*32,self.settings,allow_auxiliary=True)
        config=json.loads(Path(result["config"]).read_text())
        self.assertEqual(config["image_roots"],[result["directory"]])
        self.assertEqual(config["sn_index"],str(self.background))
        self.assertTrue(jobs.remove_job(jobs.target("a"*32)))
        self.assertTrue(self.matcher.exists())
        self.assertTrue((self.background/"manifest.json").exists())
        self.assertFalse(Path(result["config"]).exists())

    def test_legacy_create_rejects_auxiliary_paths(self):
        with self.assertRaisesRegex(ValueError,"runtime settings"):
            jobs.create_job("a"*32,self.settings)

    def test_active_worker_prevents_expiry_or_explicit_removal(self):
        jobs.create_job("a"*32,self.settings,allow_auxiliary=True)
        job=jobs.target("a"*32)
        with jobs.active_job(job):
            self.assertEqual(jobs.reap(time.time()+jobs.TTL+1),0)
            self.assertFalse(jobs.remove_job(job))
            self.assertTrue((job/"images").is_dir())
        self.assertEqual(jobs.reap(time.time()+jobs.TTL+1),1)
        self.assertTrue(self.background.is_dir())

    def test_auxiliary_symlink_rejected_before_creating_job(self):
        link=self.root/"linked-background"
        link.symlink_to(self.background)
        with self.assertRaisesRegex(ValueError,"symbolic"):
            jobs.create_job("a"*32,dict(self.settings,sn_index=str(link)),allow_auxiliary=True)
        self.assertFalse(jobs.target("a"*32).exists())
