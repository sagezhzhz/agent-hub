#!/usr/bin/env python3
"""Write the Sparkle appcast for a notarized release DMG.

The installed app reads releases/latest/download/appcast.xml, so attach the file this
writes to the GitHub release together with the DMG. The DMG is signed with the EdDSA key
in this Mac's login Keychain (created once with Sparkle's generate_keys --account agent-hub).
"""

import argparse
from email.utils import formatdate
import html
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build import DIST, fetch_sparkle  # noqa: E402

REPO = "https://github.com/sagezhzhz/agent-hub"


def app_info(dmg):
    """Version, build number and minimum macOS of the app inside the DMG."""
    with tempfile.TemporaryDirectory() as mount:
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mount, str(dmg)],
                       check=True, stdout=subprocess.DEVNULL)
        try:
            info = plistlib.loads((Path(mount) / "Agent Hub.app/Contents/Info.plist").read_bytes())
        finally:
            subprocess.run(["hdiutil", "detach", mount], check=True, stdout=subprocess.DEVNULL)
    return info["CFBundleShortVersionString"], info["CFBundleVersion"], info["LSMinimumSystemVersion"]


def notes_html(path):
    """The release notes' bullet points ("- ..."), as the short list Sparkle shows in its update window."""
    if not path:
        return ""
    items = [line[2:].strip() for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- ")]
    return "<ul>" + "".join(f"<li>{html.escape(item)}</li>" for item in items) + "</ul>" if items else ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dmg", type=Path, help="the notarized, stapled release DMG")
    parser.add_argument("--notes", type=Path, help="release notes; their '- ' bullet lines are shown")
    parser.add_argument("--account", default="agent-hub", help="Keychain account of the signing key")
    parser.add_argument("--download-base", help="where the DMG is downloaded from (default: the version's GitHub release)")
    parser.add_argument("--output", type=Path, default=DIST / "appcast.xml")
    args = parser.parse_args()
    dmg = args.dmg.resolve()
    subprocess.run(["xcrun", "stapler", "validate", str(dmg)], check=True)
    version, build, minimum = app_info(dmg)
    signed = subprocess.run([str(fetch_sparkle() / "bin/sign_update"), "--account", args.account, str(dmg)],
                            check=True, capture_output=True, text=True).stdout
    signature = re.search(r'sparkle:edSignature="([^"]+)"', signed).group(1)
    length = re.search(r'length="(\d+)"', signed).group(1)
    base = (args.download_base or f"{REPO}/releases/download/v{version}").rstrip("/")
    notes = notes_html(args.notes)
    args.output.write_text(f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
  <channel>
    <title>Agent Hub</title>
    <link>{REPO}</link>
    <item>
      <title>Agent Hub {version}</title>
      <pubDate>{formatdate(usegmt=True)}</pubDate>
      <sparkle:version>{build}</sparkle:version>
      <sparkle:shortVersionString>{version}</sparkle:shortVersionString>
      <sparkle:minimumSystemVersion>{minimum}</sparkle:minimumSystemVersion>
      <sparkle:hardwareRequirements>arm64</sparkle:hardwareRequirements>
      <sparkle:fullReleaseNotesLink>{REPO}/releases/tag/v{version}</sparkle:fullReleaseNotesLink>{f'''
      <description><![CDATA[{notes}]]></description>''' if notes else ""}
      <enclosure url="{base}/{dmg.name}" length="{length}" type="application/octet-stream"
                 sparkle:edSignature="{signature}"/>
    </item>
  </channel>
</rss>
""", encoding="utf-8")
    print(f"Appcast for {version} (build {build}):", args.output)


if __name__ == "__main__":
    main()
