#!/usr/bin/env python3
"""Build a self-contained macOS app and optional drag-to-install DMG."""

import argparse
import hashlib
import importlib.metadata
import os
from pathlib import Path
import platform
import plistlib
import re
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


def sign_application(identity):
    if identity:
        # Sign nested native code first; the outer bundle seals those signatures.
        # All code uses the same Developer ID, retaining library validation.
        magic = {b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
                 b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
                 b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'}
        for path in sorted(APP.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_file() and not path.is_symlink():
                with path.open("rb") as stream:
                    native = stream.read(4) in magic
                if native and path != APP / "Contents/MacOS/AgentHub":
                    run("codesign", "--force", "--sign", identity, "--timestamp", "--options", "runtime", path)
        run("codesign", "--force", "--sign", identity, "--timestamp", "--options", "runtime", APP)
    else:
        run("codesign", "--force", "--deep", "--sign", "-", APP)
    run("codesign", "--verify", "--deep", "--strict", "--verbose=2", APP)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dmg", action="store_true")
    parser.add_argument("--version", default=VERSION)
    parser.add_argument("--build-number", default="1")
    parser.add_argument("--sign-identity", help="Developer ID Application identity name or SHA-1 fingerprint")
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version) or not re.fullmatch(r"\d+", args.build_number):
        parser.error("Use a three-part numeric version and an integer build number.")
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
        "CFBundleShortVersionString": args.version, "CFBundleVersion": args.build_number,
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
    manifest = {"version": args.version, "architecture": arch, "python": platform.python_version(),
                "pyinstaller": importlib.metadata.version("pyinstaller"),
                "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "desktop_changes": True,
                "signing": "Developer ID; verify notarization ticket separately" if args.sign_identity else "ad-hoc, not notarized"}
    (resources / "BuildInfo.plist").write_bytes(plistlib.dumps(manifest))
    sign_application(args.sign_identity)

    if args.dmg:
        stage = BUILD / "dmg-stage"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir()
        run("ditto", APP, stage / APP.name)
        (stage / "Applications").symlink_to("/Applications")
        (stage / "安装说明.txt").write_text(
            f"Agent Hub {args.version}\n\n将 Agent Hub 拖到 Applications 文件夹即可安装。\n"
            "以后从应用程序或 Spotlight 打开，无需终端、Python 或 Xcode。\n"
            "关闭主窗口或按 Command+Q 退出，后台服务和灵动岛也会停止。\n\n"
            f"适用：macOS 13 及以上，{'Apple Silicon（M 系列）' if arch == 'arm64' else 'Intel'} Mac。\n"
            + ("已使用 Developer ID 签名，Apple 公证状态请查看版本发布说明。\n" if args.sign_identity
               else "这是本地签名版本，未进行 Apple 公证。\n")
            + "数据保存在 ~/Library/Application Support/Agent Hub。\n", encoding="utf-8")
        dmg = DIST / f"Agent-Hub-{args.version}-macOS-{arch}.dmg"
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
        if args.sign_identity:
            run("codesign", "--force", "--sign", args.sign_identity, "--timestamp", dmg)
            run("codesign", "--verify", "--strict", dmg)
        (DIST / (dmg.name + ".sha256")).write_text(hashlib.sha256(dmg.read_bytes()).hexdigest() + "  " + dmg.name + "\n")
        print("Installer:", dmg)
    print("Application:", APP)


if __name__ == "__main__":
    main()
