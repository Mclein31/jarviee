import tempfile
import unittest
from pathlib import Path

from hub import templates


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.cfg = {"templates_path": str(Path(self.d.name, "t.json"))}

    def tearDown(self):
        self.d.cleanup()

    def test_save_list_replace_delete(self):
        self.assertEqual(templates.list_all(self.cfg), [])
        templates.save(self.cfg, "Fix bug", "Fix it in {project}")
        templates.save(self.cfg, "audit", "Audit {file}")
        templates.save(self.cfg, "fix  BUG", "new body")   # same name, replaced
        self.assertEqual([t["name"] for t in templates.list_all(self.cfg)], ["audit", "fix BUG"])
        templates.delete(self.cfg, "AUDIT")
        self.assertEqual(len(templates.list_all(self.cfg)), 1)
        with self.assertRaises(KeyError):
            templates.delete(self.cfg, "nope")

    def test_validation(self):
        for n, b in (("", "x"), ("x", "  "), ("a" * 100, "x"), ("x", "b" * 9000)):
            with self.assertRaises(ValueError):
                templates.save(self.cfg, n, b)

    def test_corrupt_file_reads_empty(self):
        Path(self.cfg["templates_path"]).write_text("{nope")
        self.assertEqual(templates.list_all(self.cfg), [])

    def test_fill(self):
        self.assertEqual(templates.fill("{project}:{file}", "app"), "app:{file}")
        self.assertEqual(templates.fill("{project}", ""), "{project}")


if __name__ == "__main__":
    unittest.main()
