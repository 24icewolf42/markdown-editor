#!/usr/bin/env python3
"""Write a git-based Flatpak manifest for a pushed commit.

    packaging/flathub/generate.py OUTDIR --commit SHA [--tag vX.Y.Z]

The manifest is the local one (packaging/flatpak/) with the checkout replaced
by the git commit, so CI builds the pushed commit as Flathub's builders would.
OUTDIR receives the manifest and node-sources.json.
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_ID = "io.github._24icewolf42.markdown-editor"
REPO = "https://github.com/24icewolf42/markdown-editor.git"
SOURCE_BLOCK = re.compile(r"^( *)# BEGIN app-source\n.*?^ *# END app-source\n", re.S | re.M)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--commit", required=True)
    ap.add_argument("--tag")
    args = ap.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        ap.error("--commit must be a full 40-character commit hash")

    src = ROOT / "packaging" / "flatpak"
    manifest = (src / f"{APP_ID}.yml").read_text(encoding="utf-8")

    def git_source(m: re.Match) -> str:
        pad = m.group(1)
        lines = [f"{pad}- type: git", f"{pad}  url: {REPO}"]
        if args.tag:
            lines.append(f"{pad}  tag: {args.tag}")
        lines.append(f"{pad}  commit: {args.commit}")
        if args.tag:
            lines += [f"{pad}  x-checker-data:", f"{pad}    type: git", f"{pad}    tag-pattern: ^v([\\d.]+)$"]
        return "\n".join(lines) + "\n"

    manifest, n = SOURCE_BLOCK.subn(git_source, manifest)
    if n != 1:
        raise SystemExit("the local manifest has no single '# BEGIN app-source' block")
    # Comments about the local build do not apply to the git-based one.
    manifest = re.sub(r"\A(?:#.*\n)+", "", manifest)

    args.outdir.mkdir(parents=True, exist_ok=True)
    (args.outdir / f"{APP_ID}.yml").write_text(manifest, encoding="utf-8")
    shutil.copy(src / "node-sources.json", args.outdir / "node-sources.json")
    print(args.outdir / f"{APP_ID}.yml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
