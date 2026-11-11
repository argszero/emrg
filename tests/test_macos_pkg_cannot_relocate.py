"""A macOS package must not ask PackageKit to put a bundle somewhere else.

`install-location` states where a component package's payload goes. A `<relocate>`
element naming bundle identifiers overrides it: PackageKit asks LaunchServices for
each named bundle's current path and installs there instead. Measured on this host
2026-11-11 (cycle `cyc20261111-164717`) with a payload carrying two `.app` bundles:

* `pkgbuild --root <payload> --install-location '/.emrg/install'` printed
  `Inferring bundle components from contents of <payload>` and wrote
  `<relocate><bundle id="com.emrg.gui"/><bundle id="com.argszero.emrg.uninstall"/></relocate>`
  into the component PackageInfo -- while that same element carried
  `relocatable="false"`. The attribute is not a defence, and the packages that
  shipped this way installed the GUI into a local build directory for months.
* setting `BundleIsRelocatable` false in the property list `pkgbuild --analyze`
  writes, then passing it back as `--component-plist`, produced `<relocate/>`:
  the element stays, **empty**.

That second line is why the reading counts bundle identifiers inside the element
instead of asking whether the element exists. A guard phrased as "the package must
not carry `<relocate>`" rejects the one output that is correct and can never pass.

What is pinned here, in halves that fail independently:

1. the reading (`scripts/check-pkg-relocation.py`) fails a package that names a
   bundle, passes one that does not, and refuses to call an unreadable subject
   clean;
2. the packaging step arms the component plist before the component is built and
   reads the built package back afterwards, the release asks the same question of
   the artifact it publishes, and the postinstall no longer reports success over a
   bundle that never arrived.

Every assertion about a script's text is paired with the same predicate applied to
a doctored copy of that text, so a pattern that matches anything shows up here
rather than in a release.
"""

from __future__ import annotations

import importlib.util
import plistlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-pkg-relocation.py"
MAKE_INSTALLER = REPO_ROOT / "packaging" / "make-installer.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-release.yml"

RELOCATES = """<?xml version="1.0" encoding="utf-8"?>
<pkg-info overwrite-permissions="true" relocatable="false" identifier="com.argszero.emrg" version="0.3.48" format-version="2" install-location="/.emrg/install" auth="root">
    <payload numberOfFiles="12" installKBytes="6"/>
    <bundle id="com.emrg.gui" path="./emrg-gui/EMRG.app"/>
    <bundle id="com.argszero.emrg.uninstall" path="./emrg-gui/x.app"/>
    <relocate>
        <bundle id="com.emrg.gui"/>
        <bundle id="com.argszero.emrg.uninstall"/>
    </relocate>
</pkg-info>
"""

EMPTY_RELOCATE = """<?xml version="1.0" encoding="utf-8"?>
<pkg-info overwrite-permissions="true" relocatable="false" identifier="com.argszero.emrg" version="0.3.48" format-version="2" install-location="/.emrg/install" auth="root">
    <payload numberOfFiles="12" installKBytes="6"/>
    <bundle id="com.emrg.gui" path="./emrg-gui/EMRG.app"/>
    <relocate/>
</pkg-info>
"""

NO_RELOCATE = """<?xml version="1.0" encoding="utf-8"?>
<pkg-info overwrite-permissions="true" relocatable="false" identifier="com.argszero.emrg" version="0.3.48" format-version="2" install-location="/.emrg/install" auth="root">
    <payload numberOfFiles="12" installKBytes="6"/>
    <bundle id="com.emrg.gui" path="./emrg-gui/EMRG.app"/>
</pkg-info>
"""


def _tool():
    spec = importlib.util.spec_from_file_location("emrg_check_pkg_relocation", SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _component(tmp_path: Path, text: str) -> Path:
    """A build root shaped like the one pkgbuild is handed."""
    root = tmp_path / "payload"
    component = root / "EMRG-component.pkg"
    component.mkdir(parents=True)
    (component / "PackageInfo").write_text(text, encoding="utf-8")
    return root


# ---------------------------------------------------------------- the reading


def test_a_relocate_naming_bundles_is_a_fault_and_names_them(tmp_path, capsys):
    """The identifiers are the reading: they are what tells PackageKit where to go."""
    rc = _tool().main([str(_component(tmp_path, RELOCATES))])
    assert rc == 1, "a package that names two bundles inside <relocate> was passed"
    out = capsys.readouterr().out
    assert "RELOCATES" in out
    assert "com.emrg.gui" in out and "com.argszero.emrg.uninstall" in out


def test_relocatable_false_does_not_excuse_the_element(tmp_path):
    """"relocatable=false" sat beside the block in every package that shipped this bug."""
    tool = _tool()
    assert tool.relocated_bundles(RELOCATES) == ["com.emrg.gui", "com.argszero.emrg.uninstall"]
    assert 'relocatable="false"' in RELOCATES


def test_an_empty_relocate_is_clean(tmp_path, capsys):
    """Measured: arming the component plist leaves the element, empty. Not a fault."""
    rc = _tool().main([str(_component(tmp_path, EMPTY_RELOCATE))])
    assert rc == 0, "the output correct packaging produces was reported as a fault"
    assert "no bundle id sits inside a <relocate>" in capsys.readouterr().out


def test_a_package_with_no_relocate_at_all_is_clean(tmp_path):
    assert _tool().main([str(_component(tmp_path, NO_RELOCATE))]) == 0


def test_a_subject_holding_no_packageinfo_is_unmeasurable(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _tool().main([str(empty)]) == 2
    assert "holds no PackageInfo" in capsys.readouterr().out


def test_an_unparsable_packageinfo_is_unmeasurable(tmp_path, capsys):
    rc = _tool().main([str(_component(tmp_path, "<pkg-info><relocate>"))])
    assert rc == 2, "a package that could not be parsed was reported as clean"
    assert "unmeasurable" in capsys.readouterr().out


def test_a_pkg_that_cannot_be_expanded_here_is_unmeasurable(tmp_path, monkeypatch, capsys):
    """No pkgutil means the package was not read -- which is exit 2, never 0."""
    tool = _tool()
    monkeypatch.setattr(tool.shutil, "which", lambda name: None)
    pkg = tmp_path / "EMRG-9.9.9-macos-arm64.pkg"
    pkg.write_bytes(b"not really a package")
    assert tool.main([str(pkg)]) == 2
    assert "pkgutil" in capsys.readouterr().out


def test_a_file_this_tool_does_not_open_is_unmeasurable(tmp_path):
    stray = tmp_path / "notes.txt"
    stray.write_text("hello", encoding="utf-8")
    assert _tool().main([str(stray)]) == 2


# ---------------------------------------------------------------- the write mode


def _components_plist(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "components.plist"
    path.write_bytes(plistlib.dumps(entries))
    return path


def test_disarm_marks_every_bundle_non_relocatable(tmp_path, capsys):
    """pkgbuild --analyze infers true for each .app; that is the value to turn off."""
    plist = _components_plist(
        tmp_path,
        [
            {"BundleIsRelocatable": True, "RootRelativeBundlePath": "emrg-gui/EMRG.app"},
            {"BundleIsRelocatable": True, "RootRelativeBundlePath": "emrg-gui/x.app"},
        ],
    )
    assert _tool().main(["--disarm-component-plist", str(plist)]) == 0
    entries = plistlib.loads(plist.read_bytes())
    assert [entry["BundleIsRelocatable"] for entry in entries] == [False, False]
    assert entries[0]["RootRelativeBundlePath"] == "emrg-gui/EMRG.app", "the path is how pkgbuild finds it"
    out = capsys.readouterr().out
    assert "disarmed: emrg-gui/EMRG.app" in out and "2 changed" in out


def test_disarm_is_idempotent_and_says_so(tmp_path, capsys):
    plist = _components_plist(
        tmp_path, [{"BundleIsRelocatable": False, "RootRelativeBundlePath": "emrg-gui/EMRG.app"}]
    )
    assert _tool().main(["--disarm-component-plist", str(plist)]) == 0
    assert "already disarmed: emrg-gui/EMRG.app" in capsys.readouterr().out


def test_disarm_refuses_a_plist_it_cannot_read(tmp_path, capsys):
    plist = tmp_path / "components.plist"
    plist.write_text("this is not a plist", encoding="utf-8")
    assert _tool().main(["--disarm-component-plist", str(plist)]) == 2
    assert "unmeasurable" in capsys.readouterr().out


def test_disarm_refuses_an_entry_it_cannot_name(tmp_path, capsys):
    """A bundle it cannot identify is one it cannot claim to have disarmed."""
    plist = _components_plist(tmp_path, [{"BundleIsRelocatable": True}])
    assert _tool().main(["--disarm-component-plist", str(plist)]) == 2
    assert "RootRelativeBundlePath" in capsys.readouterr().out


# ------------------------------------------------- the packaging step and the release


def _installer_text() -> str:
    return MAKE_INSTALLER.read_text(encoding="utf-8")


def _component_plist_is_armed_before_the_build(text: str) -> bool:
    """analyze -> disarm -> pkgbuild --component-plist -> read the built component back."""
    analyze = text.find('pkgbuild --analyze --root "$PKG_ROOT/payload"')
    disarm = text.find("--disarm-component-plist")
    build = text.find('--component-plist "$PKG_ROOT/components.plist"')
    readback = text.find('check-pkg-relocation.py" "$PKG_ROOT/EMRG-component.pkg"')
    if -1 in (analyze, disarm, build, readback):
        return False
    return analyze < disarm < build < readback


def test_the_component_plist_is_armed_before_pkgbuild_and_read_back_after():
    text = _installer_text()
    assert _component_plist_is_armed_before_the_build(text), (
        "packaging/make-installer.sh no longer arms the component plist before building the "
        "component, or no longer reads the built component back"
    )
    doctored = text.replace(
        '    python3 "$ROOT/scripts/check-pkg-relocation.py" \\\n'
        '      --disarm-component-plist "$PKG_ROOT/components.plist"\n',
        "",
    )
    assert doctored != text, "the disarm step could not be located to doctor it"
    assert not _component_plist_is_armed_before_the_build(doctored), (
        "the predicate passes a script with the disarm step removed, so it measures nothing"
    )


def _postinstall_block(text: str) -> str | None:
    opened = text.find("cat > \"$PKG_ROOT/scripts/postinstall\" <<'EOF'")
    if opened == -1:
        return None
    start = text.find("\n", opened) + 1
    closed = text.find("\nEOF\n", start)
    return None if closed == -1 else text[start:closed]


def _postinstall_fails_when_the_bundle_is_missing(block: str) -> bool:
    guard = block.find('[ -n "$GUI_MISSING" ]')
    if guard == -1:
        return False
    failing = block.find("exit 1", guard)
    success = block.rfind("exit 0")
    return failing != -1 and success != -1 and failing < success


def test_the_postinstall_fails_instead_of_reporting_success_over_a_missing_bundle():
    block = _postinstall_block(_installer_text())
    assert block is not None, "the postinstall heredoc is gone from make-installer.sh"
    assert _postinstall_fails_when_the_bundle_is_missing(block), (
        "the postinstall still skips a missing ~/Applications copy and exits 0 -- an install "
        "that reports success over a GUI that never arrived is issue #2806's silent half"
    )
    doctored = block.replace('  exit 1\n', '')
    assert doctored != block, "the failing exit could not be located to doctor it"
    assert not _postinstall_fails_when_the_bundle_is_missing(doctored)


def _relocation_step() -> str | None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "- name: Assert the pkg cannot relocate its bundles (macOS only)"
    start = text.find(marker)
    if start == -1:
        return None
    nxt = text.find("\n      - name:", start + len(marker))
    return text[start:] if nxt == -1 else text[start:nxt]


def _step_asks_the_question(step: str) -> bool:
    """macOS only, reads the artifact this run built, and treats 2 as a failure too."""
    return (
        "if: runner.os == 'macOS'" in step
        and "check-pkg-relocation.py" in step
        and "uv run --no-sync python3" in step
        and '-name \'EMRG-*-macos-*.pkg\'' in step
        and '[ "$rc" -eq 2 ]' in step
    )


def test_the_release_asks_the_artifact_whether_it_can_relocate(capsys):
    """The package exists only on a tag build, so this is the only place the question can be asked."""
    step = _relocation_step()
    assert step is not None, (
        "build-release.yml no longer asserts the built pkg cannot relocate its bundles"
    )
    assert _step_asks_the_question(step)
    doctored = step.replace("check-pkg-relocation.py", "something-else.py")
    assert not _step_asks_the_question(doctored), (
        "the predicate passes a step that runs no reading, so it measures nothing"
    )
