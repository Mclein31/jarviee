"""The Jarviee name and the Agent Hub names it still accepts: env vars, data folder, token header, hook keys."""
import os
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from helpers import TempDir, make_cfg
from hub import auth, brand
from hub.server import handle_hook, make_server
from hub.store import Store


class BrandTests(unittest.TestCase):
    def test_env_prefers_new_name_and_falls_back_to_old(self):
        with patch.dict(os.environ, {"AGENT_HUB_PORT": "1111"}, clear=False):
            os.environ.pop("JARVIEE_PORT", None)
            self.assertEqual(brand.env("PORT"), "1111")
            os.environ["JARVIEE_PORT"] = "2222"
            self.assertEqual(brand.env("PORT"), "2222")
        self.assertEqual(brand.env("SURELY_UNSET_KEY", "d"), "d")

    def test_data_path_maps_both_prefixes_to_home(self):
        with tempfile.TemporaryDirectory() as td, \
                patch.object(brand, "HOME", Path(td) / ".jarviee"), patch.object(brand, "LEGACY_HOME", Path(td) / ".agent-hub"):
            self.assertEqual(brand.data_path("~/.jarviee/hub.db"), Path(td) / ".jarviee" / "hub.db")
            (Path(td) / ".agent-hub").mkdir()   # not migrated yet: config paths land in the old folder
            self.assertEqual(brand.data_path("~/.jarviee/hub.db"), Path(td) / ".agent-hub" / "hub.db")
            self.assertEqual(brand.data_path("~/.agent-hub/logs"), Path(td) / ".agent-hub" / "logs")
            self.assertEqual(brand.data_path("/abs/x.db"), Path("/abs/x.db"))

    def test_migrate_home_moves_once_and_leaves_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            new, old = Path(td) / ".jarviee", Path(td) / ".agent-hub"
            old.mkdir()
            (old / "token").write_text("t\n")
            self.assertTrue(brand.migrate_home(new, old))
            self.assertEqual((new / "token").read_text(), "t\n")
            self.assertTrue(old.is_symlink())
            self.assertEqual((old / "token").read_text(), "t\n")   # old hooks still find it
            self.assertFalse(brand.migrate_home(new, old))         # second run is a no-op

    def test_migrate_home_without_old_folder_does_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(brand.migrate_home(Path(td) / ".jarviee", Path(td) / ".agent-hub"))
            self.assertFalse((Path(td) / ".jarviee").exists())


class LegacyProtocolTests(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.cfg, _ = make_cfg(self._t.__enter__())
        self.store = Store(self.cfg["db_path"])
        self.srv = make_server(self.cfg, self.store, scheduler=False)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self._t.__exit__()

    def _post(self, header):
        req = urllib.request.Request(f"http://127.0.0.1:{self.cfg['port']}/api/clear", data=b"{}",
                                     headers={"Content-Type": "application/json", header: auth.get_token(self.cfg)})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3) as r:
            return r.status

    def test_both_token_headers_are_accepted(self):
        self.assertEqual(self._post(brand.TOKEN_HEADER), 200)
        self.assertEqual(self._post(brand.LEGACY_TOKEN_HEADER), 200)

    def test_hook_payload_keys_from_before_the_rename_still_attach(self):
        aid = handle_hook("SessionStart", {"session_id": "old1", "agentHubTool": "claude"}, self.cfg, self.store)
        self.assertEqual(aid, "claude-old1")
        aid = handle_hook("SessionStart", {"session_id": "new1", "jarvieeTool": "claude"}, self.cfg, self.store)
        self.assertEqual(aid, "claude-new1")
        aid = handle_hook("UserPromptSubmit", {"session_id": "new1", "jarvieeTool": "claude", "prompt": "rename the thing",
                                               "jarvieeTitle": "Rename it", "jarvieeSummary": "short"}, self.cfg, self.store)
        a = self.store.get(aid)
        self.assertEqual((a["title"], a["summary"]), ("Rename it", "short"))


if __name__ == "__main__":
    unittest.main()
