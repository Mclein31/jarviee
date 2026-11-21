"""The user overlay (~/.jarviee/config.json) deep-merged over the repo's public config.json."""
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (puts the repo on sys.path)

from hub import config


def _env(**kw):
    """Patch the environment: JARVIEE_USER_CONFIG and a throwaway data dir for db/logs."""
    return mock.patch.dict(os.environ, kw)


class OverlayTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)
        self.addCleanup(self._t.cleanup)
        self.overlay = self.tmp / "user" / "config.json"
        self.base = self.tmp / "base.json"
        base = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        base["db_path"] = str(self.tmp / "hub.db")
        base["logs_dir"] = str(self.tmp / "logs")
        base["settings_path"] = str(self.tmp / "settings.json")
        self.base.write_text(json.dumps(base), encoding="utf-8")
        p = _env(JARVIEE_USER_CONFIG=str(self.overlay), JARVIEE_CONFIG=str(self.base))
        p.start()
        self.addCleanup(p.stop)
        mock.patch.object(config, "CONFIG_PATH", self.base).start()
        self.addCleanup(mock.patch.stopall)

    def _write(self, data):
        self.overlay.parent.mkdir(parents=True, exist_ok=True)
        self.overlay.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def test_public_defaults_are_generic(self):
        raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))   # the file itself, no saved settings
        self.assertEqual(raw["obsidian"]["workspace_root"], "~/Projects")
        self.assertEqual(raw["pipeline"]["skills"]["build"], "")
        cfg = config.load_config(self.base, user=False)   # same values, temp data dir and settings
        self.assertEqual(cfg["obsidian"]["workspace_root"], str(Path("~/Projects").expanduser()))
        self.assertFalse(cfg["omniroute"]["enabled"])
        self.assertFalse(cfg["voice"]["enabled"])
        self.assertEqual(cfg["terminal"]["display"], "main")
        self.assertEqual(cfg["dashboard_window"]["display"], "main")
        self.assertEqual(cfg["port"], 7777)

    def test_default_load_merges_overlay_deeply(self):
        self._write({"voice": {"enabled": True}, "terminal": {"font_size": 9},
                     "lanes": {"agy": 5}, "obsidian": {"context_files": ["A.md"]}})
        cfg = config.load_config()
        self.assertTrue(cfg["voice"]["enabled"])
        self.assertIn("engine", cfg["voice"])               # sibling keys survive a dict merge
        self.assertEqual(cfg["terminal"]["font_size"], 9)
        self.assertEqual(cfg["terminal"]["mode"], "iterm2")
        self.assertEqual(cfg["lanes"], {"agy": 5, "claude": 1})
        self.assertEqual(cfg["obsidian"]["context_files"], ["A.md"])   # lists replace

    def test_explicit_path_skips_overlay(self):
        self._write({"voice": {"enabled": True}})
        self.assertFalse(config.load_config(self.base)["voice"]["enabled"])
        self.assertTrue(config.load_config(self.base, user=True)["voice"]["enabled"])
        self.assertTrue(config.load_config()["voice"]["enabled"])

    def test_env_var_empty_disables_overlay(self):
        self._write({"voice": {"enabled": True}})
        with _env(JARVIEE_USER_CONFIG=""):
            self.assertIsNone(config.user_config_path())
            self.assertFalse(config.load_config()["voice"]["enabled"])
            self.assertEqual(config.read_user_config(), {})
            with self.assertRaises(RuntimeError):
                config.write_user_config({"a": 1})

    def test_legacy_env_prefix(self):
        with mock.patch.dict(os.environ, {"AGENT_HUB_USER_CONFIG": str(self.tmp / "old.json")}):
            os.environ.pop("JARVIEE_USER_CONFIG")
            self.assertEqual(config.user_config_path(), self.tmp / "old.json")

    def test_default_path_is_in_data_dir(self):
        os.environ.pop("JARVIEE_USER_CONFIG")
        os.environ.pop("AGENT_HUB_USER_CONFIG", None)
        self.assertEqual(config.user_config_path().name, "config.json")
        self.assertEqual(config.user_config_path().parent, config.brand.home())

    def test_bad_overlay_warns_once_and_is_ignored(self):
        for text in ("{not json", "[1, 2]"):
            self._write(text)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                cfg = config.load_config()
            self.assertEqual(len(err.getvalue().strip().splitlines()), 1, err.getvalue())
            self.assertFalse(cfg["voice"]["enabled"])

    def test_missing_overlay_is_silent(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            config.load_config()
        self.assertEqual(err.getvalue(), "")

    def test_write_merges_and_is_private(self):
        config.write_user_config({"voice": {"enabled": True}, "terminal": {"font_size": 9}})
        out = config.write_user_config({"voice": {"voice": "Alex"}, "port": 8000})
        self.assertEqual(out, {"voice": {"enabled": True, "voice": "Alex"}, "terminal": {"font_size": 9}, "port": 8000})
        self.assertEqual(json.loads(self.overlay.read_text(encoding="utf-8")), out)
        self.assertEqual(stat.S_IMODE(self.overlay.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.overlay.parent.iterdir()], ["config.json"])   # no temp file left

    def test_write_is_atomic(self):
        config.write_user_config({"a": 1})
        with mock.patch("hub.config.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                config.write_user_config({"a": 2})
        self.assertEqual(json.loads(self.overlay.read_text(encoding="utf-8")), {"a": 1})   # untouched
        self.assertEqual([p.name for p in self.overlay.parent.iterdir()], ["config.json"])

    def test_write_over_broken_overlay_replaces_it(self):
        self._write("{oops")
        with contextlib.redirect_stderr(io.StringIO()):
            out = config.write_user_config({"x": {"y": 1}})
        self.assertEqual(out, {"x": {"y": 1}})

    def test_deep_merge_does_not_alias_updates(self):
        upd = {"a": {"b": [1]}}
        merged = config.deep_merge({}, upd)
        merged["a"]["b"].append(2)
        self.assertEqual(upd, {"a": {"b": [1]}})


if __name__ == "__main__":
    unittest.main()
