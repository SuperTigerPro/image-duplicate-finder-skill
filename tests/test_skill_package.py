"""Check the Skill package, not only the image scanning implementation."""
import re
import unittest
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ("SKILL.md", "README.md", "TESTING.md", "references/report-format.md")
REQUIRED = DOCUMENTS + (
    "scripts/find_duplicates.py", "tests/test_find_duplicates.py",
    "tests/test_skill_package.py", "requirements-pixels.txt", "LICENSE",
    ".gitignore", ".gitattributes", ".github/workflows/tests.yml",
)


class SkillPackageTests(unittest.TestCase):
    def test_required_resources_are_utf8_text_without_null_bytes(self):
        for name in REQUIRED:
            with self.subTest(name=name):
                path = ROOT / name
                self.assertTrue(path.is_file(), name)
                content = path.read_text(encoding="utf-8")
                self.assertNotIn("\0", content)
                self.assertTrue(content.endswith("\n"), name)

    def test_skill_frontmatter_has_standard_name_and_description(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        front = text.split("---\n", 2)[1]
        values = dict(line.split(": ", 1) for line in front.splitlines()
                      if ": " in line and not line.startswith(" "))
        self.assertEqual(values["name"], "image-duplicate-finder-skill")
        self.assertRegex(values["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertLessEqual(len(values["name"]), 64)
        self.assertGreater(len(values["description"]), 10)
        self.assertLessEqual(len(values["description"]), 1024)
        self.assertEqual(values["license"], "MIT")
        self.assertLessEqual(len(values["compatibility"]), 500)

    def test_local_markdown_links_point_to_packaged_resources(self):
        for name in DOCUMENTS:
            source = ROOT / name
            text = source.read_text(encoding="utf-8")
            for target in re.findall(r"\]\(([^)]+)\)", text):
                if target.startswith(("https://", "http://", "mailto:", "#")):
                    continue
                target = unquote(target.split("#", 1)[0].strip("<>"))
                with self.subTest(document=name, link=target):
                    self.assertTrue((source.parent / target).is_file(), f"{name}: {target}")


if __name__ == "__main__":
    unittest.main()
