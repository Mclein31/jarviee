"""Unit tests for hub.omniroute."""
import io
import json
import os
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hub import omniroute  # noqa: E402


def _cfg(**overrides):
    cfg = {
        "omniroute": {
            "enabled": True,
            "base_url": "http://127.0.0.1:20128",
            "keychain_service": "jarviee-omniroute",
            "health_path": "/v1/models",
            "health_timeout_s": 2,
            "health_cache_s": 30,
        },
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
            }
        },
    }
    cfg.update(overrides)
    return cfg


class _FakeCtxResp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeGetcodeResp:
    def __init__(self, code=200):
        self._code = code

    def getcode(self):
        return self._code


class OmniRouteTests(unittest.TestCase):
    def setUp(self):
        omniroute.reset_cache()
        self._orig_env = os.environ.pop("OMNIROUTE_API_KEY", None)

    def tearDown(self):
        omniroute.reset_cache()
        if self._orig_env is None:
            os.environ.pop("OMNIROUTE_API_KEY", None)
        else:
            os.environ["OMNIROUTE_API_KEY"] = self._orig_env

    def test_settings_merges_defaults_when_section_missing(self):
        s = omniroute.settings({})
        self.assertTrue(s["enabled"])
        self.assertEqual(s["base_url"], "http://127.0.0.1:20128")
        self.assertEqual(s["keychain_service"], "jarviee-omniroute")
        self.assertEqual(s["health_path"], "/v1/models")
        self.assertEqual(s["health_timeout_s"], 2.0)
        self.assertEqual(s["health_cache_s"], 30.0)

        s_none = omniroute.settings(None)
        self.assertEqual(s_none["base_url"], "http://127.0.0.1:20128")

        s_custom = omniroute.settings({"omniroute": {"base_url": "http://localhost:20128/", "health_path": "health"}})
        self.assertEqual(s_custom["base_url"], "http://localhost:20128")
        self.assertEqual(s_custom["health_path"], "/health")

    def test_enabled_requires_tools_omniroute(self):
        self.assertFalse(omniroute.enabled({}))
        self.assertFalse(omniroute.enabled({"omniroute": {"enabled": True}, "tools": {}}))
        self.assertTrue(omniroute.enabled({"tools": {"omniroute": {"models": {}}}}))
        self.assertFalse(omniroute.enabled({"omniroute": {"enabled": False}, "tools": {"omniroute": {}}}))
        self.assertFalse(omniroute.enabled(None))

    def test_api_key_env_beats_keychain(self):
        os.environ["OMNIROUTE_API_KEY"] = "  env-secret-key \n"
        called = []

        def fake_runner(*a, **kw):
            called.append(a)
            return SimpleNamespace(returncode=0, stdout="kc-secret")

        self.assertEqual(omniroute.api_key(_cfg(), runner=fake_runner), "env-secret-key")
        self.assertEqual(called, [])

    def test_api_key_keychain_lookup_and_failures(self):
        calls = []

        def ok_runner(argv, **kw):
            calls.append((argv, kw))
            return SimpleNamespace(returncode=0, stdout="  kc-secret-123\n")

        self.assertEqual(omniroute.api_key(_cfg(), runner=ok_runner), "kc-secret-123")
        self.assertEqual(
            calls[0][0],
            ["/usr/bin/security", "find-generic-password", "-s", "jarviee-omniroute", "-w"],
        )

        fail_runner = lambda *a, **kw: SimpleNamespace(returncode=44, stdout="", stderr="not found")
        self.assertIsNone(omniroute.api_key(_cfg(), runner=fail_runner))

        empty_runner = lambda *a, **kw: SimpleNamespace(returncode=0, stdout="   \n")
        self.assertIsNone(omniroute.api_key(_cfg(), runner=empty_runner))

        def boom_runner(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="security", timeout=5)

        self.assertIsNone(omniroute.api_key(_cfg(), runner=boom_runner))

    def test_store_key_success_and_errors(self):
        with self.assertRaises(ValueError):
            omniroute.store_key(_cfg(), "")
        with self.assertRaises(ValueError):
            omniroute.store_key(_cfg(), "   ")
        with self.assertRaises(ValueError):
            omniroute.store_key(_cfg(), None)

        seen = []

        def ok_runner(argv, **kw):
            seen.append(argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        omniroute.store_key(_cfg(), "  my-key-999  ", runner=ok_runner)
        self.assertEqual(
            seen[0],
            ["/usr/bin/security", "add-generic-password", "-U", "-s", "jarviee-omniroute", "-a", "jarviee", "-w", "my-key-999"],
        )

        def fail_runner(*a, **kw):
            return SimpleNamespace(returncode=1, stderr="keychain denied my-key-999")

        with self.assertRaises(OSError) as ctx:
            omniroute.store_key(_cfg(), "my-key-999", runner=fail_runner)
        self.assertNotIn("my-key-999", str(ctx.exception))

        def timeout_runner(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="security", timeout=10)

        with self.assertRaises(OSError):
            omniroute.store_key(_cfg(), "my-key-999", runner=timeout_runner)

    def test_tool_env_and_apply_env_do_not_mutate_and_pop_anthropic_api_key(self):
        os.environ["OMNIROUTE_API_KEY"] = "or-test-token"
        base_env = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-real-user", "OTHER": "1"}
        snapshot = dict(base_env)

        te = omniroute.tool_env(_cfg())
        self.assertEqual(
            te,
            {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:20128",
                "ANTHROPIC_AUTH_TOKEN": "or-test-token",
                "ANTHROPIC_API_KEY": None,
                "JARVIEE_TOOL": "omniroute",
            },
        )

        applied = omniroute.apply_env(base_env, _cfg())
        self.assertEqual(base_env, snapshot)
        self.assertNotIn("ANTHROPIC_API_KEY", applied)
        self.assertEqual(applied["ANTHROPIC_BASE_URL"], "http://127.0.0.1:20128")
        self.assertEqual(applied["ANTHROPIC_AUTH_TOKEN"], "or-test-token")
        self.assertEqual(applied["JARVIEE_TOOL"], "omniroute")
        self.assertEqual(applied["OTHER"], "1")

        # When key is missing, ANTHROPIC_AUTH_TOKEN is also popped
        os.environ.pop("OMNIROUTE_API_KEY", None)
        no_key_runner = lambda *a, **kw: SimpleNamespace(returncode=1, stdout="")
        applied_no_key = omniroute.apply_env({"ANTHROPIC_AUTH_TOKEN": "stale", "ANTHROPIC_API_KEY": "sk-x"}, _cfg(), runner=no_key_runner)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", applied_no_key)
        self.assertNotIn("ANTHROPIC_API_KEY", applied_no_key)

    def test_health_disabled_and_no_key(self):
        called = []

        def opener(req, timeout=None):
            called.append(req)
            return _FakeCtxResp(200)

        cfg_off = _cfg(omniroute={"enabled": False})
        self.assertEqual(omniroute.health(cfg_off, opener=opener), (False, "disabled"))
        self.assertEqual(called, [])

        with patch("hub.omniroute.api_key", return_value=None):
            self.assertEqual(
                omniroute.health(_cfg(), opener=opener),
                (False, "no API key (python3 hub.py omniroute connect)"),
            )
        self.assertEqual(called, [])

    def test_health_ok_and_request_shape(self):
        os.environ["OMNIROUTE_API_KEY"] = "test-omni-key"
        seen = []

        def opener(req, timeout=None):
            seen.append((req.full_url, req.get_method(), req.get_header("Authorization"), timeout))
            return _FakeGetcodeResp(200)

        ok, reason = omniroute.health(_cfg(), now=100.0, opener=opener)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertEqual(seen, [("http://127.0.0.1:20128/v1/models", "GET", "Bearer test-omni-key", 2.0)])

    def test_health_failures_refused_401_timeout_and_503(self):
        os.environ["OMNIROUTE_API_KEY"] = "test-omni-key"

        def refused(req, timeout=None):
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

        self.assertEqual(
            omniroute.health(_cfg(), now=10.0, opener=refused),
            (False, "gateway not running at http://127.0.0.1:20128"),
        )

        omniroute.reset_cache()

        def unauth(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

        self.assertEqual(omniroute.health(_cfg(), now=10.0, opener=unauth), (False, "HTTP 401: key rejected"))

        omniroute.reset_cache()

        def timed_out(req, timeout=None):
            raise TimeoutError("timed out")

        self.assertEqual(omniroute.health(_cfg(), now=10.0, opener=timed_out), (False, "timed out"))

        omniroute.reset_cache()

        def srv_err(req, timeout=None):
            return _FakeCtxResp(503)

        self.assertEqual(omniroute.health(_cfg(), now=10.0, opener=srv_err), (False, "HTTP 503"))

    def test_health_cache_hit_and_expiry_for_both_ok_and_failed(self):
        os.environ["OMNIROUTE_API_KEY"] = "test-omni-key"
        calls = 0

        def fail_once(req, timeout=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
            return _FakeCtxResp(200)

        # Failed probe is cached for 30s
        self.assertEqual(omniroute.health(_cfg(), now=1000.0, opener=fail_once)[0], False)
        self.assertEqual(omniroute.health(_cfg(), now=1029.0, opener=fail_once)[0], False)
        self.assertEqual(calls, 1)

        # After cache window expires, re-probes and succeeds
        self.assertEqual(omniroute.health(_cfg(), now=1030.0, opener=fail_once), (True, "ok"))
        self.assertEqual(omniroute.health(_cfg(), now=1050.0, opener=fail_once), (True, "ok"))
        self.assertEqual(calls, 2)

        # Different base_url has its own cache entry
        cfg2 = _cfg()
        cfg2["omniroute"]["base_url"] = "http://127.0.0.1:20129"
        self.assertEqual(omniroute.health(cfg2, now=1050.0, opener=fail_once), (True, "ok"))
        self.assertEqual(calls, 3)

        # reset_cache clears it
        omniroute.reset_cache()
        self.assertEqual(omniroute.health(_cfg(), now=1050.0, opener=fail_once), (True, "ok"))
        self.assertEqual(calls, 4)

    def test_status_never_contains_key(self):
        secret = "super-secret-omniroute-token-xyz"
        os.environ["OMNIROUTE_API_KEY"] = secret

        def bad_opener(req, timeout=None):
            raise RuntimeError(f"failed with Bearer {secret}")

        st = omniroute.status(_cfg(), opener=bad_opener)
        self.assertEqual(
            set(st.keys()),
            {"enabled", "base_url", "has_key", "healthy", "reason", "models"},
        )
        self.assertTrue(st["enabled"])
        self.assertTrue(st["has_key"])
        self.assertFalse(st["healthy"])
        self.assertEqual(st["models"], _cfg()["tools"]["omniroute"]["models"])
        serialized = json.dumps(st)
        self.assertNotIn(secret, serialized)
        self.assertIn("***", st["reason"])

    def test_redact_handles_none_empty_key_and_bearer_tokens(self):
        with patch("hub.omniroute.api_key", return_value=None):
            self.assertEqual(omniroute.redact(None), "")
            self.assertEqual(omniroute.redact(""), "")
            self.assertEqual(omniroute.redact("Authorization: Bearer sk-abc123456"), "Authorization: Bearer ***")
            self.assertEqual(omniroute.redact("key=sk-test-987654321 ok"), "key=*** ok")

        os.environ["OMNIROUTE_API_KEY"] = "custom-non-sk-key"
        self.assertEqual(
            omniroute.redact("header Bearer custom-non-sk-key and raw custom-non-sk-key"),
            "header Bearer *** and raw ***",
        )

    def test_store_key_resets_health_cache(self):
        os.environ["OMNIROUTE_API_KEY"] = "old-bad-key"
        calls = 0

        def opener(req, timeout=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
            return _FakeCtxResp(200)

        self.assertEqual(omniroute.health(_cfg(), now=100.0, opener=opener), (False, "HTTP 401: key rejected"))
        ok_runner = lambda *a, **kw: SimpleNamespace(returncode=0, stdout="", stderr="")
        omniroute.store_key(_cfg(), "new-good-key", runner=ok_runner)
        self.assertEqual(omniroute.health(_cfg(), now=105.0, opener=opener), (True, "ok"))
        self.assertEqual(calls, 2)

    def test_status_disabled_with_models_and_malformed_cfg_never_raises(self):
        with patch("hub.omniroute.api_key", return_value=None):
            st = omniroute.status(_cfg(omniroute={"enabled": False}))
            self.assertFalse(st["enabled"])
            self.assertFalse(st["has_key"])
            self.assertFalse(st["healthy"])
            self.assertEqual(st["reason"], "disabled")
            self.assertEqual(st["models"], _cfg()["tools"]["omniroute"]["models"])

            bad_cfg = {"omniroute": {"health_timeout_s": "bad", "health_cache_s": -5}, "tools": "not-a-dict"}
            self.assertFalse(omniroute.enabled(bad_cfg))
            self.assertEqual(omniroute.settings(bad_cfg)["health_timeout_s"], 2)
            self.assertEqual(omniroute.settings(bad_cfg)["health_cache_s"], 30)
            self.assertEqual(omniroute.settings({"omniroute": {"health_cache_s": "nope"}})["health_cache_s"], 30)
            self.assertEqual(omniroute.settings({"omniroute": {"health_cache_s": 0}})["health_cache_s"], 0.0)
            self.assertEqual(omniroute.status(bad_cfg)["models"], {})

    def test_keychain_cached_across_status_and_second_health(self):
        sec_calls = []

        def fake_security(argv, **kw):
            sec_calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="kc-cached-secret\n")

        opener_calls = 0

        def ok_opener(req, timeout=None):
            nonlocal opener_calls
            opener_calls += 1
            return _FakeCtxResp(200)

        with patch("subprocess.run", side_effect=fake_security):
            st = omniroute.status(_cfg(), opener=ok_opener)
            self.assertTrue(st["has_key"])
            self.assertTrue(st["healthy"])
            self.assertEqual(omniroute.health(_cfg(), opener=ok_opener), (True, "ok"))
            self.assertEqual(len(sec_calls), 1)
            self.assertEqual(opener_calls, 1)

            # Env var wins immediately without poisoning or using the Keychain cache
            os.environ["OMNIROUTE_API_KEY"] = "env-override-key"
            self.assertEqual(omniroute.api_key(_cfg()), "env-override-key")
            os.environ.pop("OMNIROUTE_API_KEY", None)
            self.assertEqual(omniroute.api_key(_cfg()), "kc-cached-secret")
            self.assertEqual(len(sec_calls), 1)

            # store_key and reset_cache clear the Keychain cache
            omniroute.store_key(_cfg(), "new-kc-secret", runner=lambda *a, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""))
            self.assertEqual(omniroute.api_key(_cfg()), "kc-cached-secret")
            self.assertEqual(len(sec_calls), 2)

            omniroute.reset_cache()
            self.assertEqual(omniroute.api_key(_cfg()), "kc-cached-secret")
            self.assertEqual(len(sec_calls), 3)

    def test_health_checks_cache_before_reading_keychain(self):
        sec_calls = []

        def fake_security(argv, **kw):
            sec_calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="kc-secret\n")

        os.environ["OMNIROUTE_API_KEY"] = "warm-key"
        omniroute.health(_cfg(), now=1000.0, opener=lambda req, timeout=None: _FakeCtxResp(200))
        os.environ.pop("OMNIROUTE_API_KEY", None)

        with patch("subprocess.run", side_effect=fake_security):
            self.assertEqual(
                omniroute.health(_cfg(), now=1010.0, opener=lambda req, timeout=None: _FakeCtxResp(200)),
                (True, "ok"),
            )
            self.assertEqual(sec_calls, [])


if __name__ == "__main__":
    unittest.main()
