"""Unit tests for `hub.py omniroute` CLI commands: status, connect, and test."""
import importlib.util
import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from tests.helpers import ROOT, TempDir

_hub_spec = importlib.util.spec_from_file_location("hub_cli", str(ROOT / "hub.py"))
hub_cli = importlib.util.module_from_spec(_hub_spec)
_hub_spec.loader.exec_module(hub_cli)


class TestOmnirouteCLI(unittest.TestCase):

    def test_status_text_healthy(self):
        fake_cfg = {
            "tools": {
                "omniroute": {
                    "models": {
                        "basic": "claude/combo/jarviee-basic",
                        "light": "claude/combo/jarviee-light",
                    }
                }
            }
        }
        st = {
            "enabled": True,
            "base_url": "http://127.0.0.1:20128",
            "has_key": True,
            "healthy": True,
            "reason": "ok",
            "models": {
                "basic": "claude/combo/jarviee-basic",
                "light": "claude/combo/jarviee-light",
            },
        }
        with patch.object(hub_cli, "load_config", return_value=fake_cfg), \
             patch("hub.omniroute.status", return_value=st) as mock_st, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                hub_cli.main(["omniroute", "status"])
            self.assertEqual(cm.exception.code, 0)
            mock_st.assert_called_once_with(fake_cfg)
            out = mock_out.getvalue()
            self.assertIn("OmniRoute: enabled", out)
            self.assertIn("Base URL:  http://127.0.0.1:20128", out)
            self.assertIn("API key:   present", out)
            self.assertIn("Gateway:   healthy", out)
            self.assertIn("Combos:", out)
            self.assertIn("basic -> claude/combo/jarviee-basic", out)

    def test_status_text_unhealthy_exit_1(self):
        fake_cfg = {"tools": {}}
        st = {
            "enabled": False,
            "base_url": "http://127.0.0.1:20128",
            "has_key": False,
            "healthy": False,
            "reason": "disabled",
            "models": {},
        }
        with patch.object(hub_cli, "load_config", return_value=fake_cfg), \
             patch("hub.omniroute.status", return_value=st), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                hub_cli.main(["omniroute", "status"])
            self.assertEqual(cm.exception.code, 1)
            out = mock_out.getvalue()
            self.assertIn("OmniRoute: disabled", out)
            self.assertIn("API key:   missing", out)
            self.assertIn("Gateway:   unhealthy (disabled)", out)

    def test_status_json_healthy_and_unhealthy(self):
        fake_cfg = {"tools": {}}
        st_ok = {
            "enabled": True,
            "base_url": "http://127.0.0.1:20128",
            "has_key": True,
            "healthy": True,
            "reason": "ok",
            "models": {},
        }
        with patch.object(hub_cli, "load_config", return_value=fake_cfg), \
             patch("hub.omniroute.status", return_value=st_ok), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                hub_cli.main(["omniroute", "status", "--json"])
            self.assertEqual(cm.exception.code, 0)
            data = json.loads(mock_out.getvalue())
            self.assertTrue(data["healthy"])
            self.assertTrue(data["enabled"])

        st_fail = dict(st_ok, healthy=False, reason="timed out")
        with patch.object(hub_cli, "load_config", return_value=fake_cfg), \
             patch("hub.omniroute.status", return_value=st_fail), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                hub_cli.main(["omniroute", "status", "--json"])
            self.assertEqual(cm.exception.code, 1)
            data = json.loads(mock_out.getvalue())
            self.assertFalse(data["healthy"])
            self.assertEqual(data["reason"], "timed out")

    def test_connect_refuses_empty_key(self):
        fake_cfg = {}
        for empty_val in ["", "   ", "\t\n"]:
            with self.subTest(empty_val=empty_val):
                with patch.object(hub_cli, "load_config", return_value=fake_cfg), \
                     patch("getpass.getpass", return_value=empty_val), \
                     patch("hub.omniroute.store_key") as mock_store, \
                     patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                    with self.assertRaises(SystemExit) as cm:
                        hub_cli.main(["omniroute", "connect"])
                    self.assertEqual(cm.exception.code, 1)
                    mock_store.assert_not_called()
                    self.assertIn("error: key cannot be empty", mock_err.getvalue())

    def test_connect_stores_key_and_checks_health_healthy(self):
        fake_cfg = {}
        with patch.object(hub_cli, "load_config", return_value=fake_cfg), \
             patch("getpass.getpass", return_value="secret-omni-key-123"), \
             patch("hub.omniroute.store_key") as mock_store, \
             patch("hub.omniroute.health", return_value=(True, "ok")) as mock_health, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                hub_cli.main(["omniroute", "connect"])
            self.assertEqual(cm.exception.code, 0)
            mock_store.assert_called_once_with(fake_cfg, "secret-omni-key-123")
            mock_health.assert_called_once_with(fake_cfg)
            out = mock_out.getvalue()
            self.assertIn("Key stored in Keychain.", out)
            self.assertIn("Gateway health: healthy", out)

    def test_connect_stores_key_and_reports_unhealthy_exit_1(self):
        fake_cfg = {}
        with patch.object(hub_cli, "load_config", return_value=fake_cfg), \
             patch("getpass.getpass", return_value="secret-omni-key-123"), \
             patch("hub.omniroute.store_key") as mock_store, \
             patch("hub.omniroute.health", return_value=(False, "HTTP 401: key rejected")), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                hub_cli.main(["omniroute", "connect"])
            self.assertEqual(cm.exception.code, 1)
            mock_store.assert_called_once_with(fake_cfg, "secret-omni-key-123")
            out = mock_out.getvalue()
            self.assertIn("Gateway health: unhealthy (HTTP 401: key rejected)", out)

    def test_connect_store_key_failure(self):
        fake_cfg = {}
        with patch.object(hub_cli, "load_config", return_value=fake_cfg), \
             patch("getpass.getpass", return_value="some-key"), \
             patch("hub.omniroute.store_key", side_effect=OSError("Keychain access denied")), \
             patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            with self.assertRaises(SystemExit) as cm:
                hub_cli.main(["omniroute", "connect"])
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("error: failed to store key: Keychain access denied", mock_err.getvalue())

    def test_test_refuses_without_key(self):
        fake_cfg = {}
        with patch.object(hub_cli, "load_config", return_value=fake_cfg), \
             patch("hub.omniroute.api_key", return_value=None), \
             patch("subprocess.run") as mock_run, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            with self.assertRaises(SystemExit) as cm:
                hub_cli.main(["omniroute", "test"])
            self.assertEqual(cm.exception.code, 1)
            mock_run.assert_not_called()
            self.assertIn("error: no API key found", mock_err.getvalue())

    def test_test_refuses_when_gateway_unhealthy(self):
        fake_cfg = {}
        with patch.object(hub_cli, "load_config", return_value=fake_cfg), \
             patch("hub.omniroute.api_key", return_value="present-key"), \
             patch("hub.omniroute.health", return_value=(False, "gateway not running at http://127.0.0.1:20128")), \
             patch("subprocess.run") as mock_run, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            with self.assertRaises(SystemExit) as cm:
                hub_cli.main(["omniroute", "test"])
            self.assertEqual(cm.exception.code, 1)
            mock_run.assert_not_called()
            self.assertIn("gateway health check failed (gateway not running at http://127.0.0.1:20128)", mock_err.getvalue())

    def test_test_refuses_when_tools_omniroute_missing(self):
        fake_cfg = {"tools": {}}
        with patch.object(hub_cli, "load_config", return_value=fake_cfg), \
             patch("hub.omniroute.api_key", return_value="present-key"), \
             patch("hub.omniroute.health", return_value=(True, "ok")), \
             patch("subprocess.run") as mock_run, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            with self.assertRaises(SystemExit) as cm:
                hub_cli.main(["omniroute", "test"])
            self.assertEqual(cm.exception.code, 1)
            mock_run.assert_not_called()
            self.assertIn("OmniRoute is not configured in config.json (tools.omniroute)", mock_err.getvalue())

    def test_test_env_for_error_fails_fast(self):
        fake_cfg = {
            "tools": {
                "omniroute": {
                    "command": ["claude", "-p", "{prompt}", "--model", "{model}"],
                    "env_from": "omniroute",
                }
            }
        }
        with patch.object(hub_cli, "load_config", return_value=fake_cfg), \
             patch("hub.omniroute.api_key", return_value="key"), \
             patch("hub.omniroute.health", return_value=(True, "ok")), \
             patch("hub.runplan.env_for", side_effect=ValueError("OmniRoute has no API key")), \
             patch("subprocess.run") as mock_run, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            with self.assertRaises(SystemExit) as cm:
                hub_cli.main(["omniroute", "test"])
            self.assertEqual(cm.exception.code, 1)
            mock_run.assert_not_called()
            self.assertIn("OmniRoute has no API key", mock_err.getvalue())

    def test_test_success_builds_correct_argv_and_env_no_key_in_argv(self):
        fake_cfg = {
            "tools": {
                "omniroute": {
                    "command": ["claude", "-p", "{prompt}", "--model", "{model}", "--permission-mode", "auto"],
                    "env_from": "omniroute",
                    "models": {
                        "basic": "claude/combo/jarviee-basic",
                        "light": "claude/combo/jarviee-light",
                        "complex": "claude/combo/jarviee-complex",
                        "heavy": "claude/combo/jarviee-heavy",
                    },
                    "efforts": {"basic": "low", "light": "low", "complex": "medium", "heavy": "high"},
                }
            }
        }
        test_key = "secret-omni-auth-token-456"
        expected_env = {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:20128",
            "ANTHROPIC_AUTH_TOKEN": test_key,
            "JARVIEE_TOOL": "omniroute",
        }
        fake_res = MagicMock()
        fake_res.returncode = 0
        fake_res.stdout = "I am ready. OK"
        fake_res.stderr = ""

        with patch.object(hub_cli, "load_config", return_value=fake_cfg), \
             patch("hub.omniroute.api_key", return_value=test_key), \
             patch("hub.omniroute.health", return_value=(True, "ok")), \
             patch("hub.runplan.env_for", return_value=expected_env) as mock_env_for, \
             patch("subprocess.run", return_value=fake_res) as mock_run, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                hub_cli.main(["omniroute", "test"])
            self.assertEqual(cm.exception.code, 0)
            mock_env_for.assert_called_once()
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            argv = args[0]
            self.assertEqual(argv, ["claude", "-p", "Reply with the single word OK", "--model", "claude/combo/jarviee-basic", "--permission-mode", "auto"])
            # Ensure secret key is NEVER in argv
            for arg in argv:
                self.assertNotIn(test_key, arg)
            # Ensure env passed
            self.assertEqual(kwargs.get("env"), expected_env)
            self.assertEqual(kwargs.get("timeout"), 90)
            self.assertIn("Test successful: reply contains OK.", mock_out.getvalue())

    def test_test_failure_when_ok_in_stderr_only(self):
        # res.stdout does NOT contain OK, but stderr contains "OK"
        fake_cfg = {
            "tools": {
                "omniroute": {
                    "command": ["claude", "-p", "{prompt}", "--model", "{model}"],
                    "env_from": "omniroute",
                }
            }
        }
        fake_res = MagicMock()
        fake_res.returncode = 0
        fake_res.stdout = "Something else happened"
        fake_res.stderr = "Everything was OK on line 5"

        with patch.object(hub_cli, "load_config", return_value=fake_cfg), \
             patch("hub.omniroute.api_key", return_value="key"), \
             patch("hub.omniroute.health", return_value=(True, "ok")), \
             patch("hub.runplan.env_for", return_value={}), \
             patch("subprocess.run", return_value=fake_res), \
             patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            with self.assertRaises(SystemExit) as cm:
                hub_cli.main(["omniroute", "test"])
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("Test failed (exit code 0).", mock_err.getvalue())

    def test_test_output_redacts_keys(self):
        fake_cfg = {
            "tools": {
                "omniroute": {
                    "command": ["claude", "-p", "{prompt}", "--model", "{model}"],
                    "env_from": "omniroute",
                }
            }
        }
        fake_res = MagicMock()
        fake_res.returncode = 0
        fake_res.stdout = "OK with sk-1234567890abcdef"
        fake_res.stderr = "Bearer secret-token-xyz"

        with patch.object(hub_cli, "load_config", return_value=fake_cfg), \
             patch("hub.omniroute.api_key", return_value="secret-token-xyz"), \
             patch("hub.omniroute.health", return_value=(True, "ok")), \
             patch("hub.runplan.env_for", return_value={}), \
             patch("subprocess.run", return_value=fake_res), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
             patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            with self.assertRaises(SystemExit) as cm:
                hub_cli.main(["omniroute", "test"])
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("OK with ***", mock_out.getvalue())
            self.assertNotIn("sk-1234567890abcdef", mock_out.getvalue())
            self.assertIn("Bearer ***", mock_err.getvalue())
            self.assertNotIn("secret-token-xyz", mock_err.getvalue())

    def test_test_timeout_expired(self):
        fake_cfg = {
            "tools": {
                "omniroute": {
                    "command": ["claude", "-p", "{prompt}", "--model", "{model}"],
                    "env_from": "omniroute",
                }
            }
        }
        with patch.object(hub_cli, "load_config", return_value=fake_cfg), \
             patch("hub.omniroute.api_key", return_value="key"), \
             patch("hub.omniroute.health", return_value=(True, "ok")), \
             patch("hub.runplan.env_for", return_value={}), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=90)), \
             patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            with self.assertRaises(SystemExit) as cm:
                hub_cli.main(["omniroute", "test"])
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("error: test prompt timed out after 90s", mock_err.getvalue())


if __name__ == "__main__":
    unittest.main()
