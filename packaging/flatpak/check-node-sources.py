#!/usr/bin/env python3
"""Fail when node-sources.json misses a package from web-src/package-lock.json.

Regenerate with `make flatpak-node-sources` after changing the lockfile.
"""

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
lock = json.loads((ROOT / "web-src" / "package-lock.json").read_text(encoding="utf-8"))
sources = json.loads((ROOT / "packaging" / "flatpak" / "node-sources.json").read_text(encoding="utf-8"))
have = {s["sha512"] for s in sources if "sha512" in s}
missing = []
for path, pkg in lock.get("packages", {}).items():
    integrity = pkg.get("integrity", "")
    if not path or not integrity.startswith("sha512-"):
        continue
    if base64.b64decode(integrity[7:]).hex() not in have:
        missing.append(f"{path} {pkg.get('version', '')}")
if missing:
    print("node-sources.json is out of date; run `make flatpak-node-sources`. Missing:")
    print("\n".join(f"  {m}" for m in missing))
    sys.exit(1)
print(f"node-sources.json covers all {len(lock.get('packages', {})) - 1} packages")
