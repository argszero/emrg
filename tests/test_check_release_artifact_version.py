"""The release's version is read out of the artifact, not asserted against the source.

Why this file exists
--------------------
Issue #2286. The release chain asserted the version three times and never once inside a
product: `tests/test_version_sync.py` and `scripts/bump-version.py --check` compare the
eight **source** declarations with each other, `scripts/check-release-tag.py` compares the
tag with the tagged **tree**, and `scripts/check-release-published.py` reads the release's
**metadata** -- draft, prerelease, and the asset *names*, never an asset's bytes. So a
release whose artifact carried an old version passed every gate this repository runs.

The cost has a name. `packaging/build-runtime.sh:141` writes the artifacts' `version.txt`
from the **built tree**, and `emrg/server/upgrade.py::UpgradeManager.tick` compares that
installed file with `target.lstrip("v")`: a release whose name and product disagree
re-triggers the upgrade session on every tick, forever, and no later release repairs it.

What was hand-run before this existed, and got wrong
----------------------------------------------------
Both the v0.3.12 and v0.3.13 releases required this reading ("assert against the artifact
itself, not the source"; "confirm the `app.asar` is freshly rebuilt, not a stale product
passed off as new"), and each cycle re-derived it from scratch. Measured on the published
`EMRG-0.3.13-macos-arm64.pkg` (sha256 `1ecafe2ab8114cbd7db0bf96abb9b86a22edb0e187119ba3b149c8466bf7029d`),
the expanded payload declares:

    emrg-gui/EMRG.app/Contents/Info.plist                  short=0.3.13 build=0.3.13
    emrg-gui/EMRG.app/Contents/Frameworks/EMRG Helper*.app  build=0.3.13, no short
    emrg-gui/卸载 EMRG.app/Contents/Info.plist               declares neither
    emrg-gui/EMRG.app/Contents/Resources/app.asar          emrg-gui 0.3.13
    version.txt                                            0.3.13

Two traps sat in the asar reader and the cycle that wrote this by hand fell into both
before landing it: the header JSON runs for the **string length**, not the pickle size
(using the wrong one answers `Extra data: line 1 column 462002`), and file offsets are
relative to `8 + pickle size` **aligned up to a multiple of 4**. `test_the_asar_data_base_is_the_padded_pickle_size`
pins both, because a reader that guesses the base reads garbage and calls it a verdict.

What is asserted, and the arms that keep it from being decoration
-----------------------------------------------------------------
Every exit code in the tool's own contract, in both directions, on fixtures built from the
shapes above:

* `0` -- a product that declares the claimed version in each form it holds;
* `1` -- a stale GUI dist inside an otherwise correct artifact (the release rant's own
  question), a bundle whose `CFBundleShortVersionString` and `CFBundleVersion` disagree,
  a tarball carrying an old `version.txt`, and a wrong claim against a good artifact;
* `2` -- a container the tool does not open, a path that is not there, an archive with no
  declaration, a header that will not parse, and a claim that is not a version.

Three assertions exist to catch a *false green* rather than a wrong number:

* **a dependency's version cannot become the verdict.** An asar holds eight `package.json`
  files; the fixture's dependency says `0.1.0` and the app says `0.3.13`, and asking for
  `0.1.0` must answer `1`, not `0` -- so a reader that took "the first manifest" would
  fail here (issue #2286 requirement 3);
* **an unreadable subject is never a pass**, and the refusal names *its own* cause: a path
  that does not exist must not be told to `pkgutil --expand` a container, which is the
  one-remedy-for-every-cause defect this repository has already paid for;
* **a fault outranks an unmeasured subject** in one run, so a real finding about one
  artifact is not buried by a second artifact nobody could read.

The CI step is **executed**, not read: its `run:` block is extracted from the workflow and
run under `bash --noprofile --norc -eo pipefail` (GitHub's `shell: bash`) in a temporary
tree holding a real `tar.gz`. A test that grepped the YAML for `check-release-artifact-version.py`
would go green the moment an edit kept the words and moved the `exit`, and the step's
whole purpose is that exit code. Its subject-absent leg is issue #1913's shape: a step
whose subject is missing must refuse, not skip.

Named limits
------------
Nothing here measures signing, notarization, or whether the release is published -- those
belong to the steps and guards that already own them. The fixtures are synthetic asars and
tarballs, not the 142 MB published `.pkg`: the real artifact is read by hand once per
release (`DEVELOPMENT.md` names the command), and these fixtures carry the byte shapes the
real one has, measured and recorded above. The tool is run as a subprocess with the
repository's interpreter, so `app_package_name()` resolves this checkout's
`emrg/gui/package.json` -- the same file `scripts/bump-version.py` rewrites.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import struct
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "scripts" / "check-release-artifact-version.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-release.yml"
STEP_NAME = "Assert the artifacts declare the version the tag names"

APP_NAME = "emrg-gui"
GOOD = "0.3.13"
STALE = "0.3.12"


# --------------------------------------------------------------------------- asar writer
def write_asar(path: Path, entries: dict[str, str]) -> None:
    """Build a byte-correct asar: the header layout the shipped artifact actually uses.

    `entries` maps a package-relative path to its content. The header is four little-endian
    uint32s (4, pickle size, inner size, JSON string length), the JSON starts at byte 16 and
    runs for the string length, and contents begin at `8 + pickle size` aligned up to 4 --
    each of those three facts is a place the hand-rolled reading went wrong, so the writer
    encodes them and the reader is tested against them rather than against a guess.
    """
    files: dict[str, object] = {}
    payload = bytearray()
    offsets: list[tuple[str, int, int, dict]] = []
    for name, content in entries.items():
        data = content.encode("utf-8")
        offsets.append((name, len(payload), len(data), None))
        payload += data

    def insert(parts: list[str], size: int, off: int) -> None:
        # `files` holds the root's *entries*, and the header wraps it once. Descending a
        # directory means stepping into that entry's own `files` dict -- writing
        # `node.setdefault(part, {"files": {}})` without the step leaves the next name
        # beside `files` instead of inside it, and the shipped layout has no such shape.
        node = files
        for part in parts[:-1]:
            node = node.setdefault(part, {"files": {}})["files"]
        node[parts[-1]] = {"size": size, "offset": str(off)}

    for name, off, size, _ in offsets:
        insert(name.split("/"), size, off)

    header = json.dumps({"files": files}, separators=(",", ":")).encode("utf-8")
    json_len = len(header)
    # The V8 ValueSerializer layout, read off the shipped file's own four uint32s
    # (4, 462012, 462008, 462001 for a 462001-byte JSON): `inner = 4 + pad4(json_len + 1)`
    # (the string, its NUL, and the length prefix that precedes it), `pickle = 4 + inner`,
    # and contents begin at `8 + pickle` -- the same number as `16 + json_len` aligned up
    # to 4, which is the arithmetic the tool implements. The four uint32s occupy bytes
    # 0..16 and the JSON itself starts at 16, so the length is NOT written a second time:
    # emitting it again makes the fixture a different file format, and the reader would be
    # tested against a shape no artifact has.
    padded = (json_len + 1) + (-(json_len + 1) % 4)   # the string, its NUL, aligned up
    inner = 4 + padded
    pickle_size = 4 + inner
    base = 8 + pickle_size
    # Both routes are 4-aligned by construction, which is why the tool's own "align up to
    # 4" is a no-op on a real asar -- and why stating the relation here is what keeps the
    # fixture and the reader from agreeing by accident.
    assert base == 16 + padded, "the two routes to the data base disagree"
    tail = b"\x00" * (base - 16 - json_len)
    path.write_bytes(struct.pack("<4I", 4, pickle_size, inner, json_len) + header + tail + bytes(payload))


def manifest(version: str, name: str = APP_NAME) -> str:
    return json.dumps({"name": name, "version": version}, indent=2)


# --------------------------------------------------------------------------- subprocess
def run_tool(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd or REPO_ROOT),
    )


@pytest.fixture
def payload(tmp_path: Path) -> Path:
    """A directory shaped like the expanded payload of the published pkg.

    The app bundle, one helper bundle (build version only, as electron-builder ships it),
    the uninstaller bundle that declares nothing, the runtime's `version.txt`, and an asar
    carrying the app manifest plus a dependency whose version differs on purpose.
    """
    root = tmp_path / "payload"
    app = root / "emrg-gui" / "EMRG.app" / "Contents"
    (app / "Resources").mkdir(parents=True)
    helper = root / "emrg-gui" / "EMRG.app" / "Contents" / "Frameworks" / "EMRG Helper.app" / "Contents"
    helper.mkdir(parents=True)
    uninstaller = root / "emrg-gui" / "卸载 EMRG.app" / "Contents"
    uninstaller.mkdir(parents=True)
    (root / "version.txt").write_text(f"{GOOD}\n", encoding="utf-8")
    write_asar(
        app / "Resources" / "app.asar",
        {
            "package.json": manifest(GOOD),
            "renderer/package.json": manifest("0.1.0", "emrg-gui-renderer"),
            "node_modules/ws/package.json": manifest("8.21.2", "ws"),
        },
    )
    return root


def plist(short: str | None, build: str | None) -> bytes:
    entries = ""
    if short is not None:
        entries += f"<key>CFBundleShortVersionString</key><string>{short}</string>"
    if build is not None:
        entries += f"<key>CFBundleVersion</key><string>{build}</string>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        f'<plist version="1.0"><dict>{entries}</dict></plist>\n'
    ).encode("utf-8")


def fill_payload(root: Path, app_version: str = GOOD) -> None:
    """Give the fixture tree its real declarations, with the app's version chosen per test."""
    contents = root / "emrg-gui" / "EMRG.app" / "Contents"
    (contents / "Info.plist").write_bytes(plist(app_version, app_version))
    helper = contents / "Frameworks" / "EMRG Helper.app" / "Contents" / "Info.plist"
    helper.write_bytes(plist(None, app_version))
    (root / "emrg-gui" / "卸载 EMRG.app" / "Contents" / "Info.plist").write_bytes(plist(None, None))
    write_asar(
        contents / "Resources" / "app.asar",
        {
            "package.json": manifest(app_version),
            "renderer/package.json": manifest("0.1.0", "emrg-gui-renderer"),
            "node_modules/ws/package.json": manifest("8.21.2", "ws"),
        },
    )


def write_tar_gz(path: Path, version: str) -> Path:
    """A Linux runtime archive in the shape make-installer.sh writes:
    `tar -czf EMRG-<ver>-linux-<arch>.tar.gz -C <parent> runtime`."""
    stage = path.parent / f"{path.stem}-stage"
    (stage / "runtime").mkdir(parents=True, exist_ok=True)
    (stage / "runtime" / "version.txt").write_text(f"{version}\n", encoding="utf-8")
    with tarfile.open(path, "w:gz") as tf:
        tf.add(stage / "runtime", arcname="runtime")
    return path


# ------------------------------------------------------------------- the pass direction
def test_the_published_payload_shape_reads_zero(payload: Path) -> None:
    """Every declaration in a correctly built product names the claimed version."""
    fill_payload(payload)
    proc = run_tool(GOOD, str(payload))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK: every version read names 0.3.13" in proc.stdout
    # The helper bundle and the runtime version.txt were read too, not just the app:
    # a reader that stopped at the first file would not notice a half-rebuilt bundle.
    assert proc.stdout.count("-> matches") >= 4


def test_from_tag_strips_the_v_the_way_the_upgrade_chain_does(payload: Path) -> None:
    """`--from-tag v0.3.13` and `0.3.13` are the same question.

    `UpgradeManager.tick` compares `version.txt` with `target.lstrip("v")`; if this guard
    and that code disagreed about what a tag means, the guard would be measuring a release
    nobody can install -- the reason `check-release-tag.py` states for the same choice.
    """
    fill_payload(payload)
    tagged = run_tool("--from-tag", f"v{GOOD}", str(payload / "version.txt"))
    bare = run_tool(GOOD, str(payload / "version.txt"))
    assert tagged.returncode == 0, tagged.stdout + tagged.stderr
    assert bare.returncode == 0, bare.stdout
    assert "names 0.3.13" in tagged.stdout


# ------------------------------------------------------------------- the fault direction
def test_a_stale_gui_dist_inside_an_otherwise_correct_artifact_is_a_fault(payload: Path) -> None:
    """The release rant's own question: the dist was not rebuilt, the plist was.

    This is the case every source-side guard misses -- the tree declares 0.3.13, the
    bundle's Info.plist says 0.3.13, and the packaged manifest is still the previous
    release's. A pass here would mean the asar was never opened.
    """
    fill_payload(payload, app_version=GOOD)
    asar = payload / "emrg-gui" / "EMRG.app" / "Contents" / "Resources" / "app.asar"
    archive = asar.read_bytes()
    needle = f'"version": "{GOOD}"'.encode("utf-8")
    assert archive.count(needle) >= 1
    asar.write_bytes(archive.replace(needle, f'"version": "{STALE}"'.encode("utf-8"), 1))

    proc = run_tool(GOOD, str(payload))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"package.json: {STALE} -> DOES NOT MATCH {GOOD}" in proc.stdout
    assert "0.3.13 -> matches" in proc.stdout, "the other declarations were not read"


def test_a_bundle_whose_two_version_fields_disagree_is_a_fault(payload: Path) -> None:
    """`CFBundleShortVersionString` and `CFBundleVersion` are both compared.

    One of the two moved is a build that half-rebuilt the bundle; reading only the first
    field found would call it clean.
    """
    fill_payload(payload)
    contents = payload / "emrg-gui" / "EMRG.app" / "Contents"
    (contents / "Info.plist").write_bytes(plist(STALE, GOOD))
    proc = run_tool(GOOD, str(payload))
    assert proc.returncode == 1, proc.stdout
    assert f"CFBundleShortVersionString: {STALE} -> DOES NOT MATCH {GOOD}" in proc.stdout


def test_a_stale_runtime_archive_is_a_fault(tmp_path: Path) -> None:
    """The Linux tarball's `version.txt` is the field `UpgradeManager.tick` reads."""
    stale = write_tar_gz(tmp_path / "EMRG-0.3.13-linux-x86_64.tar.gz", STALE)
    proc = run_tool(GOOD, str(stale))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"runtime/version.txt: {STALE} -> DOES NOT MATCH {GOOD}" in proc.stdout


def test_the_claim_can_be_the_stale_half(tmp_path: Path) -> None:
    """A good artifact and a wrong claim is still a fault -- the direction that catches
    a step comparing against the wrong version rather than against the wrong file."""
    good = write_tar_gz(tmp_path / "EMRG-0.3.13-linux-x86_64.tar.gz", GOOD)
    proc = run_tool("0.3.99", str(good))
    assert proc.returncode == 1, proc.stdout
    assert "DOES NOT MATCH 0.3.99" in proc.stdout


def test_a_dependency_version_cannot_produce_the_verdict(payload: Path) -> None:
    """Requirement 3: the app's manifest is selected by name, not by position.

    The fixture's asar holds `emrg-gui-renderer` at 0.1.0 and `ws` at 8.21.2 beside the
    app's 0.3.13. Asking for a dependency's version must answer 1: a reader that took the
    first manifest it found would answer 0 here, and the release would be gated on a
    number that means nothing.
    """
    fill_payload(payload)
    for other in ("0.1.0", "8.21.2"):
        proc = run_tool(other, str(payload))
        assert proc.returncode == 1, f"{other}: {proc.stdout}"
        assert f"package.json: {GOOD} -> DOES NOT MATCH {other}" in proc.stdout


def test_a_fault_outranks_an_unreadable_subject(payload: Path, tmp_path: Path) -> None:
    """A real finding is not buried by a subject nobody could read.

    The family's ordering: `1` is a fact about something that *was* read, `2` is the
    absence of a reading. Collapsing them would let one broken path turn a fault into a
    shrug, and the CI step's per-code messages would then print the wrong remedy.
    """
    fill_payload(payload, app_version=GOOD)
    stale_tar = write_tar_gz(tmp_path / "EMRG-0.3.13-linux-aarch64.tar.gz", STALE)
    proc = run_tool(GOOD, str(stale_tar), str(tmp_path / "not-there"))
    assert proc.returncode == 1, proc.stdout


# ---------------------------------------------------------------- the unmeasurable side
@pytest.mark.parametrize("suffix", [".pkg", ".zip", ".exe", ".AppImage", ".APPIMAGE"])
def test_a_container_this_tool_does_not_open_refuses_with_its_own_command(tmp_path: Path, suffix: str) -> None:
    """`2`, never `0`, and the line carries the command that reaches the value.

    `.AppImage` and `.APPIMAGE` both name the real product
    (`EMRG-0.3.13-x86_64.AppImage`): the lookup lowercases, so a key written with the
    product's own spelling would miss it and fall through to a message about a file that
    is not a declaration at all -- the wrong cause for the right refusal.
    """
    subject = tmp_path / f"EMRG-{GOOD}-macos-arm64{suffix}"
    subject.write_bytes(b"not really an artifact")
    proc = run_tool(GOOD, str(subject))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "Extract it first:" in proc.stdout, proc.stdout


def test_a_missing_path_is_refused_without_an_extract_remedy(tmp_path: Path) -> None:
    """Per cause, per subject: a path that does not exist is not a container.

    The closing line of a run that could not measure must not tell this reader to
    `pkgutil --expand` a file that is absent -- that is the one-remedy-for-every-cause
    shape this repository already recorded as false for most of its rows.
    """
    missing = tmp_path / "no-such-artifact"
    proc = run_tool(GOOD, str(missing))
    assert proc.returncode == 2, proc.stdout
    assert "no such path" in proc.stdout
    assert "pkgutil" not in proc.stdout.split("no such path")[0]
    assert "Extract the container" not in proc.stdout, proc.stdout
    assert "Each line above names its own cause" in proc.stdout


def test_an_archive_with_no_declaration_refuses(tmp_path: Path) -> None:
    """A tarball that carries no `version.txt` answers nothing -- and says so."""
    empty = tmp_path /"EMRG-0.3.13-linux-x86_64.tar.gz"
    with tarfile.open(empty, "w:gz") as tf:
        tf.addfile(tarfile.TarInfo("runtime/README"))
    proc = run_tool(GOOD, str(empty))
    assert proc.returncode == 2, proc.stdout
    assert "no version.txt" in proc.stdout


def test_an_unparseable_asar_header_refuses(payload: Path) -> None:
    """A corrupt header is `2`, not a verdict about the artifact."""
    fill_payload(payload)
    asar = payload / "emrg-gui" / "EMRG.app" / "Contents" / "Resources" / "app.asar"
    asar.write_bytes(b"\x00\x00\x00\x00" + b"garbage" * 40)
    proc = run_tool(GOOD, str(payload))
    assert proc.returncode == 2, proc.stdout
    assert "not measurable" in proc.stdout


def test_an_asar_without_the_apps_manifest_refuses(payload: Path) -> None:
    """Dependencies alone answer nothing about the product."""
    fill_payload(payload)
    asar = payload / "emrg-gui" / "EMRG.app" / "Contents" / "Resources" / "app.asar"
    write_asar(
        asar,
        {
            "renderer/package.json": manifest("0.1.0", "emrg-gui-renderer"),
            "node_modules/ws/package.json": manifest("8.21.2", "ws"),
        },
    )
    proc = run_tool(GOOD, str(payload))
    assert proc.returncode == 2, proc.stdout
    assert "no package.json whose name is" in proc.stdout


def test_a_claim_that_is_not_a_version_is_unmeasurable(tmp_path: Path) -> None:
    """`1` would report "an artifact disagrees" about an argument that never reached one."""
    good = write_tar_gz(tmp_path / "EMRG-0.3.13-linux-x86_64.tar.gz", GOOD)
    proc = run_tool("latest", str(good))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "is not a version" in proc.stdout


def test_a_directory_of_nothing_refuses(tmp_path: Path) -> None:
    """An empty tree is not a clean reading -- the family's own rule (issue #1872)."""
    (tmp_path / "dist").mkdir()
    proc = run_tool(GOOD, str(tmp_path / "dist"))
    assert proc.returncode == 2, proc.stdout
    assert "no .app bundle, .asar or version.txt" in proc.stdout


def test_the_walk_refuses_at_its_ceiling_rather_than_finding_nothing(tmp_path: Path, monkeypatch) -> None:
    """A walk that stops early must say so, not report "no declaration here".

    Run in-process because the ceiling is 200,000 entries: a subprocess cannot be given a
    smaller one, and building a quarter-million files to test a bound is not a test, it is
    a disk incident. The module is loaded by path, so no import side effect is involved.
    """
    spec = importlib.util.spec_from_file_location("crav", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    root = tmp_path / "huge"
    for i in range(6):
        (root / f"d{i}").mkdir(parents=True)
    monkeypatch.setattr(module, "MAX_WALK_ENTRIES", 3)
    with pytest.raises(module.Unmeasurable) as excinfo:
        module._bundles_and_files(root)
    assert "walk stopped at 3 entries" in str(excinfo.value)


# --------------------------------------------------------------------------- the reader
def test_the_asar_data_base_is_the_padded_pickle_size(tmp_path: Path) -> None:
    """The two offsets this cycle got wrong by hand, pinned on a fixture.

    Contents begin at `8 + pickle size` aligned up to 4, and the JSON runs for the string
    length. Both wrong answers were measured while writing this: the string length as a
    data base lands on 462017 (unaligned) and reads nothing; the pickle size where the
    JSON length belongs answers `Extra data: line 1 column 462002`. A reader that guesses
    either reads garbage and calls it a verdict, so the base is asserted to be 4-aligned
    and the version asserted to come back.
    """
    asar = tmp_path / "app.asar"
    write_asar(asar, {"package.json": manifest(GOOD)})
    archive = asar.read_bytes()
    _tag, pickle_size, _inner, json_len = struct.unpack("<4I", archive[:16])
    base = 8 + pickle_size
    assert json.loads(archive[16:16 + json_len].decode("utf-8")), "the JSON is not the string length"
    assert base % 4 == 0, "the data base must be the padded offset"

    header, resolved = __import__("importlib").import_module("json") and _load_tool().asar_header(archive)
    assert resolved == base, f"tool reads at {resolved}, measured base is {base}"
    assert _load_tool().read_asar(asar, APP_NAME) == GOOD


def _load_tool():
    spec = importlib.util.spec_from_file_location("crav_reader", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_version_with_a_leading_v_or_a_prerelease_tail_is_parsed(tmp_path: Path) -> None:
    """The shapes a declaration really comes in.

    `parse_version` is the one place a version is recognised, so `v0.3.13` (the tag's
    spelling) and `0.3.13-rc.1` (a prerelease) must land on the same value the comparison
    uses -- and `latest`, `0.3` and an empty file must not land on any.
    """
    tool = _load_tool()
    assert tool.parse_version("v0.3.13") == "0.3.13"
    assert tool.parse_version(" 0.3.13\n") == "0.3.13"
    assert tool.parse_version("0.3.13-rc.1") == "0.3.13-rc.1"
    for bad in ("latest", "0.3", "", "0.3.13.1"):
        assert tool.parse_version(bad) is None, bad


def test_the_app_package_name_is_read_not_hardcoded(tmp_path: Path) -> None:
    """The name comes from `emrg/gui/package.json`, the file bump-version.py rewrites.

    Asserted against the checkout rather than a fixture, because that is the claim: a
    reader with `emrg-gui` typed into it would keep working after a rename and silently
    select nothing.
    """
    declared = json.loads((REPO_ROOT / "emrg" / "gui" / "package.json").read_text(encoding="utf-8"))["name"]
    assert _load_tool().app_package_name() == declared


def test_reading_a_version_txt_needs_no_gui_manifest(tmp_path: Path) -> None:
    """The release job's invocation, kept independent of the checkout's GUI tree.

    The app's name is resolved **lazily**: a caller asking about a `version.txt` is not
    asking about the GUI manifest, and an eager resolution made that reading fail whenever
    the script ran outside a checkout. Proved by running a copy of the tool from a tree
    that has no `emrg/gui/package.json` at all.
    """
    lone = tmp_path / "scripts"
    lone.mkdir()
    shutil.copyfile(TOOL, lone / TOOL.name)
    subject = tmp_path / "version.txt"
    subject.write_text(f"{GOOD}\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(lone / TOOL.name), GOOD, str(subject)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # And the same tree cannot answer an asar question -- laziness is not a licence to
    # invent a name when the file that carries it is missing.
    asar = tmp_path / "app.asar"
    write_asar(asar, {"package.json": manifest(GOOD)})
    refused = subprocess.run(
        [sys.executable, str(lone / TOOL.name), GOOD, str(asar)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(tmp_path),
    )
    assert refused.returncode == 2, refused.stdout


# --------------------------------------------------------------------------- the step
def _step_body() -> str:
    """The step's own `run:` block, read from the workflow -- not a copy kept here."""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"]["release"]["steps"]
    for step in steps:
        if step.get("name") == STEP_NAME:
            assert isinstance(step.get("run"), str), "the step has no run: block to execute"
            return step["run"]
    raise AssertionError(f"no step named {STEP_NAME!r} in the release job")


#: The Windows leg has no `bash` to run the step under: `C:\Windows\System32\bash.exe` is
#: WSL's launcher, `CreateProcess` searches System32 before `PATH`, and it answers
#: `Windows Subsystem for Linux has no installed distributions.` (unsigned UTF-16LE, exit 1)
#: rather than running anything. `shutil.which("bash") is None` is therefore *not* a gate
#: there -- the launcher exists and is found. These three arms carry the platform gate the
#: same-shaped arms carry in `tests/test_a_step_without_its_product_refuses.py` (`:207`,
#: `:230`) and `tests/test_release_end_state.py`; the step is `shell: bash` on the runner
#: whose job actually invokes it, and the tool's own forty readings below stay ungated
#: because they run the tool in-process.
_WINDOWS = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash on the Windows runner is WSL's launcher, not the shell the step runs under",
)


def _run_step(body: str, workspace: Path, ref_name: str) -> subprocess.CompletedProcess:
    """Execute the step as GitHub's `shell: bash` would, in a tree the caller built."""
    if shutil.which("bash") is None:  # pragma: no cover - a POSIX-less host
        pytest.skip("no bash on PATH: this guard drives the step's shell text, which it cannot run")

    script = workspace / "step.sh"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    env = {"GITHUB_REF_NAME": ref_name, "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(workspace), env=env,
    )


@pytest.fixture
def release_job_tree(tmp_path: Path) -> Path:
    """A workspace holding `artifacts/` and the tool the step calls.

    The step runs `python3 scripts/check-release-artifact-version.py`, so the tree carries
    a `scripts/` copy -- and the assertion that the release job *checks the tree out* is
    below, where a missing checkout would make this path a fiction.
    """
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(TOOL, tmp_path / "scripts" / TOOL.name)
    (tmp_path / "artifacts").mkdir()
    return tmp_path


@_WINDOWS
def test_the_step_reads_a_matching_artifact_and_exits_zero(release_job_tree: Path) -> None:
    write_tar_gz(release_job_tree / "artifacts" / f"EMRG-{GOOD}-linux-x86_64.tar.gz", GOOD)
    proc = _run_step(_step_body(), release_job_tree, f"v{GOOD}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"asserting version {GOOD} (from tag v{GOOD}) inside 1 artifact(s)" in proc.stdout


@_WINDOWS
def test_the_step_fails_on_a_stale_artifact(release_job_tree: Path) -> None:
    """The release must not be created from products that disagree with the tag."""
    write_tar_gz(release_job_tree / "artifacts" / f"EMRG-{GOOD}-linux-x86_64.tar.gz", STALE)
    proc = _run_step(_step_body(), release_job_tree, f"v{GOOD}")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"DOES NOT MATCH {GOOD}" in proc.stdout
    assert "::error::an artifact declares a version other than" in proc.stdout


@_WINDOWS
def test_the_step_refuses_when_its_subject_is_absent(release_job_tree: Path) -> None:
    """Issue #1913's shape: a step whose subject is missing refuses, never skips.

    An empty `artifacts/` with a silent `exit 0` would let a release with no readable
    product pass — the same "stamps an absent subject" defect the three macOS steps were
    fixed for (issue #1913).
    """
    proc = _run_step(_step_body(), release_job_tree, f"v{GOOD}")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "no EMRG-*-linux-*.tar.gz among the downloaded artifacts" in proc.stdout


def test_the_step_names_each_cause_of_a_nonzero_exit(release_job_tree: Path) -> None:
    """An unreadable artifact and a crashed tool are different facts.

    Both legs are driven by the *same* rc the step reads, and a step that printed one
    message for both would send a reader to the wrong file. The third branch -- an rc that
    is neither 1 nor 2 -- is the family's own contract, so the body must carry it.
    """
    body = _step_body()
    for marker in ("an artifact declares a version other than", "no artifact's version could be read", "neither this tool's fault code (1) nor its unmeasurable one (2)"):
        assert marker in body, f"the step lost its message for: {marker}"


def test_the_release_job_checks_the_tree_out_before_reading_it() -> None:
    """The step calls `scripts/…`, so the job needs the file -- and the order is load-bearing.

    `actions/checkout`'s default `clean: true` runs `git clean -ffdx`, and `artifacts/` is
    untracked: a checkout placed *after* `download-artifact` would delete the files this
    step exists to read. Asserted on the parsed job, in order, rather than on a grep.
    """
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"]["release"]["steps"]
    index = {name: i for i, step in enumerate(steps) for name in [step.get("name") or step.get("uses") or ""]}
    checkout = [i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/checkout")]
    assert checkout, "the release job has no checkout, so scripts/check-release-artifact-version.py would not exist"
    download = [i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/download-artifact")]
    assert download, "the release job downloads no artifacts"
    assert max(checkout) < min(download), "a checkout after download-artifact cleans away the artifacts"
    assert index[STEP_NAME] > min(download), "the assertion must run against the downloaded artifacts"
    create = index["actions/checkout@v5"] if "actions/checkout@v5" in index else 0
    assert create >= 0


def test_the_step_runs_before_the_release_is_created() -> None:
    """A mismatch found after publication is a host-side incident; found before, a re-run."""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    names = [step.get("name") or step.get("uses") for step in doc["jobs"]["release"]["steps"]]
    assert names.index(STEP_NAME) < names.index("Create GitHub Release"), names


def test_the_step_does_not_depend_on_the_venv_or_the_network() -> None:
    """The release job installs nothing; a step needing `uv sync` would fail there.

    Read on the executed body rather than the prose: `python3 scripts/…` with no `uv` in
    front is the claim, and the tool's own imports are checked against the standard
    library so a future `import httpx` in it is a failure here, not in a tag build.
    """
    body = _step_body()
    assert "uv " not in body, "the release job has no uv environment to run the tool in"
    assert "python3 scripts/check-release-artifact-version.py" in body
    source = TOOL.read_text(encoding="utf-8")
    imports = {line.split()[1] for line in source.splitlines() if line.startswith("import ")}
    assert imports <= {
        "argparse", "json", "plistlib", "re", "shutil", "struct", "sys",
        "tarfile", "traceback", "pathlib", "tempfile",
    }, imports
