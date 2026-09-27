"""Builds a Firefox XPI add-on package from an extension dist directory.

The launcher (Launch-AutonomousBrowser.ps1) invokes this instead of
Compress-Archive because XPI entry names must use forward slashes and
include manifest.json at the archive root.
"""

import argparse
import os
import sys
import zipfile


def build_xpi(source_dir, destination_path):
    source_dir = os.path.abspath(source_dir)
    if not os.path.isfile(os.path.join(source_dir, "manifest.json")):
        raise SystemExit(f"manifest.json not found under {source_dir}")
    parent = os.path.dirname(os.path.abspath(destination_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(destination_path):
        os.remove(destination_path)
    with zipfile.ZipFile(destination_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for root, dirs, files in os.walk(source_dir):
            dirs.sort()
            for name in sorted(files):
                file_path = os.path.join(root, name)
                arcname = os.path.relpath(file_path, source_dir).replace("\\", "/")
                archive.write(file_path, arcname)


def main():
    parser = argparse.ArgumentParser(description="Package a Firefox extension directory as an XPI.")
    parser.add_argument("source", help="Extension directory containing manifest.json")
    parser.add_argument("destination", help="Path of the XPI file to create")
    args = parser.parse_args()
    build_xpi(args.source, args.destination)
    print(f"Created {args.destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())