#!/usr/bin/env python3
"""Read back every path electron-builder resolves for the GUI, and say which of them is not there.

Usage
-----
    python3 scripts/check-gui-package-refs.py                    # this checkout's emrg/gui
    python3 scripts/check-gui-package-refs.py --root <GUIPROJ>   # another project dir
    python3 scripts/check-gui-package-refs.py --platform linux   # only the refs that build reads

Why this exists
---------------
electron-builder resolves every `from` in `extraResources`, and `directories.buildResources`,
relative to the **project dir** — the directory holding `package.json`, here `emrg/gui` — and a
source that is not there is a **warning, not a failure**. Measured on the v0.3.9 tag build
(`build-release.yml` run 37723424917, job 113136083119):

    • file source doesn't exist  from=/home/runner/work/emrg/emrg/emrg/dist/runtime
    • file source doesn't exist  from=/home/runner/work/emrg/emrg/emrg/packaging/assets/icon.png
    • file source doesn't exist  from=/home/runner/work/emrg/emrg/emrg/dist/runtime
    • default Electron icon is used  reason=application icon is not set

Build green, artifacts published, and the products are missing a payload: the resolution landed
inside `emrg/` while `packaging/build-runtime.sh` writes `dist/runtime` at the **repo root** and
`packaging/gen-assets.sh` writes `packaging/assets/` there too. The AppImage is the one that
hurts: `main.js`'s first-run install copies `process.resourcesPath/runtime` into
`~/.emrg/install` (`ensureAppImageExtracted`), so a Linux user's AppImage install has no runtime
to copy, while the same build job reports success.

The build cannot answer this question about itself — it is the warning it prints. The reading that
can is the paths' own resolution, against a tree where the products exist (in `build-release.yml`,
after the runtime and icon steps). Paths are checked against the platform whose block declares
them: the app-level entries everywhere, a `mac`/`win`/`linux` block only where the build reading it
runs — the mac icons are generated on macOS alone (`iconutil` is macOS-only in gen-assets.sh), so
an unconditional check would report a fault on the two legs that never read them.

Exit codes: 0 every reference resolves / 1 at least one does not / 2 could not measure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# sys.platform -> the package.json block whose build reads that platform's refs
PLATFORM_BLOCKS = {"darwin": "mac", "win32": "win", "linux": "linux"}


def _block(build: dict, block: str) -> dict:
    """The config block a ref lives in: "app" is the top level, a platform name is a nested block."""
    return build if block == "app" else (build.get(block) or {})


def _refs(build: dict, block: str) -> list[str]:
    """The `from` paths one config block declares (empty when the block has none)."""
    return [entry["from"] for entry in _block(build, block).get("extraResources", []) if "from" in entry]


def main(argv: list[str] | None = None) -> int:
    default_root = Path(__file__).resolve().parent.parent / "emrg" / "gui"
    ap = argparse.ArgumentParser(description="Resolve electron-builder's GUI path references and report missing sources.")
    ap.add_argument("--root", default=str(default_root), help="the GUI project dir (default: this checkout's emrg/gui)")
    ap.add_argument(
        "--platform",
        default=PLATFORM_BLOCKS.get(sys.platform, "linux"),
        choices=sorted(set(PLATFORM_BLOCKS.values()) | {"all"}),
        help="which platform's config block to read as well as the app-level entries (default: this host's)",
    )
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    manifest = root / "package.json"
    try:
        build = json.loads(manifest.read_text(encoding="utf-8"))["build"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"UNMEASURABLE: {manifest} - {exc}")
        return 2

    relative = (build.get("directories") or {}).get("buildResources")
    if not relative:
        print(f"UNMEASURABLE: {manifest} declares no directories.buildResources")
        return 2
    build_resources = (root / relative).resolve()

    wanted: list[tuple[str, Path]] = []
    blocks = ["app", "mac", "win", "linux"] if args.platform == "all" else ["app", args.platform]
    for block in blocks:
        prefix = "" if block == "app" else f"{block}."
        wanted += [
            (f"{prefix}extraResources[{i}].from", (root / from_).resolve())
            for i, from_ in enumerate(_refs(build, block))
        ]
        icon = _block(build, block).get("icon")
        if icon:
            wanted.append((f"{prefix}icon ({relative})", (build_resources / icon).resolve()))

    print(f"tree: {root} (platform block: {args.platform})")
    missing = []
    for label, path in wanted:
        if path.exists():
            print(f"  OK       {path}   [{label}]")
        else:
            missing.append((label, path))
            print(f"  MISSING  {path}   [{label}]")

    if missing:
        print(
            f"\n{len(missing)} of {len(wanted)} reference(s) do not resolve. electron-builder only *warns* about this\n"
            "and publishes an artifact without the payload, so this fault is invisible in a green build\n"
            "(its own words for the state: \"file source doesn't exist\" / \"default Electron icon is used\").\n"
            "The products: dist/runtime from packaging/build-runtime.sh, packaging/assets/icon.* from\n"
            "packaging/gen-assets.sh (both run earlier in build-release.yml); the paths in\n"
            f"{manifest} are relative to that file's own directory."
        )
        return 1

    print(f"\nOK: {len(wanted)} reference(s) resolve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
