"""Speech to text through Handy (hub/stt.py): engine choice, status for the dashboard, and reading Handy's output."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT  # noqa: F401  (puts the repo on sys.path)
from hub import stt


class HandyEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.bin = d / "handy"
        self.bin.write_text("#!/bin/sh\nexit 0\n")
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IEXEC)
        self.data = d / "com.pais.handy"
        (self.data / "models").mkdir(parents=True)
        self.patches = [mock.patch.dict(os.environ, {"JARVIEE_HANDY_BIN": str(self.bin)}),
                        mock.patch.object(stt, "HANDY_DATA", self.data),
                        mock.patch.object(stt.shutil, "which", lambda name: "/usr/bin/" + name if name == "ffmpeg" else None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def settings(self, model="", binding="ctrl+option"):
        (self.data / "settings_store.json").write_text(json.dumps({"settings": {
            "selected_model": model, "bindings": {"transcribe": {"current_binding": binding}}}}))

    def test_handy_without_a_model_is_not_used_yet_and_says_what_to_do(self):
        self.settings(model="")
        st = stt.status()
        self.assertEqual((st["engine"], st["handy_installed"], st["handy_shortcut"]), ("", True, "ctrl+option"))
        self.assertIn("finish its setup", st["hint"])

    def test_handy_with_a_model_is_the_engine(self):
        self.settings(model="parakeet-tdt-0.6b-v3")
        (self.data / "models" / "parakeet").mkdir()
        self.assertEqual(stt.engine(), "handy")
        self.assertTrue(stt.status()["local"])

    def test_a_model_in_the_hugging_face_cache_counts(self):
        sel = "handy-computer/whisper-large-v3-gguf/whisper-large-v3-Q8_0.gguf"
        self.settings(model=sel)
        hub_dir = Path(self.tmp.name) / "hf"
        with mock.patch.object(stt, "HF_HUB", hub_dir):
            self.assertEqual(stt.engine(), "")                       # picked but not downloaded yet
            snap = hub_dir / "models--handy-computer--whisper-large-v3-gguf" / "snapshots" / "abc"
            snap.mkdir(parents=True)
            (snap / "whisper-large-v3-Q8_0.gguf").write_text("x")
            self.assertEqual(stt.engine(), "handy")

    def test_no_handy_falls_back_to_whisper_or_nothing(self):
        with mock.patch.dict(os.environ, {"JARVIEE_HANDY_BIN": str(Path(self.tmp.name) / "missing")}):
            self.assertEqual(stt.engine(), "")
            self.assertIn("brew install --cask handy", stt.status()["hint"])

    def test_reading_handy_json_output(self):
        out = "[2026-11-13][INFO] loading model\n{\"text\": \" add a quiz on Friday \", \"best_ms\": 410}\n"
        self.assertEqual(stt._handy_text(out).strip(), "add a quiz on Friday")
        self.assertEqual(stt._handy_text("[log] x\nhello there\n"), "hello there")


if __name__ == "__main__":
    unittest.main()
