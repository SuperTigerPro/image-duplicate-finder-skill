"""Provider-neutral adapter contract and local path authorization tests."""
import json
import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

from test_find_duplicates import Fixture, Image, ROOT

ADAPTER = ROOT / "scripts" / "tool_adapter.py"


class ToolAdapterTests(Fixture):
    def request(self, **overrides):
        value = {"paths": [str(self.images)], "mode": "exact",
                 "output_dir": str(self.root / "reports" / "new-run"),
                 "recursive": True, "include_hidden": False}
        value.update(overrides)
        return value

    def invoke_adapter(self, request=None, raw=None):
        if raw is None:
            raw = json.dumps(request or self.request(), ensure_ascii=False).encode("utf-8")
        result = subprocess.run(
            [sys.executable, str(ADAPTER), "--allow-root", str(self.images),
             "--report-root", str(self.root / "reports")],
            input=raw, capture_output=True, timeout=30,
            env=dict(os.environ, PYTHONIOENCODING="ascii"))
        return result, json.loads(result.stdout.decode("utf-8"))

    def test_function_schema_matches_openai_compatible_chat_tools(self):
        tools = json.loads((ROOT / "adapters/chat-completions-tools.json").read_text(encoding="utf-8"))
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["type"], "function")
        function = tools[0]["function"]
        self.assertEqual(function["name"], "find_image_duplicates")
        parameters = function["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(set(parameters["required"]), set(self.request()))
        self.assertEqual(set(parameters["properties"]), set(self.request()) | {"similarity_threshold"})
        self.assertEqual(parameters["properties"]["similarity_threshold"]["default"], 90)
        self.assertEqual(parameters["properties"]["mode"]["enum"], ["exact", "pixels", "similar"])

    def test_json_only_exact_roundtrip_and_reports(self):
        self.write("a.png")
        self.write("b.png")
        result, response = self.invoke_adapter()
        self.assertEqual(result.returncode, 0, response)
        self.assertTrue(response["ok"])
        self.assertEqual(response["exit_code"], 0)
        self.assertEqual(response["report"]["summary"]["duplicate_groups"], 1)
        self.assertTrue((self.root / "reports/new-run/duplicates.csv").is_file())
        self.assertEqual(result.stderr, b"")

    @unittest.skipUnless(Image is not None, "Pillow is optional")
    def test_pixel_mode_roundtrip(self):
        self.write("a.png")
        self.write("b.png")
        result, response = self.invoke_adapter(self.request(mode="pixels"))
        self.assertEqual(result.returncode, 0, response)
        self.assertEqual(response["report"]["mode"], "pixels")
        self.assertEqual(response["report"]["summary"]["duplicate_images"], 2)

    @unittest.skipUnless(Image is not None and importlib.util.find_spec("numpy") is not None,
                         "Install requirements-similar.txt")
    def test_similarity_default_threshold_is_90(self):
        self.write("a.png")
        self.write("b.png")
        result, response = self.invoke_adapter(self.request(mode="similar"))
        self.assertEqual(result.returncode, 0, response)
        self.assertEqual(response["report"]["schema_version"], 2)
        self.assertEqual(response["report"]["similarity_threshold"], 90)
        self.assertEqual(response["report"]["summary"]["similar_pairs"], 1)

    @unittest.skipUnless(Image is not None and importlib.util.find_spec("numpy") is not None,
                         "Install requirements-similar.txt")
    def test_similarity_single_threshold_roundtrip(self):
        self.write("a.png")
        self.write("b.png")
        result, response = self.invoke_adapter(self.request(mode="similar", similarity_threshold=100))
        self.assertEqual(result.returncode, 0, response)
        self.assertEqual(response["report"]["similarity_threshold"], 100)
        self.assertTrue(response["report"]["similar_pairs"][0]["pixel_identical"])
        self.assertTrue((self.root / "reports/new-run/similarity-preview.png").is_file())

    def test_invalid_similarity_threshold_types_and_range_rejected(self):
        for threshold in (True, None, -1, 101, "90", 90.5):
            with self.subTest(threshold=threshold):
                result, response = self.invoke_adapter(self.request(mode="similar", similarity_threshold=threshold))
                self.assertEqual(result.returncode, 2)
                self.assertIsNone(response["report"])
                self.assertIn("阈值", response["stderr"])

    def test_similarity_threshold_cannot_be_silently_ignored_in_exact(self):
        result, response = self.invoke_adapter(self.request(similarity_threshold=90))
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(response["report"])

    def test_input_outside_host_allowed_roots_rejected(self):
        outside = self.root / "outside.png"
        outside.write_bytes(b"do not read")
        result, response = self.invoke_adapter(self.request(paths=[str(outside)]))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(response["ok"])
        self.assertIsNone(response["report"])
        self.assertFalse((self.root / "reports").exists())
        self.assertEqual(outside.read_bytes(), b"do not read")

    def test_report_outside_host_root_rejected(self):
        result, response = self.invoke_adapter(self.request(output_dir=str(self.root / "elsewhere")))
        self.assertEqual(result.returncode, 2)
        self.assertIn("报告根目录", response["stderr"])
        self.assertFalse((self.root / "elsewhere").exists())

    def test_report_root_itself_cannot_be_output(self):
        result, response = self.invoke_adapter(self.request(output_dir=str(self.root / "reports")))
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(response["report"])

    def test_existing_report_is_not_returned_as_new_result(self):
        result, response = self.invoke_adapter()
        self.assertEqual(result.returncode, 0, response)
        original = (self.root / "reports/new-run/duplicates.json").read_bytes()
        result, response = self.invoke_adapter()
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(response["report"])
        self.assertEqual((self.root / "reports/new-run/duplicates.json").read_bytes(), original)

    def test_wrong_types_and_unrecognized_fields_rejected(self):
        for changes in ({"paths": []}, {"recursive": 1}, {"include_hidden": "false"},
                        {"mode": "fuzzy"}, {"output_dir": ""}, {"command": "delete"}):
            with self.subTest(changes=changes):
                result, response = self.invoke_adapter(self.request(**changes))
                self.assertEqual(result.returncode, 2)
                self.assertIsNone(response["report"])

    @unittest.skipUnless(Image is not None, "Pillow is optional")
    def test_partial_errors_preserve_structured_report(self):
        self.write("a.png")
        self.write("b.png")
        self.write("broken.jpg", b"invalid")
        result, response = self.invoke_adapter(self.request(mode="pixels"))
        self.assertEqual(result.returncode, 1)
        self.assertFalse(response["ok"])
        self.assertFalse(response["report"]["complete"])
        self.assertEqual(response["report"]["summary"]["duplicate_groups"], 1)

    def test_invalid_and_oversized_json_rejected(self):
        for raw in (b"{", b"null", b" " * (1024 * 1024 + 1)):
            with self.subTest(size=len(raw)):
                result, response = self.invoke_adapter(raw=raw)
                self.assertEqual(result.returncode, 2)
                self.assertIsNone(response["report"])


if __name__ == "__main__":
    unittest.main()
