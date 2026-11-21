"""Build and manage the macOS 'Jarviee.app' launcher bundle."""
import os
import plistlib
import shutil
import stat
import sys
from pathlib import Path

from hub import brand

ROOT = Path(__file__).resolve().parent.parent
APP_PATH = Path.home() / "Applications" / "Jarviee.app"
LEGACY_APP_NAMES = ("Agent Hub.app",)   # removed next to APP_PATH on install
DEFAULT_LOG_PATH = brand.home() / "logs" / "server.log"


def generate_info_plist():
    return {
        "CFBundleName": "Jarviee",
        "CFBundleDisplayName": "Jarviee",
        "CFBundleIdentifier": "dev.jarviee.app",
        "CFBundleVersion": "1.0",
        "CFBundleShortVersionString": "1.0",
        "CFBundlePackageType": "APPL",
        "CFBundleSignature": "????",
        "CFBundleExecutable": "jarviee-launcher",
        "CFBundleIconFile": "Jarviee",
        "LSMinimumSystemVersion": "12.0",
    }


def generate_launcher_script(cfg=None):
    jarviee_bin = str((ROOT / "bin" / "jarviee").resolve())
    return f"""#!/bin/bash
exec "{jarviee_bin}" "$@"
"""


def install(cfg=None, dry_run=False):
    """Create ~/Applications/Jarviee.app (and remove the old Agent Hub.app beside it)."""
    target = APP_PATH
    macos_dir = target / "Contents" / "MacOS"
    resources_dir = target / "Contents" / "Resources"
    plist_target = target / "Contents" / "Info.plist"
    launcher_target = macos_dir / "jarviee-launcher"
    icns_target = resources_dir / "Jarviee.icns"

    script_content = generate_launcher_script(cfg)
    plist_content = plistlib.dumps(generate_info_plist()).decode("utf-8")

    if dry_run:
        print(f"=== DRY RUN: {target} ===")
        print(f"Info.plist:\n{plist_content}")
        print(f"\njarviee-launcher:\n{script_content}")
        return True

    # 1. Prepare directory structure
    target.parent.mkdir(parents=True, exist_ok=True)
    macos_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(parents=True, exist_ok=True)

    # 2. Write Info.plist
    plist_target.write_text(plist_content, encoding="utf-8")

    # 3. Write launcher script with execute permissions
    launcher_target.write_text(script_content, encoding="utf-8")
    launcher_target.chmod(launcher_target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # 4. Generate & write app icon
    try:
        from hub.icon import build_icns
        build_icns(icns_target)
    except ImportError:
        print("Pillow is not installed; skipping custom app icon (Jarviee.app will use the generic icon).")
    except Exception as e:
        print(f"Warning: iconutil build failed ({e}), app will use generic icon", file=sys.stderr)

    for name in LEGACY_APP_NAMES:
        old = target.parent / name
        if old.is_dir() and (old / "Contents" / "Info.plist").is_file():
            shutil.rmtree(old)
            print(f"Removed old bundle: {old}")

    print(f"Created application bundle: {target}")
    return True


def uninstall():
    """Remove ~/Applications/Jarviee.app."""
    if APP_PATH.exists():
        if APP_PATH.is_dir():
            shutil.rmtree(APP_PATH)
        else:
            APP_PATH.unlink()
        print(f"Removed {APP_PATH}")
        return True
    print(f"App not found at {APP_PATH}")
    return False
