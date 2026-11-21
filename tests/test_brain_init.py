import tempfile
import unittest
from pathlib import Path

from tests import helpers  # noqa: F401
from hub import brain_init


class BrainInit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.ws = self.tmp / "ws"
        (self.ws / "proj").mkdir(parents=True)
        self.cfg = {"obsidian": {"workspace_root": str(self.ws), "project_brain_dir": ".brain"}}

    def test_creates_all_files(self):
        ok, created, skipped, _ = brain_init.init(self.ws / "proj", self.cfg)
        self.assertTrue(ok)
        self.assertEqual(sorted(created), sorted(brain_init.FILES))
        text = (self.ws / "proj" / ".brain" / "TODO.md").read_text()
        self.assertIn("⬜", text)
        self.assertIn("# proj — TODO", text)
        self.assertNotIn("{{", text)

    def test_never_overwrites(self):
        b = self.ws / "proj" / ".brain"
        b.mkdir()
        (b / "NOTES.md").write_text("mine")
        ok, created, skipped, _ = brain_init.init(self.ws / "proj", self.cfg)
        self.assertEqual((b / "NOTES.md").read_text(), "mine")
        self.assertIn("NOTES.md", skipped)
        self.assertNotIn("NOTES.md", created)

    def test_outside_workspace_refused_unless_force(self):
        other = self.tmp / "other"
        other.mkdir()
        ok, *_ = brain_init.init(other, self.cfg)
        self.assertFalse(ok)
        self.assertFalse((other / ".brain").exists())
        ok, created, *_ = brain_init.init(other, self.cfg, force=True)
        self.assertTrue(ok and created)

    def test_missing_folder(self):
        ok, *_ = brain_init.init(self.ws / "nope", self.cfg)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
