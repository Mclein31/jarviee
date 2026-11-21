"""Tests for config loading, schema defaults, and job configuration keys."""
import json
import tempfile
import unittest
from pathlib import Path

from hub.config import (
    DEFAULT_COMPLEX_JOB_DURATION_SECONDS,
    DEFAULT_COMPLEX_JOB_MIN_STEPS,
    DEFAULT_FINISHED_JOB_RETENTION_SECONDS,
    ROOT,
    load_config,
    migrate,
)


class ConfigJobDefaultsTests(unittest.TestCase):
    def test_default_config_json_values(self):
        """Verifies that config.json contains the expected defaults."""
        raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(raw.get("complex_job_duration_seconds"), 60)
        self.assertEqual(raw.get("complex_job_min_steps"), 2)
        self.assertEqual(raw.get("finished_job_retention_seconds"), 300)

        cfg = load_config(ROOT / "config.json")
        self.assertEqual(cfg["complex_job_duration_seconds"], 60)
        self.assertEqual(cfg["complex_job_min_steps"], 2)
        self.assertEqual(cfg["finished_job_retention_seconds"], 300)

    def test_fallbacks_when_keys_are_absent(self):
        """Missing keys should fall back to default values."""
        cfg = migrate({})
        self.assertEqual(cfg["complex_job_duration_seconds"], DEFAULT_COMPLEX_JOB_DURATION_SECONDS)
        self.assertEqual(cfg["complex_job_min_steps"], DEFAULT_COMPLEX_JOB_MIN_STEPS)
        self.assertEqual(cfg["finished_job_retention_seconds"], DEFAULT_FINISHED_JOB_RETENTION_SECONDS)

        # Via load_config on a file with absent keys
        with tempfile.TemporaryDirectory() as td:
            base = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
            base.pop("complex_job_duration_seconds", None)
            base.pop("complex_job_min_steps", None)
            base.pop("finished_job_retention_seconds", None)
            p = Path(td) / "config.json"
            p.write_text(json.dumps(base), encoding="utf-8")

            loaded = load_config(p)
            self.assertEqual(loaded["complex_job_duration_seconds"], 60)
            self.assertEqual(loaded["complex_job_min_steps"], 2)
            self.assertEqual(loaded["finished_job_retention_seconds"], 300)

    def test_custom_config_values(self):
        """Custom configured values should be respected and preserved."""
        custom = {
            "complex_job_duration_seconds": 120,
            "complex_job_min_steps": 5,
            "finished_job_retention_seconds": 600,
        }
        cfg = migrate(dict(custom))
        self.assertEqual(cfg["complex_job_duration_seconds"], 120)
        self.assertEqual(cfg["complex_job_min_steps"], 5)
        self.assertEqual(cfg["finished_job_retention_seconds"], 600)

        with tempfile.TemporaryDirectory() as td:
            base = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
            base.update(custom)
            p = Path(td) / "config.json"
            p.write_text(json.dumps(base), encoding="utf-8")

            loaded = load_config(p)
            self.assertEqual(loaded["complex_job_duration_seconds"], 120)
            self.assertEqual(loaded["complex_job_min_steps"], 5)
            self.assertEqual(loaded["finished_job_retention_seconds"], 600)

    def test_coercion_and_invalid_fallbacks(self):
        """String numbers should be coerced to int; invalid values should fall back to default."""
        cfg = migrate({
            "complex_job_duration_seconds": "90",
            "complex_job_min_steps": "3",
            "finished_job_retention_seconds": "450",
        })
        self.assertEqual(cfg["complex_job_duration_seconds"], 90)
        self.assertEqual(cfg["complex_job_min_steps"], 3)
        self.assertEqual(cfg["finished_job_retention_seconds"], 450)

        cfg_invalid = migrate({
            "complex_job_duration_seconds": "not-an-int",
            "complex_job_min_steps": None,
            "finished_job_retention_seconds": [],
        })
        self.assertEqual(cfg_invalid["complex_job_duration_seconds"], 60)
        self.assertEqual(cfg_invalid["complex_job_min_steps"], 2)
        self.assertEqual(cfg_invalid["finished_job_retention_seconds"], 300)


if __name__ == "__main__":
    unittest.main()


class SavedSettingsTypesTest(unittest.TestCase):
    """Dashboard settings survive a restart with their own types (they used to be forced to booleans)."""

    def test_non_finite_numbers_and_type_changes_are_rejected(self):
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path
        from hub import config as _config
        with _tempfile.TemporaryDirectory() as d:
            path = str(_Path(d) / "settings.json")
            _Path(path).write_text('{"voice": {"report_volume": NaN, "duck_volume": Infinity, "enabled": 1}, "push": {"lead_min": 25.0, "enabled": 0}}')
            base = {"settings_path": path, "voice": {"report_volume": 70, "duck_volume": 20, "enabled": True},
                    "push": {"lead_min": 15, "enabled": True}}
            out = _config.apply_settings(_json.loads(_json.dumps(base)))
            self.assertEqual(out["voice"], {"report_volume": 70, "duck_volume": 20, "enabled": True})
            self.assertIs(out["voice"]["enabled"], True)       # 1 is not a switch
            self.assertEqual(out["push"]["lead_min"], 25)
            self.assertIsInstance(out["push"]["lead_min"], int)   # a whole float stays an int
            self.assertIs(out["push"]["enabled"], True)
            self.assertIsNone(_config._saved_value(float("inf")))
            self.assertIsNone(_config._saved_value(70.5, 70))

    def test_numbers_times_and_lists_round_trip(self):
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path
        from hub import config as _config
        with _tempfile.TemporaryDirectory() as d:
            path = str(_Path(d) / "settings.json")
            cfg = {"settings_path": path, "voice": {"enabled": False, "report_volume": 80, "duck_volume": 10},
                   "push": {"enabled": True, "lead_min": 25, "digest_at": "06:30", "mute_calendars": ["Classes"],
                            "limit_alerts": False}}
            _config.save_settings(cfg)
            fresh = _config.apply_settings({"settings_path": path, "voice": {"report_volume": 70}})
            self.assertEqual(fresh["voice"], {"report_volume": 80, "enabled": False, "duck_volume": 10})
            self.assertEqual(fresh["push"]["lead_min"], 25)
            self.assertEqual(fresh["push"]["digest_at"], "06:30")
            self.assertEqual(fresh["push"]["mute_calendars"], ["Classes"])
            self.assertIs(fresh["push"]["limit_alerts"], False)
            _Path(path).write_text(_json.dumps({"voice": {"report_volume": {"evil": 1}}}))
            self.assertEqual(_config.apply_settings({"settings_path": path, "voice": {"report_volume": 70}})["voice"],
                             {"report_volume": 70})
