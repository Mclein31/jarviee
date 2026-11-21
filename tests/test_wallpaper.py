"""The photo behind the glass (hub/wallpaper.py): the desktop picture from the wallpaper store, the dark appearance
of a dynamic HEIC, conversion to a browser JPEG, and the choices."""
import base64
import plistlib
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from helpers import TempDir   # noqa: F401  (puts the repo on sys.path)
from hub import wallpaper


def store_with(url):
    conf = plistlib.dumps({"type": "systemDesktopPicture", "url": {"relative": url}}, fmt=plistlib.FMT_BINARY)
    return plistlib.dumps({"SystemDefault": {"Desktop": {"Content": {"Choices": [{"Configuration": conf, "Provider": "x"}]}}},
                           "AllSpacesAndDisplays": {"Idle": {}}})


def heic_with_appearance(d=1, l=0, key="apr"):
    meta = base64.b64encode(plistlib.dumps({"d": d, "l": l} if key == "apr" else {"ap": {"d": d, "l": l}}, fmt=plistlib.FMT_BINARY))
    return b"\x00\x00\x00\x18ftypheic" + b"x" * 200 + b'apple_desktop:' + key.encode() + b'="' + meta + b'"' + b"y" * 100


class Desktop(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = Path(self._t.__enter__())

    def tearDown(self):
        self._t.__exit__()

    def test_plain_picture_url(self):
        pic = self.tmp / "My Photo.jpg"
        pic.write_bytes(b"jpg")
        st = self.tmp / "Index.plist"
        st.write_bytes(store_with(pic.as_uri()))
        self.assertEqual(wallpaper.desktop_file(st), pic)

    def test_system_wallpaper_maps_to_its_downloaded_still(self):
        assets = self.tmp / "assets"
        assets.mkdir()
        (assets / "hello Red.heic").write_bytes(b"h")
        st = self.tmp / "Index.plist"
        st.write_bytes(store_with("file:///System/Library/Desktop%20Pictures/hello%20Red.madesktop"))
        with mock.patch.object(wallpaper, "ASSETS", assets), mock.patch.object(wallpaper, "SYSTEM", self.tmp / "none"):
            self.assertEqual(wallpaper.desktop_file(st), assets / "hello Red.heic")

    def test_unreadable_or_video_desktop_gives_none(self):
        self.assertIsNone(wallpaper.desktop_file(self.tmp / "missing.plist"))
        st = self.tmp / "Index.plist"
        st.write_bytes(store_with("file:///Library/Aerials/x.mov"))
        self.assertIsNone(wallpaper.desktop_file(st))

    def test_dark_index_from_appearance_metadata(self):
        for key in ("apr", "h24", "solar"):
            f = self.tmp / f"dyn-{key}.heic"
            f.write_bytes(heic_with_appearance(1, 0, key))
            self.assertEqual(wallpaper.dark_index(f), 1, key)
        plain = self.tmp / "plain.heic"
        plain.write_bytes(b"\x00ftypheic" + b"z" * 300)
        self.assertIsNone(wallpaper.dark_index(plain))
        light_first = self.tmp / "zero.heic"
        light_first.write_bytes(heic_with_appearance(0, 1))
        self.assertIsNone(wallpaper.dark_index(light_first))   # the primary image already is the dark one
        self.assertIsNone(wallpaper.dark_index(self.tmp / "x.png"))


class Convert(unittest.TestCase):
    def setUp(self):
        self._t = TempDir()
        self.tmp = Path(self._t.__enter__())
        self.calls = []

    def tearDown(self):
        self._t.__exit__()

    def runner(self, argv, **k):
        self.calls.append(argv)
        out = Path(argv[-1])
        out.write_bytes(b"\xff\xd8jpeg")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def test_converts_once_and_caches_by_file(self):
        pic = self.tmp / "a.png"
        pic.write_bytes(b"png")
        cfg = {"wallpaper_dir": str(self.tmp / "cache"), "appearance": {"wallpaper": "custom-pic"}}
        with mock.patch.dict(wallpaper.CHOICES, {"custom-pic": ("Custom Pic", (pic,))}):
            path, v = wallpaper.jpeg(cfg, self.runner)
            self.assertTrue(path.is_file())
            self.assertEqual(self.calls[0][0], "/usr/bin/sips")
            self.assertIn("2560", self.calls[0])
            again, v2 = wallpaper.jpeg(cfg, self.runner)
        self.assertEqual((again, v2), (path, v))
        self.assertEqual(len(self.calls), 1)

    def test_none_choice_and_bad_choice(self):
        self.assertEqual(wallpaper.jpeg({"appearance": {"wallpaper": "none"}}, self.runner), (None, "none"))
        self.assertEqual(wallpaper.choice({"appearance": {"wallpaper": "../etc"}}), "desktop")
        self.assertEqual(wallpaper.choice({"appearance": {"wallpaper": "unknown-preset"}}), "desktop")

    def test_extra_wallpapers_from_config_with_tilde_expansion(self):
        pic_dir = self.tmp / "Pictures"
        pic_dir.mkdir()
        pic = pic_dir / "coast.png"
        pic.write_bytes(b"png")
        cfg = {
            "wallpaper_dir": str(self.tmp / "cache"),
            "appearance": {
                "wallpaper": "coast",
                "wallpapers": [
                    {"id": "coast", "label": "Coast", "path": "~/Pictures/coast.png"},
                    {"id": "missing", "label": "Missing", "path": "~/Pictures/missing.png"},
                    {"id": "desktop", "label": "Ignored", "path": "~/Pictures/coast.png"},
                ],
            },
        }
        with mock.patch.dict("os.environ", {"HOME": str(self.tmp)}):
            self.assertEqual(wallpaper.choice(cfg), "coast")
            self.assertEqual(wallpaper.source(cfg), ("coast", pic))
            opts = {o["id"]: o["label"] for o in wallpaper.info(cfg)["options"]}
            self.assertEqual(opts.get("coast"), "Coast")
            self.assertNotIn("missing", opts)
            self.assertEqual(opts.get("desktop"), "Follow my desktop")
            path, _ = wallpaper.jpeg(cfg, self.runner)
            self.assertTrue(path.is_file())

    def test_desktop_without_a_still_falls_back_to_a_photo(self):
        road = self.tmp / "Big Sur Road.heic"
        road.write_bytes(b"h")
        with mock.patch.object(wallpaper, "desktop_file", return_value=None), \
                mock.patch.dict(wallpaper.CHOICES, {"big-sur-road": ("Big Sur Road", (road,))}):
            self.assertEqual(wallpaper.source({}), ("big-sur-road", road))

    def test_info_lists_only_choices_on_this_mac(self):
        with mock.patch.dict(wallpaper.CHOICES, {"catalina-sunset": ("Catalina Sunset", (self.tmp / "nope.heic",))}):
            ids = [o["id"] for o in wallpaper.info({"appearance": {"wallpaper": "none"}})["options"]]
        self.assertIn("desktop", ids)
        self.assertIn("none", ids)
        self.assertNotIn("catalina-sunset", ids)


if __name__ == "__main__":
    unittest.main()
