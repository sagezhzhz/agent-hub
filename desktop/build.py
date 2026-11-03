#!/usr/bin/env python3
"""Build a self-contained macOS app and optional drag-to-install DMG."""

import argparse
import hashlib
import importlib.metadata
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".build"
DIST = ROOT / "dist"
APP = DIST / "Agent Hub.app"
VERSION = "1.0.0"


def run(*args, **kwargs):
    print("Running:", " ".join(map(str, args)), flush=True)
    subprocess.run([str(a) for a in args], cwd=ROOT, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dmg", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("Build this application on macOS.")
    arch = platform.machine()
    BUILD.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    environment = os.environ.copy()
    environment["PYINSTALLER_CONFIG_DIR"] = str(BUILD / "pyinstaller-cache")
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "agent-hub-server", "--paths", ROOT,
        "--add-data", f"{ROOT / 'agent_hub.html'}:.",
        "--distpath", BUILD / "frozen", "--workpath", BUILD / "pyinstaller",
        "--specpath", BUILD / "spec", ROOT / "desktop/server_entry.py", env=environment)

    # Only replace generated build output, never an installed application.
    if APP.exists():
        shutil.rmtree(APP)
    contents = APP / "Contents"
    resources = contents / "Resources"
    executable = contents / "MacOS/AgentHub"
    executable.parent.mkdir(parents=True)
    resources.mkdir(parents=True)
    shutil.copytree(BUILD / "frozen/agent-hub-server", resources / "backend", symlinks=True)
    for source, target in [(ROOT / "desktop/AgentHub.swift", executable),
                           (ROOT / "agent_hub_island.swift", resources / "agent-hub-island"),
                           (ROOT / "desktop/MakeIcon.swift", BUILD / "make-icon")]:
        run("xcrun", "swiftc", "-O", "-target", f"{arch}-apple-macosx13.0",
            "-module-cache-path", BUILD / "swift-cache", source, "-o", target)
    iconset = BUILD / "AppIcon.iconset"
    run(BUILD / "make-icon", iconset)
    run("iconutil", "-c", "icns", iconset, "-o", resources / "AppIcon.icns")

    metadata = {
        "CFBundleName": "Agent Hub", "CFBundleDisplayName": "Agent Hub",
        "CFBundleExecutable": "AgentHub", "CFBundleIdentifier": "io.github.sagezhzhz.agent-hub.desktop",
        "CFBundlePackageType": "APPL", "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": VERSION, "CFBundleVersion": "1",
        "CFBundleIconFile": "AppIcon.icns", "LSMinimumSystemVersion": "13.0",
        "NSPrincipalClass": "NSApplication", "NSHighResolutionCapable": True,
        "NSSupportsAutomaticGraphicsSwitching": True,
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSHumanReadableCopyright": "Agent Hub contributors · Apache-2.0",
    }
    (contents / "Info.plist").write_bytes(plistlib.dumps(metadata))
    for name in ["LICENSE", "NOTICE"]:
        shutil.copy2(ROOT / name, resources / name)
    shutil.copy2(ROOT / "desktop/README.md", resources / "Desktop-README.md")
    licenses = resources / "ThirdPartyLicenses"
    licenses.mkdir()
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if not python_license.exists():
        raise SystemExit("The Python runtime license is missing; refusing to distribute it.")
    shutil.copy2(python_license, licenses / "Python-LICENSE.txt")
    distribution = importlib.metadata.distribution("pyinstaller")
    for item in distribution.files or []:
        if item.name == "COPYING.txt":
            shutil.copy2(distribution.locate_file(item), licenses / "PyInstaller-COPYING.txt")
    manifest = {"version": VERSION, "architecture": arch, "python": platform.python_version(),
                "pyinstaller": importlib.metadata.version("pyinstaller"),
                "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "desktop_changes": True, "signing": "ad-hoc, not notarized"}
    (resources / "BuildInfo.plist").write_bytes(plistlib.dumps(manifest))
    run("codesign", "--force", "--deep", "--sign", "-", APP)
    run("codesign", "--verify", "--deep", "--strict", "--verbose=2", APP)

    if args.dmg:
        stage = BUILD / "dmg-stage"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir()
        run("ditto", APP, stage / APP.name)
        (stage / "Applications").symlink_to("/Applications")
        (stage / "安装说明.txt").write_text(
            "Agent Hub 1.0.0\n\n将 Agent Hub 拖到 Applications 文件夹即可安装。\n"
            "以后从应用程序或 Spotlight 打开，无需终端、Python 或 Xcode。\n"
            "关闭主窗口或按 Command+Q 退出，后台服务和灵动岛也会停止。\n\n"
            f"适用：macOS 13 及以上，{'Apple Silicon（M 系列）' if arch == 'arm64' else 'Intel'} Mac。\n"
            "这是本地签名版本，未进行 Apple 公证。\n"
            "数据保存在 ~/Library/Application Support/Agent Hub。\n", encoding="utf-8")
        dmg = DIST / f"Agent-Hub-{VERSION}-macOS-{arch}.dmg"
        if dmg.exists():
            dmg.unlink()
        run("hdiutil", "create", "-volname", "Agent Hub", "-srcfolder", stage,
            "-ov", "-format", "UDZO", dmg)
        # Some macOS versions leave the image's creation device attached.
        # Detach only devices associated with this exact generated image.
        mounted = plistlib.loads(subprocess.check_output(["hdiutil", "info", "-plist"]))
        for image in mounted.get("images", []):
            if image.get("image-path") == str(dmg):
                devices = [entry["dev-entry"] for entry in image.get("system-entities", []) if "dev-entry" in entry]
                if devices:
                    run("hdiutil", "detach", devices[0])
        run("hdiutil", "verify", dmg)
        (DIST / (dmg.name + ".sha256")).write_text(hashlib.sha256(dmg.read_bytes()).hexdigest() + "  " + dmg.name + "\n")
        print("Installer:", dmg)
    print("Application:", APP)


if __name__ == "__main__":
    main()
