"""Tests for scripts/bump-version.py — the release version-bump tool.

Background (rant 2026-09-10T14:07:19, release v0.2.94)
-----------------------------------------------------
The release version lives in **8 declarations across 8 files**. Bumping it
by hand has caused at least three incidents:

* #408 — `emrg/gui/package.json` forgotten; the release had to be deleted
  and rebuilt.
* #1065 — `emrg/gui/package-lock.json` had no guard at all.
* v0.2.94 — a stray ``uv run`` rewrote every registry URL in ``uv.lock``
  (556 lines of mirror churn) around a 1-line version change.

``tests/test_version_sync.py`` only *detects* drift after the fact and
``tests/test_doc_counts.py`` guards the docs; neither can tell you what to
edit, and neither stops ``uv`` from churning the lockfile. These tests pin
the tool's behaviour in **both states** (#455 lesson — never infer a
discriminator from the failure case alone):

* negative — a consistent tree reports clean and ``bump`` is a no-op;
* positive — a single drifted source is reported, and ``bump`` repairs it.

Every test runs against a synthetic copy of the real 8-source layout under
``tmp_path``, so nothing here can touch the working tree or the network.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "bump-version.py"

# Every file that carries a version declaration, copied verbatim from the
# real repo so the anchors are exercised against the true formatting.
SOURCE_FILES = (
    "emrg/__init__.py",
    "pyproject.toml",
    "uv.lock",
    "emrg/gui/package.json",
    "emrg/gui/package-lock.json",
    "packaging/build-runtime.sh",
    "packaging/make-installer.sh",
    "packaging/make-run-installer.sh",
)


# The version a bump test moves *to*, and the version its doctoring tests
# write *away from*. Kept clear of the repo's own version and of every
# literal the tool touches — see test_version_literals_survive_the_next_release.
TARGET = "6.6.6"

_SEMVER = re.compile(r"\d+\.\d+\.\d+")


def _next_release(base: str) -> str:
    """The patch bump this repo will carry at its next release (v0.2.x cadence)."""
    major, minor, patch = (int(part) for part in base.split("."))
    return f"{major}.{minor}.{patch + 1}"


def _sentinel(base: str) -> str:
    """A version that is safe to stamp into a synthetic source.

    Every doctoring test writes a different version into one source and then
    asserts ``check()`` reports exactly that drift — so the stamped literal must
    differ from the tree's own version. Measured mechanism (not assumed):

        consistent tree at 0.2.94, stamp "0.0.1" → 1 drift reported
        consistent tree at 0.0.1,  stamp "0.0.1" → 0 drifts (no-op edit)
        consistent tree at 1.1.1,  stamp "1.1.1" → 0 drifts (no-op edit)

    ``str.replace(old, new)`` is a no-op when old == new, so a coinciding
    literal silently creates no drift and the test fails while looking like a
    tool bug. Deriving the value from ``base`` makes that impossible rather than
    leaving it to the author to notice: the components are strictly above
    ``base``'s, so the result can neither equal nor be a substring of a version
    this repo carries (the cadence moves through patch numbers, and this suite
    never bumps past a major of ``base.major + 2``).
    """
    major, minor, patch = (int(part) for part in base.split("."))
    candidate = f"{major + 2}.{minor + 1}.{patch + 1}"
    assert _SEMVER.findall(candidate) == [candidate], candidate
    return candidate


def _load_module():
    spec = importlib.util.spec_from_file_location("bump_version", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


@pytest.fixture(autouse=True)
def _pristine_sources():
    """Fail loudly if a CLI test rewrites the real repo's version sources.

    Regression guard for a bug found while writing this file: ``main()`` bound
    ``REPO_ROOT`` as a *default argument*, evaluated once at import time, so
    ``monkeypatch.setattr(mod, "REPO_ROOT", fake_repo)`` had no effect and the
    CLI tests bumped ``emrg/__init__.py``, ``pyproject.toml`` and
    ``emrg/gui/package.json`` in the working tree to a sentinel version.

    Snapshot-and-compare is used instead of the real ``REPO_ROOT`` being
    patched, because the whole point is to detect a test that escapes tmp_path.
    A stale snapshot from an unrelated concurrent edit is indistinguishable
    from pollution, so the message says to check ``git status``.
    """
    before = {rel: (REPO_ROOT / rel).read_bytes() for rel in SOURCE_FILES}
    yield
    after = {rel: (REPO_ROOT / rel).read_bytes() for rel in SOURCE_FILES}
    changed = sorted(rel for rel in SOURCE_FILES if before[rel] != after[rel])
    assert not changed, (
        f"test polluted the real repo's version sources: {changed}. "
        "Restore with `git checkout -- "
        + " ".join(changed)
        + "` and check `git status` for unrelated causes."
    )


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A copy of just the version-bearing files, with the real formatting."""
    for rel in SOURCE_FILES:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, dst)
    return tmp_path


# --------------------------------------------------------------------------
# source table integrity — the tool must not silently stop covering a source
# --------------------------------------------------------------------------


def test_covers_every_documented_version_source(mod):
    """The tool's table must list exactly the files the release process names."""
    assert {p for p, _, _ in mod.VERSION_SOURCES} == set(SOURCE_FILES)
    assert mod.FILE_COUNT == 8


def test_version_literals_survive_the_next_release(mod, fake_repo):
    """This file must keep passing after the repo's own next release.

    Regression guard for a defect that reached review: the ``--check`` CLI test
    asserted the literal ``0.2.93`` — true when written, false within the hour,
    because the branch was merged during a release. Master (0.2.94) then ran
    with a permanently red suite (1 failed, 21 passed), reproduced by merging
    the branch head into master in a throwaway worktree.

    Rather than chase individual literals, this simulates the next release: it
    predicts the version the repo will actually carry, bumps the synthetic tree
    to it, and requires every literal this file manipulates to stay distinct
    from both the current version and that prediction.

    Measured failure modes being prevented (both verified on real trees):

    * a **doctoring stamp that equals the tree's version** — ``str.replace`` is
      then a no-op, no drift is created, and the test fails as though the tool
      had missed one (0 drifts reported on a tree stamped with its own version);
    * a **bump target that equals the tree's version** — ``bump()`` returns
      early ("already at ..."), so every test asserting a change fails.
    """
    base = mod.read_current_version(fake_repo)
    next_release = _next_release(base)

    hint = " — pick a different value for it in this file"
    for name, literal in {"TARGET": TARGET, "sentinel": _sentinel(base)}.items():
        assert literal != base, (
            f"{name}={literal!r} equals the version under test {base!r}: "
            f"str.replace would be a no-op, so the test would assert a change "
            f"that never happened{hint}"
        )
        assert literal not in base and base not in literal, (
            f"{name}={literal!r} is a substring of the version under test "
            f"{base!r}{hint}"
        )
        assert literal != next_release, (
            f"{name}={literal!r} equals the next release {next_release!r}, "
            f"which this file itself bumps the tree to{hint}"
        )
        for rel, pattern, _count in mod.VERSION_SOURCES:
            text = (fake_repo / rel).read_text(encoding="utf-8")
            for declared in mod._find_versions(text, pattern):
                assert literal != declared, (
                    f"{name}={literal!r} equals {rel}'s declared {declared!r}{hint}"
                )

    # And the file's own machinery must survive a real bump to that release.
    assert mod.bump(next_release, root=fake_repo) != []
    assert mod.check(root=fake_repo) == []


def test_package_lock_declares_two_occurrences(mod):
    """package-lock.json carries root + packages[""] — and both must be bumped.

    #1065's whole point: the two occurrences are not adjacent, so a naive
    single-shot replace silently leaves the second one stale.
    """
    entry = next(e for e in mod.VERSION_SOURCES if e[0] == "emrg/gui/package-lock.json")
    assert entry[2] == 2


def test_anchors_never_match_dependency_versions(mod, fake_repo):
    """Each anchor must match only app-version declarations, never a dependency.

    package-lock.json has 300+ ``"version"`` fields; pyproject.toml has
    dependency pins. If an anchor were too loose, the count assertion would
    fire — this pins the counts that make that guarantee real.
    """
    for rel, pattern, count in mod.VERSION_SOURCES:
        text = (fake_repo / rel).read_text(encoding="utf-8")
        assert len(mod._find_versions(text, pattern)) == count, rel


# --------------------------------------------------------------------------
# negative state — a consistent tree is clean and bump() is a no-op
# --------------------------------------------------------------------------


def test_check_clean_on_consistent_tree(mod, fake_repo):
    assert mod.check(root=fake_repo) == []


def test_bump_is_noop_when_already_at_target(mod, fake_repo):
    current = mod.read_current_version(fake_repo)
    before = {
        rel: (fake_repo / rel).read_text(encoding="utf-8") for rel in SOURCE_FILES
    }
    assert mod.bump(current, root=fake_repo) == []
    after = {rel: (fake_repo / rel).read_text(encoding="utf-8") for rel in SOURCE_FILES}
    assert before == after, "a no-op bump must not rewrite files"


def test_bump_refuses_noop_when_another_source_is_drifted(mod, fake_repo):
    """A dropped target must not be reported as success while drift remains.

    #1119 review (pm25coder, verified independently): the already-at-target
    check ran *before* validation, so this exact tree — ``emrg/__init__.py``
    already at the target version, another source stale — printed "already at
    <target> — nothing to do" and exited 0, leaving the drift in place. The
    repair mode silently disagreed with ``--check``, which names the same
    drift and exits 1 for it. That is precisely the #408 / #1065 shape this
    tool exists to prevent, and the natural way the mistake is made: hand-edit
    the base file, then run the tool.

    Correct behaviour is to refuse loudly (exit 2 for the CLI), never to
    report success for a state that was not verified.
    """
    current = mod.read_current_version(fake_repo)
    drifted = fake_repo / "emrg/gui/package.json"
    drifted.write_text(
        drifted.read_text(encoding="utf-8").replace(current, _sentinel(current)),
        encoding="utf-8",
    )
    before = {rel: (fake_repo / rel).read_text(encoding="utf-8") for rel in SOURCE_FILES}

    with pytest.raises(mod.BumpError, match="already inconsistent"):
        mod.bump(current, root=fake_repo)

    # ...and it must refuse without half-writing anything.
    after = {rel: (fake_repo / rel).read_text(encoding="utf-8") for rel in SOURCE_FILES}
    assert before == after, "a refused bump must not modify any source"

    # A consistent tree at the target still short-circuits as a no-op, so the
    # ordering change tightened the drifted case without losing the clean one.
    drifted.write_text(
        drifted.read_text(encoding="utf-8").replace(_sentinel(current), current),
        encoding="utf-8",
    )
    assert mod.bump(current, root=fake_repo) == []


def test_bump_refuses_when_base_file_was_hand_edited_forward(mod, fake_repo):
    """Sibling shape: the *base* file is the one moved ahead of the others.

    ``emrg/__init__.py`` reads as authoritative, so editing it to the target and
    running ``bump <target>`` is the other natural way to reach a drifted tree.
    Same contract: refuse, do not report "nothing to do".
    """
    current = mod.read_current_version(fake_repo)
    target = _sentinel(current)
    base_file = fake_repo / "emrg/__init__.py"
    base_file.write_text(
        base_file.read_text(encoding="utf-8").replace(current, target), encoding="utf-8"
    )

    with pytest.raises(mod.BumpError, match="already inconsistent"):
        mod.bump(target, root=fake_repo)


# --------------------------------------------------------------------------
# positive state — drift is reported, and bump() repairs it everywhere
# --------------------------------------------------------------------------


def test_check_reports_a_single_drifted_source(mod, fake_repo):
    """One stale source must be named — this is the #408 failure mode."""
    target = fake_repo / "emrg/gui/package.json"
    base = mod.read_current_version(fake_repo)
    target.write_text(
        target.read_text(encoding="utf-8").replace(base, _sentinel(base)),
        encoding="utf-8",
    )
    problems = mod.check(root=fake_repo)
    assert len(problems) == 1
    assert "emrg/gui/package.json" in problems[0]


def test_check_reports_drift_in_the_non_adjacent_lock_occurrence(mod, fake_repo):
    """Only the second lock occurrence stale → still caught (#1065 mode).

    The half-miss is created by **anchor**, not by position. Taking the last
    textual occurrence of ``base`` looks equivalent but is not: the lockfile
    holds 300+ dependency versions, and a base such as ``1.0.0`` appears inside
    them, so ``rpartition`` would doctor a dependency and leave the app's
    ``packages[""]`` entry intact — the test would then inspect a tree where
    nothing is stale and fail for the wrong reason. Same class of mistake as
    the loose anchor this PR fixes, so it is fixed here the same way.
    """
    lock = fake_repo / "emrg/gui/package-lock.json"
    base = mod.read_current_version(fake_repo)
    stamp = _sentinel(base)
    text = lock.read_text(encoding="utf-8")

    pattern = re.compile(r'("name": "emrg-gui",\s*\n\s*"version": ")[^"]+(")')
    matches = list(pattern.finditer(text))
    assert len(matches) == 2, "expected root + packages[''] declarations"
    last = matches[1]
    lock.write_text(
        text[: last.start()] + last.group(1) + stamp + last.group(2) + text[last.end() :],
        encoding="utf-8",
    )

    # The root declaration must still be correct — otherwise this is a
    # full miss, and the test would not be exercising the reported gap.
    assert text[: last.start()].count(f'"version": "{base}"') >= 1

    problems = mod.check(root=fake_repo)
    assert len(problems) == 1
    assert "package-lock.json" in problems[0]


def test_bump_updates_every_source_and_leaves_them_consistent(mod, fake_repo):
    changed = mod.bump(TARGET, root=fake_repo)
    assert set(changed) == set(SOURCE_FILES)
    assert mod.check(root=fake_repo) == []
    assert mod.read_current_version(fake_repo) == TARGET


def test_bump_preserves_lockfile_structure_and_dependency_versions(mod, fake_repo):
    """Only the two app-version literals change; 300+ dependency lines must not.

    This is the v0.2.94 uv-churn lesson in miniature: a bump must be a
    surgical literal swap, never a structural rewrite.
    """
    lock = fake_repo / "emrg/gui/package-lock.json"
    before = lock.read_text(encoding="utf-8").splitlines()
    current = mod.read_current_version(fake_repo)

    mod.bump(TARGET, root=fake_repo)

    after = lock.read_text(encoding="utf-8").splitlines()
    assert len(before) == len(after), "line count must not change"
    diff = [(b, a) for b, a in zip(before, after) if b != a]
    assert len(diff) == 2, f"expected exactly 2 changed lines, got {len(diff)}: {diff}"
    for b, a in diff:
        assert b.replace(current, TARGET) == a


def test_bump_preserves_shell_fallback_quoting(mod, fake_repo):
    """build-runtime.sh quotes its fallback; the others do not — keep both."""
    quoted = fake_repo / "packaging/build-runtime.sh"
    unquoted = fake_repo / "packaging/make-installer.sh"
    mod.bump(TARGET, root=fake_repo)
    assert f'|| echo "{TARGET}"' in quoted.read_text(encoding="utf-8")
    assert f"|| echo {TARGET})" in unquoted.read_text(encoding="utf-8")


def test_uv_lock_changes_only_the_emrg_version_line(mod, fake_repo):
    lock = fake_repo / "uv.lock"
    before = lock.read_text(encoding="utf-8")
    mod.bump(TARGET, root=fake_repo)
    after = lock.read_text(encoding="utf-8")
    assert f'name = "emrg"\nversion = "{TARGET}"' in after
    # exactly one line differs
    b_lines, a_lines = before.splitlines(), after.splitlines()
    assert len([1 for b, a in zip(b_lines, a_lines) if b != a]) == 1


# --------------------------------------------------------------------------
# fail-loud contracts — never guess, never half-write
# --------------------------------------------------------------------------


def test_bump_refuses_when_sources_are_already_inconsistent(mod, fake_repo):
    """A pre-drifted tree must abort instead of propagating the wrong version.

    ⚠️ The stamp must differ from the tree's own version: ``replace(old, new)``
    is a no-op when they coincide, so the drift this test exists to create would
    silently not appear (measured: 0 drifts reported on a tree stamped with its
    own version). ``_sentinel`` derives a value that cannot coincide.
    """
    target = fake_repo / "pyproject.toml"
    stamp = _sentinel(mod.read_current_version(fake_repo))
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            mod.read_current_version(fake_repo), stamp
        ),
        encoding="utf-8",
    )
    with pytest.raises(mod.BumpError, match="already inconsistent"):
        mod.bump(TARGET, root=fake_repo)
    # and nothing was written
    assert stamp in target.read_text(encoding="utf-8")


def test_bump_rejects_non_semver(mod, fake_repo):
    for bad in ("0.2", "v0.2.94", "0.2.94-rc1", ""):
        with pytest.raises(mod.BumpError, match="semver"):
            mod.bump(bad, root=fake_repo)


def test_bump_raises_when_an_anchor_disappears(mod, fake_repo):
    """If a file is reformatted, fail loud rather than skip the source."""
    (fake_repo / "packaging/make-installer.sh").write_text(
        "#!/bin/sh\nVERSION=0.2.93\n", encoding="utf-8"
    )
    with pytest.raises(mod.BumpError, match="anchor"):
        mod.bump(TARGET, root=fake_repo)


def test_anchor_missing_report_is_non_destructive(mod, fake_repo):
    (fake_repo / "uv.lock").write_text("nothing here\n", encoding="utf-8")
    problems = mod.check(root=fake_repo)
    assert any("uv.lock" in p for p in problems)


# --------------------------------------------------------------------------
# CLI surface
# --------------------------------------------------------------------------


def test_cli_check_succeeds_on_the_real_repo(mod):
    """The shipped repo must be consistent — the release gate, runnable by hand."""
    assert mod.main(["--check"]) == 0


def test_cli_dry_run_writes_nothing(mod, fake_repo, monkeypatch, capsys):
    monkeypatch.setattr(mod, "REPO_ROOT", fake_repo)
    before = {
        rel: (fake_repo / rel).read_text(encoding="utf-8") for rel in SOURCE_FILES
    }
    assert mod.main([TARGET, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    after = {rel: (fake_repo / rel).read_text(encoding="utf-8") for rel in SOURCE_FILES}
    assert before == after


def test_cli_bump_then_check_round_trips(mod, fake_repo, monkeypatch, capsys):
    monkeypatch.setattr(mod, "REPO_ROOT", fake_repo)
    assert mod.main([TARGET]) == 0
    assert TARGET in capsys.readouterr().out
    assert mod.main(["--check"]) == 0


def test_cli_check_against_an_explicit_version(mod, fake_repo, monkeypatch, capsys):
    monkeypatch.setattr(mod, "REPO_ROOT", fake_repo)
    assert mod.main(["--check", TARGET]) == 1
    assert "drift" in capsys.readouterr().out


# --------------------------------------------------------------------------
# --check positional validation (#1119 review, how2how2how2-arch)
# --------------------------------------------------------------------------


def test_cli_check_rejects_tag_style_version(mod, fake_repo, monkeypatch, capsys):
    """`--check v0.2.94` must error, not silently ignore the argument.

    Release tags are vX.Y.Z, so the leading `v` is a natural slip. Previously
    it fell through to "compare against emrg/__init__.py", discarding the
    argument and exiting 0 with a green line about a version the caller never
    named — a false OK on the one command whose whole job is to gate a release.
    """
    monkeypatch.setattr(mod, "REPO_ROOT", fake_repo)
    rc = mod.main(["--check", "v0.2.94"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not a semver" in err
    assert "0.2.94" in err, "should hint at the corrected form"


def test_cli_check_rejects_other_non_semver(mod, fake_repo, monkeypatch, capsys):
    """Every non-semver positional is rejected, including an explicit ``""``.

    ``""`` is the subtle one: validating presence with a truthiness test would
    send it down the no-argument branch, printing a green line about a version
    the caller never named — the original "silently reinterpret the input"
    defect reintroduced through a different spelling.
    """
    monkeypatch.setattr(mod, "REPO_ROOT", fake_repo)
    for bad in ("banana", "0.2", "0.2.94-rc1", ""):
        assert mod.main(["--check", bad]) == 2, bad
        capsys.readouterr()


def test_cli_check_without_positional_still_uses_the_base_version(
    mod, fake_repo, monkeypatch, capsys
):
    monkeypatch.setattr(mod, "REPO_ROOT", fake_repo)
    assert mod.main(["--check"]) == 0
    assert mod.read_current_version(fake_repo) in capsys.readouterr().out


# --------------------------------------------------------------------------
# host-codec safety — the self-check a host runs before pushing a release
# --------------------------------------------------------------------------


def _tool_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Materialise the tool *and* its sources under ``tmp_path``.

    The tool resolves ``REPO_ROOT`` from the checkout the caller is standing in
    (``cwd``), falling back to ``__file__``'s directory - so a subprocess run
    needs its own copy of the script placed next to its own sources *and* must
    use that directory as ``cwd``. Patching the module attribute - the
    in-process tests' route - cannot reach a child process.

    Measured 2026-09-11 (see ``test_the_tree_is_the_checkout_you_are_standing_in``):
    before that fix the root came from ``__file__`` alone, so running the main
    checkout's copy from inside a worktree gated the wrong tree entirely and
    ``bump()`` rewrote it.
    """
    tool = tmp_path / "scripts" / "bump-version.py"
    tool.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SCRIPT, tool)
    for rel in SOURCE_FILES:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, dst)
    return tool, tmp_path


def _run_tool(
    tool: Path, args: list[str], cwd: Path, codec: str
) -> subprocess.CompletedProcess[bytes]:
    """Run the CLI as a *subprocess* with a non-UTF-8 stdout codec.

    Raw byte capture (``capture_output=True`` without ``text=True``) is
    deliberate: a text-mode capture makes the *parent* decode the child's bytes,
    which hides the defect — the child's own traceback is what a host sees.
    """
    env = dict(os.environ, PYTHONIOENCODING=codec)
    return subprocess.run(
        [sys.executable, str(tool), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("codec", ["ascii", "gbk"])
def test_cli_verdicts_survive_a_non_utf8_stdout(tmp_path, codec, mod):
    """A healthy tree must report rc=0 even when stdout cannot encode Unicode.

    #1119 review (pm25coder, reproduced independently by how2how2how2-arch and
    here): the verdict lines printed ``✓`` / ``✗`` (U+2713 / U+2717). Any
    invocation whose stdout is a pipe or a file rather than a UTF-8 console
    raised ``UnicodeEncodeError`` on that print, so

    * a **consistent** tree exited 1 — reporting the exact "drift found" that
      this pre-push self-check exists to report, from a tree it had just proven
      consistent; and
    * a drifted tree exited 1 only by accident, through a traceback that threw
      away the list of drifted files.

    Neither the suite nor CI could see it: every other CLI test drives
    ``main()`` in-process, so stdout is a Python buffer and no codec is
    involved. ``ascii`` here is the general invariant (any console codec can
    encode ASCII); ``gbk`` is the codec from the report.
    """
    tool, root = _tool_tree(tmp_path)
    base = mod.read_current_version(root)

    clean = _run_tool(tool, ["--check"], root, codec)
    assert clean.stdout.isascii(), clean.stdout
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert clean.stderr == b"", clean.stderr
    assert b"OK: all 8 version sources agree on" in clean.stdout

    # Drifted: both the exit code and the diagnosis must survive the codec.
    stale = root / "emrg/gui/package.json"
    stale_text = stale.read_text(encoding="utf-8")
    stale.write_text(stale_text.replace(base, _sentinel(base)), encoding="utf-8")

    drifted = _run_tool(tool, ["--check"], root, codec)
    assert drifted.stdout.isascii(), drifted.stdout
    assert drifted.returncode == 1, drifted.stdout + drifted.stderr
    assert b"FAIL:" in drifted.stdout
    assert b"emrg/gui/package.json" in drifted.stdout, "the drift must be named"

    # The writer path prints its own verdict plus a next-steps block, and
    # ``--help`` sources its epilog from the module docstring — both are output
    # surfaces a host reads, so both go through the same codec.
    stale.write_text(stale_text, encoding="utf-8")
    bumped = _run_tool(tool, [TARGET], root, codec)
    assert bumped.stdout.isascii(), bumped.stdout
    assert bumped.returncode == 0, bumped.stdout + bumped.stderr
    assert b"LGTMs" in bumped.stdout, bumped.stdout
    assert _run_tool(tool, ["--check"], root, codec).returncode == 0
    assert _run_tool(tool, ["--help"], root, codec).returncode == 0

    rejected = _run_tool(tool, ["--check", "v0.2.94"], root, codec)
    assert rejected.returncode == 2, rejected.stdout + rejected.stderr
    assert rejected.stderr.isascii(), rejected.stderr


def test_tool_source_stays_ascii_only():
    """Static backstop for the crash above: keep every output byte printable.

    The behavioural test only exercises the output paths that exist today; a
    new ``print`` added on a path it does not drive would slip past it — the
    same "what does the new entry path bypass?" gap that let the original
    defect through three reviews. Restricting the *whole file* to ASCII is the
    one invariant that covers unknown future paths, so this script is kept
    ASCII-only on purpose (including comments): use ``-`` and ``->`` rather
    than the em dash/arrow this repo's prose normally carries.
    """
    raw = SCRIPT.read_bytes()
    offenders = sorted({byte for byte in raw if byte > 127})
    # The message names code points only: a failure report that itself contains
    # a character the console cannot encode would replace a clear assertion
    # error with a UnicodeEncodeError.
    codes = " ".join(f"0x{byte:02x}" for byte in offenders)
    assert not offenders, (
        "scripts/bump-version.py must stay ASCII-only so its output is "
        f"encodable by any console codec; found non-ASCII byte values: {codes}. "
        "This script prints a release verdict that hosts read through pipes, "
        "files and non-UTF-8 consoles; use '-' and '->' in its prose."
    )


# --------------------------------------------------------------------------
# which tree was bumped - measured 2026-09-11
# --------------------------------------------------------------------------


def _fake_checkout(root: Path, version: str) -> Path:
    """A checkout shape carrying one version source, and the two markers.

    Only ``emrg/__init__.py`` (the base source) is needed: the resolver keys on
    ``BASE_FILE`` plus ``scripts/``, and a one-source tree keeps the assertion
    about *which tree* independent of the 8-source content.
    """
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "emrg").mkdir(parents=True, exist_ok=True)
    (root / "emrg" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    return root


def test_the_tree_is_the_checkout_you_are_standing_in(mod, monkeypatch, tmp_path):
    """The defect, measured 2026-09-11 while unblocking a PR.

    Unblocking works in a git worktree, and the natural invocation is running
    the *main* checkout's copy of this script from inside it. The old root was
    ``Path(__file__).parent.parent`` - the main checkout - so the run reported

        checking all 8 files against 0.2.94 (emrg/__init__.py) ...
        OK: all 8 version sources agree on 0.2.94

    about the worktree, which was at 9.9.9 and drifted in 7 sources (its own
    copy of this script exits 1 there). A false green on the release gate, and
    ``bump()`` in that position rewrites that other checkout's eight version
    sources - including the one that decides what gets built.

    Pinned on the predicate, not on the printed line: ``_resolve_root`` is the
    decision, and a test that made ``main()`` agree could pass while the wrong
    root was still chosen.
    """
    fake = _fake_checkout(tmp_path / "checkout", "9.9.9")
    monkeypatch.chdir(fake)
    assert mod._resolve_root() == fake.resolve(), (
        "the tool must bump the checkout the caller is standing in; deriving "
        "the root from __file__ targets whatever checkout the script happens "
        "to live in and reports its version as if it were yours"
    )
    assert mod._resolve_root() != SCRIPT.parent.parent, (
        "the fixture must not be the script's own root, or this test proves nothing"
    )


def test_the_measured_tree_is_named_in_the_output(mod, monkeypatch, tmp_path, capsys):
    """`which tree did you bump` must never be ambiguous.

    A confident ``OK`` about a checkout the caller was not in is the failure
    above; naming the tree turns that from a silent wrong answer into a visible
    one on the single command that gates a release.

    Needs the complete 8-source layout: ``--check`` validates every source, so a
    one-source tree exits 1 for reasons unrelated to the tree being named.
    """
    fake = tmp_path / "checkout"
    for rel in SOURCE_FILES:
        dst = fake / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, dst)
    (fake / "scripts").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(fake)
    monkeypatch.setattr(mod, "REPO_ROOT", mod._resolve_root())
    assert mod.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert f"tree: {fake.resolve()}" in out, out


def test_a_directory_that_is_not_a_checkout_falls_back_to_the_script_root(
    mod, monkeypatch, tmp_path
):
    """The documented invocation must keep working from anywhere.

    ``python3 scripts/bump-version.py`` is run from the repo root in every hint
    this tool prints, but an absolute-path call from elsewhere (a wrapper, an
    editor task, ``git -C``) has no checkout in the cwd to stand in.
    """
    monkeypatch.chdir(tmp_path)  # a bare temp dir: no emrg/__init__.py, no scripts/
    assert mod._resolve_root() == SCRIPT.parent.parent.resolve()


def test_a_directory_with_only_half_the_shape_is_not_a_checkout(
    mod, monkeypatch, tmp_path
):
    """Both markers are required, so a stray base file does not claim the tree.

    The check is a heuristic for "this is a checkout of this project"; requiring
    both the base source and the scripts directory keeps it from matching, say,
    an extracted copy of the package that happens to sit under a temp dir.
    """
    (tmp_path / "emrg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "emrg" / "__init__.py").write_text(
        '__version__ = "0.2.94"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert mod._resolve_root() == SCRIPT.parent.parent.resolve()
