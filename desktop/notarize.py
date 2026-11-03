#!/usr/bin/env python3
"""Notarize a Developer ID-signed DMG using credentials kept in Keychain."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dmg", type=Path)
    parser.add_argument("--keychain-profile", required=True)
    parser.add_argument("--keychain", type=Path, help="Use the same explicit keychain as store-credentials")
    parser.add_argument("--submission-id", help="Resume a previous submission without uploading again")
    args = parser.parse_args()
    dmg = args.dmg.resolve()
    if not dmg.is_file() or dmg.suffix.lower() != ".dmg":
        parser.error("Provide an existing, signed DMG file.")
    subprocess.run(["codesign", "--verify", "--strict", str(dmg)], check=True)
    log_dir = dmg.parent.parent / ".build/notarization"
    log_dir.mkdir(parents=True, exist_ok=True)
    credentials = ["--keychain-profile", args.keychain_profile]
    if args.keychain:
        credentials.extend(["--keychain", str(args.keychain.expanduser().resolve())])
    if args.submission_id:
        submission_id = args.submission_id
    else:
        result = subprocess.run(["xcrun", "notarytool", "submit", str(dmg), *credentials,
                                 "--output-format", "json"], capture_output=True, text=True)
        if result.returncode:
            print(result.stderr, file=sys.stderr)
            raise SystemExit(result.returncode)
        submitted = json.loads(result.stdout)
        submission_id = submitted["id"]
        (log_dir / f"{dmg.stem}-submission.json").write_text(json.dumps(submitted, indent=2) + "\n")
    print("Notarization submission:", submission_id, flush=True)
    print("To resume: add --submission-id", submission_id, flush=True)
    result = subprocess.run(["xcrun", "notarytool", "wait", submission_id, *credentials,
                             "--output-format", "json"], capture_output=True, text=True)
    if result.returncode:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)
    status = json.loads(result.stdout)
    (log_dir / f"{dmg.stem}-status.json").write_text(json.dumps(status, indent=2) + "\n")
    subprocess.run(["xcrun", "notarytool", "log", submission_id, *credentials,
                    str(log_dir / f"{dmg.stem}-log.json")], check=True)
    if status.get("status") != "Accepted":
        raise SystemExit("Notarization not accepted; inspect the saved notary log before publishing.")
    subprocess.run(["xcrun", "stapler", "staple", str(dmg)], check=True)
    subprocess.run(["xcrun", "stapler", "validate", str(dmg)], check=True)
    checksum = hashlib.sha256(dmg.read_bytes()).hexdigest()
    Path(str(dmg) + ".sha256").write_text(checksum + "  " + dmg.name + "\n")
    print("Accepted, stapled and verified:", dmg)
    print("SHA-256:", checksum)


if __name__ == "__main__":
    main()
