"""The GUI's electron-builder references, and the reading that answers for them.

`scripts/check-gui-package-refs.py` runs in `build-release.yml` — i.e. only when a tag is pushed —
so without a test here nothing would exercise its logic on a pull request. It is the reader that
catches the fault a green build hides: electron-builder **warns** about a `from` that is not there
and publishes the artifact anyway (measured in run 37723424917, where `emrg/dist/runtime` and
`emrg/packaging/assets/icon.png` resolved inside `emrg/` and the AppImage shipped with no
`resources/runtime`). Each case below is driven with an explicit `--platform`, because the script's
default is this host's platform: a test that let the default decide would be asserting the
machine, not the script.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-gui-package-refs.py"


def _gui_project(tmp_path: Path, *, app_from: str = "../../dist/runtime", icon: str = "../../packaging/assets/icon.png") -> Path:
    """A GUI project dir shaped like emrg/gui, plus the repo-root products its refs name."""
    repo = tmp_path / "repo"
    gui = repo / "emrg" / "gui"
    gui.mkdir(parents=True)
    (repo / "dist" / "runtime" / "bin").mkdir(parents=True)
    (repo / "dist" / "runtime" / "bin" / "emrg").write_text("#!/bin/sh\n", encoding="utf-8")
    assets = repo / "packaging" / "assets"
    assets.mkdir(parents=True)
    (assets / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (assets / "icon.icns").write_bytes(b"icns")
    (assets / "icon.ico").write_bytes(b"\x00\x00\x01\x00")
    manifest = {
        "name": "emrg-gui",
        "build": {
            "appId": "com.emrg.gui",
            "extraResources": [{"from": app_from, "to": "runtime"}],
            "icon": "icon.png",
            "mac": {"icon": "icon.icns"},
            "win": {"icon": "icon.ico"},
            "linux": {"extraResources": [{"from": "../../dist/runtime", "to": "runtime"}], "icon": "icon.png"},
            "directories": {"buildResources": icon.rsplit("/", 1)[0]},
        },
    }
    (gui / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    return gui


def _run(gui: Path, platform: str = "linux") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(gui), "--platform", platform],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_every_reference_resolving_is_a_pass(tmp_path: Path) -> None:
    gui = _gui_project(tmp_path)
    done = _run(gui)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "OK: " in done.stdout
    # the tree it read is named before its verdict
    assert str(gui.resolve()) in done.stdout


def test_a_reference_one_level_short_is_a_fault(tmp_path: Path) -> None:
    """The measured shape: `../dist/runtime` resolves inside emrg/ while the product is at the root."""
    gui = _gui_project(tmp_path, app_from="../dist/runtime")
    done = _run(gui)
    assert done.returncode == 1, done.stdout + done.stderr
    # the reading names the place the short path lands (inside emrg/), not just "something is wrong"
    assert str((gui.parent / "dist" / "runtime").resolve()) in done.stdout
    assert "file source doesn't exist" in done.stdout  # the build's own words for the same fault


def test_a_platform_block_is_read_only_where_its_build_runs(tmp_path: Path) -> None:
    """The mac icons exist on macOS alone (iconutil), so a Linux reading must not fault on them."""
    gui = _gui_project(tmp_path)
    (gui.parent.parent / "packaging" / "assets" / "icon.icns").unlink()
    assert _run(gui, platform="linux").returncode == 0
    mac = _run(gui, platform="mac")
    assert mac.returncode == 1, mac.stdout + mac.stderr
    assert "mac.icon" in mac.stdout


def test_an_unreadable_manifest_is_not_a_pass(tmp_path: Path) -> None:
    gui = _gui_project(tmp_path)
    (gui / "package.json").write_text("{ not json", encoding="utf-8")
    done = _run(gui)
    assert done.returncode == 2, done.stdout + done.stderr
    assert "UNMEASURABLE" in done.stdout
