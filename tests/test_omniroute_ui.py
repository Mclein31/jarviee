"""Tests for OmniRoute dashboard status endpoint and settings persistence."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from hub import config, routes
from tests.helpers import TempDir, make_cfg


class _DummyCtx:
    def __init__(self, cfg, body=None):
        self.cfg = cfg
        self.body = body or {}
        self.path = ""
        self.query = ""


class OmniRouteUITests(unittest.TestCase):
    def test_status_endpoint_never_leaks_key(self):
        with TempDir() as d:
            cfg, _ = make_cfg(Path(d))
            cfg["omniroute"] = {"enabled": True, "base_url": "http://127.0.0.1:20128"}
            cfg["tools"]["omniroute"] = {"models": {"basic": "claude/combo/jarviee-basic"}}
            secret = "omni-secret-token-xyz"
            with patch("hub.omniroute.api_key", return_value=secret), \
                 patch("hub.omniroute.health", return_value=(True, "ok")):
                status, payload = routes.api_omniroute_status(_DummyCtx(cfg))
            self.assertEqual(status, 200)
            self.assertTrue(payload["enabled"])
            self.assertTrue(payload["has_key"])
            self.assertTrue(payload["healthy"])
            self.assertNotIn(secret, json.dumps(payload))

    def test_fallback_toggle_persists_and_reloads_omniroute_enabled(self):
        with TempDir() as d:
            root = Path(d)
            cfg, _ = make_cfg(root)
            cfg["settings_path"] = str(root / "settings.json")
            cfg["omniroute"] = {"enabled": True}

            status, payload = routes.api_settings_fallback(_DummyCtx(cfg, {"omniroute": False}))
            self.assertEqual(status, 200)
            self.assertFalse(payload["omniroute"])
            self.assertFalse(cfg["omniroute"]["enabled"])

            reloaded = {"settings_path": cfg["settings_path"], "omniroute": {"enabled": True}}
            config.apply_settings(reloaded)
            self.assertFalse(reloaded["omniroute"]["enabled"])


if __name__ == "__main__":
    unittest.main()
