import base64
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import copynce_cli as cli
import copynce_report as reports

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4"
                       "nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")


def sample(paths):
    pair = {"path_a": str(paths[0]), "path_b": str(paths[1]), "score": .9,
            "size_a": [40,30], "size_b": [40,30], "box_a": [0,0,40,30], "box_b": [4,8,20,22]}
    return dict(cli.empty_result(), matches=[pair], image_count=2, candidate_file_count=2,
                view_count=44, pairs_possible=1, pairs_assessed=1, pairs_returned=1, pairs_above_threshold=1)


class ModelCLITests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        for redirect in (contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr)):
            redirect.__enter__()
            self.addCleanup(redirect.__exit__, None, None, None)

    def file(self, name):
        p = self.root/name
        p.write_bytes(PNG)
        return p

    def test_conflicting_sources_and_unsafe_options(self):
        cases = [["a", "--allow-upload"], ["a", "--threshold", "nan"], ["a", "--top-k", "-2"],
                 ["a", "--require-strict-remote"], ["--backend", "cloud", "--profile", "p", "a"],
                 ["--backend", "cloud", "--profile", "p", "a", "--remote-directory", "r"],
                 ["--backend", "cloud", "--profile", "p", "a", "--allow-upload", "--extensions", "txt"],
                 ["--backend", "cloud", "--profile", "p", "a", "--plan-upload", "--extensions", "*"]]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(SystemExit):
                cli.parse(case)

    def test_empty_directory_and_output_collision(self):
        out = self.root/"output"
        with mock.patch.object(cli, "local_scan", side_effect=AssertionError):
            self.assertEqual(cli.main([str(self.root), "--output-dir", str(out)]), 0)
        r = json.loads((out/"duplicates.json").read_text())
        self.assertTrue(r["all_matches_reported"])
        self.assertEqual((r["schema_version"], r["matches"]), (3, []))
        with self.assertRaises(SystemExit):
            cli.main([str(self.root), "--output-dir", str(out)])

    def test_plan_upload_verifies_contents_offline(self):
        valid = self.file("included.png")
        self.file(".hidden.png")
        self.file("not-selected.txt")
        bad = self.root/"fake.png"
        bad.write_bytes(b"plain text")
        with mock.patch("copynce_cloud.load_profile") as load, mock.patch("copynce_cloud.cloud_scan") as cloud:
            load.return_value.host = "example.invalid"
            code = cli.main(["--backend","cloud","--profile","p","--plan-upload",str(self.root)])
            cloud.assert_not_called()
        self.assertEqual(code, 1)
        r = json.loads(self.stdout.getvalue())
        self.assertEqual((r["files"], r["bytes"], r["destination"]), (1, valid.stat().st_size, "selected-private-server"))
        self.assertNotIn("example.invalid", self.stdout.getvalue())
        self.assertEqual(r["transfer_format"], "metadata-free-rgb-png")
        self.assertFalse(r["uploaded"])
        self.assertEqual(r["errors"][0]["path"], str(bad))

    def test_coverage_fields_and_window_validation(self):
        raw = sample(["/demo/a.png","/demo/b.png"])
        raw.update(exhaustive=False, truncated=True, pairs_possible=3, pairs_assessed=2, pairs_above_threshold=2)
        r = reports.build_report(raw, "local", [], {})
        self.assertTrue(r["complete"])
        self.assertFalse(r["all_matches_reported"])
        self.assertEqual(r["matches"][0]["match_type"], "whole-region")
        raw.update(error_count=1, errors=[{"message":"bad"}])
        self.assertFalse(reports.build_report(raw, "local", [], {})["complete"])
        raw["matches"][0]["box_a"] = [-2,0,40,30]
        with self.assertRaises(ValueError):
            reports.build_report(raw, "local", [], {})

    def test_remote_directory_never_opens_local_file(self):
        paths = [self.file("a.png"), self.file("b.png")]
        out = self.root/"out"
        with mock.patch("copynce_cloud.cloud_scan", return_value=sample(paths)) as scan, mock.patch.object(
                reports, "checked_stream", side_effect=AssertionError):
            code = cli.main(["--backend","cloud","--profile","p","--remote-directory","collection",
                             "--require-strict-remote","--output-dir",str(out)])
            self.assertTrue(scan.call_args.kwargs["require_strict_remote"])
        self.assertEqual(code, 0)
        r = json.loads((out/"duplicates.json").read_text())
        self.assertEqual(r["inputs"], ["collection"])
        self.assertIsNone(r["preview"])

    def test_returned_paths_must_be_selected(self):
        a, b = self.file("a.png"), self.file("b.png")
        with mock.patch("copynce_cloud.cloud_scan", return_value=sample([a, "/other"])) as scan:
            self.assertEqual(cli.main(["--backend","cloud","--profile","p","--allow-upload",str(a),str(b)]), 2)
            self.assertEqual(set(scan.call_args.kwargs["file_stamps"]), {str(a), str(b)})
            self.assertFalse(scan.call_args.kwargs["upload_original"])

    def test_unified_entry_dispatches_without_loading_optional_model(self):
        import find_duplicates
        with mock.patch.object(cli, "main", return_value=17) as run:
            self.assertEqual(find_duplicates.main(["--mode=copynce", "example.png", "--offline"]), 17)
        self.assertEqual(run.call_args.args[0], ["example.png", "--offline"])

    def test_abbreviated_mode_cannot_bypass_model_dispatch(self):
        import find_duplicates
        with mock.patch.object(cli, "main", side_effect=AssertionError("must reject ambiguity")):
            with self.assertRaises(SystemExit):
                find_duplicates.main(["--mo", "copynce", str(self.root)])

    def test_inconsistent_remote_error_count_cannot_claim_complete(self):
        raw = cli.empty_result()
        raw["errors"] = [{"operation": "descriptor", "message": "synthetic failure"}]
        with self.assertRaisesRegex(ValueError, "错误计数"):
            reports.build_report(raw, "cloud", [], {"threshold": .75})

    def test_model_reports_are_rejected_inside_any_git_worktree(self):
        (self.root / ".git").mkdir()
        with self.assertRaises(SystemExit):
            cli.parse([str(self.root), "--output-dir", str(self.root / "private-report")])

    def test_exact_job_cleanup_has_no_scan_side_effects(self):
        job_id = "a" * 32
        with mock.patch("copynce_cloud.cleanup_job", return_value=True) as remove, mock.patch.object(
                cli, "select_inputs", side_effect=AssertionError("cleanup must not scan")):
            self.assertEqual(cli.main(["--backend", "cloud", "--profile", "private-profile",
                                       "--cleanup-job", job_id]), 0)
        remove.assert_called_once_with(Path("private-profile"), job_id)

    def test_summary_only_omits_paths_but_private_report_keeps_them(self):
        a, b = self.file("a.png"), self.file("b.png")
        out = self.root / "private-report"
        with mock.patch.object(cli, "local_scan", return_value=sample([a, b])):
            self.assertEqual(cli.main([str(a), str(b), "--summary-only", "--output-dir", str(out)]), 0)
        self.assertNotIn(str(a), self.stdout.getvalue())
        self.assertEqual(json.loads((out / "duplicates.json").read_text())["matches"][0]["path_a"], str(a))

    def test_checked_region_preview(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow unavailable")
        a, b = self.root/"a.png", self.root/"b.png"
        for p in (a,b):
            Image.new("RGB", (40,30), "white").save(p)
        out = self.root/"out"
        with mock.patch.object(cli, "local_scan", return_value=sample([a,b])):
            self.assertEqual(cli.main([str(self.root),"--output-dir",str(out)]), 0)
        r = json.loads((out/"duplicates.json").read_text())
        self.assertEqual(r["preview"], {"file":"similarity-preview.png","pairs_shown":1})
        with Image.open(out/"similarity-preview.png") as image:
            x, y = 280+(280-40)//2, 20+(200-30)//2
            self.assertEqual(image.getpixel((x+4,y+8)), (235,65,40))
            self.assertEqual(image.getpixel((x+2,y+6)), (255,255,255))

    def test_stdlib_client_gets_local_html_overlay(self):
        paths = [self.file("a.png"), self.file("b.png")]
        out = self.root/"report"
        with mock.patch.dict(sys.modules, {"PIL": None}), mock.patch("copynce_cloud.cloud_scan",
                                                                     return_value=sample(paths)):
            code = cli.main(["--backend","cloud","--profile","p","--allow-upload","--output-dir",str(out),
                             *map(str, paths)])
        self.assertEqual(code, 0)
        r = json.loads((out/"duplicates.json").read_text())
        self.assertEqual(r["preview"], {"file":"similarity-preview.html","pairs_shown":1})
        page = (out/"similarity-preview.html").read_text()
        self.assertIn(paths[1].as_uri(), page)
        self.assertIn('x="4" y="8"', page)
        self.assertIn("Content-Security-Policy", page)

    def test_explicit_file_under_symlink_rejected(self):
        target = self.root/"sub"
        target.mkdir()
        p = target/"file.png"
        p.write_bytes(PNG)
        link = self.root/"alias"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (NotImplementedError, OSError):
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(ValueError):
            cli.select_inputs(cli.parse([str(link/p.name)]))

    def test_custom_local_extensions_and_mixed_roots(self):
        f = self.file("foo.ext")
        self.file(".hidden.png")
        selection = cli.select_inputs(cli.parse([str(self.root),str(f),"--extensions","*"]))
        self.assertEqual([i.path for i in selection.files], [f])

    def test_local_subprocess_gets_original_file_stamps(self):
        f = self.file("selected.png")
        args = cli.parse([str(f)])
        records = {str(i.path): i for i in cli.select_inputs(args).files}
        with mock.patch("copynce_setup.prepare", return_value=(sys.executable, f)), mock.patch(
                "copynce_setup.user_cache", return_value=self.root), mock.patch("subprocess.run") as proc:
            proc.return_value.returncode = 0
            proc.return_value.stdout = "{}"
            cli.local_scan(records, args, {})
        req = json.loads(proc.call_args.kwargs["input"])
        self.assertEqual(req["file_stamps"][str(f)], list(records[str(f)].stamp))
