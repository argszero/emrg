"""A brace list is several words to the shell and one to this walk (issue #1396).

`{a,b}` is expanded *before* the command runs, so a destination or a target
carrying one names a path that is not in the token stream. Read literally, the
token is joined onto the cwd — so every spelling of the list reads as being
**inside** the workspace, the one direction this guard must never drift in.
Measured on master `67ba7f52` (predicate only, nothing executed, one geometry
whose outside directory is outside every allowed root):

| command | master | the shell reaches |
|---|---|---|
| `cd {../emrg-1396-outside,sub} && cat > f` | ALLOW | the sibling directory (this host's `cd` takes the first expanded word) |
| `rm -rf {../emrg-1396-outside/doomed,inside}` | ALLOW | the sibling directory: the list is handed to the verb's operands |
| `tee {../emrg-1396-outside/written,inside}` | ALLOW | the sibling directory, the same way |
| `cat > {../emrg-1396-outside/t,inside}` | ALLOW | nowhere — bash answers "ambiguous redirect" |
| `cd {../emrg-1396-outside,{a,b}} && cat > f` | ALLOW | the sibling directory, through the nested spelling |

This is the class `tests/test_unplaceable_move_destination.py` pins for
variables and command substitution — the two sides of the guard are asked
together here, because the same lexeme is read as a path by both.

Three things the rule has to get right, each pinned below:

* **the expansion is a comma or a `..` inside a brace pair.** ``a{b}c`` is one
  literal word to the shell as well, so it must keep the verdict the join gives
  it; the search is for the inner pair, which is also what stops the nested
  spelling from slipping past.
* **the price is stated, not discovered.** A *legitimate* list whose every
  spelling stays inside is refused too (``rm {dist,build}``), because which
  spelling the command takes is exactly what the text does not say. The
  work-around is the one every refusal in this file names: spell them out.
* **ground truth, and its limits.** A verdict mismatch alone is not a bug, so the
  expansion is driven in a real shell below — and that arm is honest about the
  one shell where it witnesses nothing: `dash` (the usual Linux `/bin/sh`) does
  not brace-expand at all.
"""

import os
import shutil
import subprocess

import pytest

from emrg.tools.bash_tool import (
    _BRACE_EXPANSION_RE,
    _check_sandbox,
    _cwd_at_write_site,
    _cwd_left_workspace,
    _is_absolute_path,
    _is_within,
    _temp_write_roots,
    _trusted_write_zones,
)

# Synthetic paths: the checks are textual, so the directories need not exist.
WORKDIR = os.path.join(os.path.expanduser("~"), "Documents", "emrg-1396-ws")
OUTSIDE = os.path.join(os.path.expanduser("~"), "Documents", "emrg-1396-outside")
WW = "workspace-write"

# The issue's own destination, plus the two other spellings the class has to hold
# for: the nested list and the sequence (`{1..3}` carries no comma at all).
LIST = "{../emrg-1396-outside,sub}"
NESTED = "{../emrg-1396-outside,{a,b}}"
SEQUENCE = "{1..3}"


def spelled(path: str) -> str:
    """The path as a command line spells it (forward slashes)."""
    return path.replace(os.sep, "/") if os.sep != "/" else path


def _verdict(cmd: str, mode: str = WW, workdir: str | None = WORKDIR) -> bool:
    allowed, _reason, _enforcement = _check_sandbox(cmd, mode, workdir)
    return allowed


def test_premise_the_fixture_is_outside_every_allowed_root():
    """The escape rows only measure anything if ``OUTSIDE`` is really outside.

    Without this, a fixture that happened to sit in the OS temp root or under
    ``~/.emrg`` would make the rows below pass for the wrong reason — the tier
    allows those as write roots, so a *placeable* move into them is allowed.
    """
    assert _is_absolute_path(spelled(OUTSIDE))
    assert not _is_within(OUTSIDE, WORKDIR)
    roots = [_temp_write_roots(), _trusted_write_zones()]
    assert not any(_is_within(OUTSIDE, r) for group in roots for r in group)
    assert _is_absolute_path(spelled(WORKDIR))


# ---------------------------------------------------------------------------
# The class itself, on both sides of the guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        f"cd {LIST} && cat > f",
        f"cd {NESTED} && cat > f",
        f"cd {SEQUENCE} && cat > f",
        f'D={LIST} && cd "$D" && cat > f',
    ],
)
def test_a_move_through_a_brace_list_is_refused(cmd):
    """Each row's file really lands where no allowed root covers.

    The target is a bare relative name (`f`), which the tier allows, so the
    verdict can only come from the move walk — the same discipline the sibling
    file's rows use. The block names the destination it could not place, so the
    caller can see which part of the command to respell.
    """
    allowed, reason, _enforcement = _check_sandbox(cmd, WW, WORKDIR)
    assert allowed is False, cmd
    assert "changing directory to" in (reason or ""), (cmd, reason)


def test_the_walk_reports_the_list_it_could_not_place():
    """`_cwd_left_workspace`'s answer, apart from the tier's verdict.

    A destination it cannot decide is reported by its own token — the treatment
    `cd -` and the `$(…)` class already get — rather than by a path joined onto
    the cwd, which is what made the move invisible. The write-site walk's mirror
    answers the other way for the same text: `None`, i.e. "keep the start
    directory", the join base it falls back on whenever it cannot prove where the
    shell writes from.
    """
    cmd = f"cd {LIST} && cat > f"
    assert _cwd_left_workspace(cmd, WORKDIR) == LIST
    assert _cwd_at_write_site(cmd, WORKDIR, "f") is None


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf {../emrg-1396-outside/doomed,inside}",
        "tee {../emrg-1396-outside/written,inside}",
        "echo x > {../emrg-1396-outside,inside}/f",
        f"echo x > {{{spelled(OUTSIDE)},inside}}",
        "rm {dist,build}",
        "cp a {b,c}",
    ],
)
def test_a_brace_target_is_refused_for_its_own_reason(cmd):
    """The target side, with the reason it is refused named as its own.

    The variable rule deliberately exempts a *bare* operand (`cp $SRC $DST`),
    because there the variable is the whole name and refusing it would refuse the
    copy idiom. A brace operand has no such reading: it is several words
    whichever way it is spelled, so it is refused with the rest — and the message
    says so, rather than reusing the variable rule's wording for a case that is
    not about variables.
    """
    allowed, reason, _enforcement = _check_sandbox(cmd, WW, WORKDIR)
    assert allowed is False, cmd
    assert "brace expansion the guard cannot place" in (reason or ""), (cmd, reason)
    assert "issue #1396" in (reason or ""), (cmd, reason)


def test_the_nested_spelling_does_not_slip_past_the_class():
    """The instrument reads the inner pair, and that is what makes nesting a match.

    The outer pair of `{../outside,{a,b}}` cannot be read as a list by a rule that
    skips brace characters (`[^{}]*` cannot cross the inner `{`), so a rule that
    only matched well-formed single lists would leave every nested spelling open.
    The inner pair is a match on its own, so the search finds it. The control rows
    are here as well: braces with neither a comma nor a `..` are one literal word
    to the shell, so the rule must NOT match them.
    """
    for text in ("{a,b}", "{1..3}", NESTED, "x{a,b}y", "{a,b}{c,d}"):
        assert _BRACE_EXPANSION_RE.search(text), text
    for text in ("a{b}c", "{inside}", "{a}", "{}", "plainname", ""):
        assert not _BRACE_EXPANSION_RE.search(text), text


def test_a_literal_brace_is_not_a_list():
    """The other direction: a rule that refused every brace would pass the rows above."""
    assert _verdict("cd {inside} && cat > f") is True
    assert _verdict("cat > a{b}c") is True
    assert _verdict("cd sub && cat > f") is True


def test_the_neighbours_keep_their_verdicts():
    """The classes either side of this one, unchanged.

    ``cp $SRC $DST`` is the variable rule's own documented exemption; the
    unresolved-root refusal is the one this change is modelled on; the computed
    move is the sibling file's class. A brace rule that reached any of them would
    be fixing this defect by breaking a decided one.
    """
    assert _verdict("cp $SRC $DST") is True
    assert _verdict("echo x > $UNKNOWN/repo/out.txt") is False
    assert _verdict('cd "$(mktemp -d)" && cat > f') is False
    assert _verdict("cd sub && cat > f") is True


def test_the_price_is_stated_not_discovered():
    """A list whose every spelling stays inside is refused too, with the work-around.

    `rm {dist,build}` writes two files inside the workspace and is refused
    anyway: the walk cannot say which spelling the shell takes, so it fails
    closed rather than guess — the same trade the `$(…)` class makes for a
    destination that lands in the OS temp root. Pinned so the friction is
    recorded rather than discovered, and paired with the spelling-out that
    keeps working.
    """
    assert _verdict("rm {dist,build}") is False
    assert _verdict("rm dist build") is True
    assert _verdict("cp a {b,c}") is False
    assert _verdict("cp a b") is True


# ---------------------------------------------------------------------------
# Ground truth: a real shell really expands the list
# ---------------------------------------------------------------------------


def _brace_expanding_shell() -> str | None:
    """A shell that expands `{a,b}`, or None.

    Asked rather than assumed, because the arm below is about *a* shell's
    behaviour and the two POSIX shells differ: `bash` expands the list before the
    command runs, `dash` (the usual Linux `/bin/sh`) does not expand it at all and
    would leave the command failing without a witness. The guard must fail closed
    either way — a command's meaning depends on the host's shell — so the refusal
    is asserted on every platform and only the witness is gated.
    """
    for name in ("bash", "sh"):
        shell = shutil.which(name)
        if shell is None:
            continue
        probe = subprocess.run(
            [shell, "-c", "printf '[%s]' {a,b}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if probe.stdout == "[a][b]":
            return shell
    return None


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "the ground truth needs a POSIX shell: the point of the arm is where the "
        "file really lands, so it is gated to the platform that has one."
    ),
)
def test_the_shell_really_hands_the_list_to_the_verb(tmp_path):
    """Measured, not asserted: the list really names two paths, one of them outside.

    A verdict mismatch alone is not a bug. The verbs that take several operands
    get the expanded list as operands, so `rm` and `tee` really reach the sibling
    directory in the same tree the guard is asked about — and this holds for bash
    and for dash alike, since it is the verb and not the shell that decides. The
    redirect row is here because it is the one shape that does **not** escape:
    bash refuses it as an ambiguous redirect and writes nothing. It is pinned so
    the claim "a list in a target is a live escape" is never made on the strength
    of that spelling — it was, in the first draft of this file, and the
    measurement said otherwise.
    """
    shell = _brace_expanding_shell()
    if shell is None:
        pytest.skip(
            "no brace-expanding shell here: dash does not expand a list, so this "
            "row would witness nothing"
        )
    work = tmp_path / "ws"
    outside = tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    (outside / "doomed").mkdir()
    workdir = os.path.join(spelled(str(tmp_path)), "ws")

    # The two verbs that hand the list straight to their operands.
    rm_cmd = "rm -rf {../outside/doomed,inside_r}"
    tee_cmd = "tee {../outside/written_by_tee,inside_t} </dev/null"
    for cmd in (rm_cmd, tee_cmd):
        allowed, reason, _enforcement = _check_sandbox(cmd, WW, workdir)
        assert allowed is False, cmd
        assert "brace expansion the guard cannot place" in (reason or ""), (cmd, reason)
        subprocess.run([shell, "-c", cmd], cwd=work, capture_output=True, check=False)
    assert not (outside / "doomed").exists(), "rm did not reach the sibling directory"
    assert (outside / "written_by_tee").exists(), "tee did not reach the sibling directory"

    # The redirect row: how far it *fails* is the shell's own business, and the
    # guard must refuse it anyway — for this walk the two spellings are one text.
    redirect = "cat > {../outside/redirected,inside_x} </dev/null"
    done = subprocess.run([shell, "-c", redirect], cwd=work, capture_output=True, check=False)
    assert done.returncode != 0, "expected the shell to refuse an ambiguous redirect"
    assert "ambiguous redirect" in done.stderr.decode(), done.stderr
    assert not (outside / "redirected").exists()
    assert _verdict(redirect, WW, workdir) is False


@pytest.mark.skipif(
    os.name == "nt",
    reason="the ground truth needs a POSIX shell, as above.",
)
def test_the_shell_places_a_brace_move_where_the_walk_cannot_follow(tmp_path):
    """The move row, witnessed where the shell's `cd` behaves that way.

    A brace list is two words to `cd`, and what `cd` does with two operands is
    the shell's business, not this guard's: measured here (macOS bash 3.2) it
    places the shell in the **first** word and the file lands in the sibling
    directory, while bash 5.x refuses `cd` with two operands and the `&&` then
    short-circuits. Both are stated rather than papered over: the escape is
    witnessed where it exists, and the arm skips with the measurement where it
    does not, because on that shell the row proves nothing about the guard. The
    refusal itself is asserted either way — the destination is text the walk
    cannot place, so it fails closed on both shells.
    """
    shell = _brace_expanding_shell()
    if shell is None:
        pytest.skip("no brace-expanding shell here: this row would witness nothing")
    work = tmp_path / "ws"
    outside = tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    cmd = "cd {../outside,sub} && cat > f_brace_move"
    assert _verdict(cmd, WW, os.path.join(spelled(str(tmp_path)), "ws")) is False
    subprocess.run([shell, "-c", cmd], cwd=work, capture_output=True, check=False)
    if not (outside / "f_brace_move").exists():
        pytest.skip(
            f"{os.path.basename(shell)}'s cd does not place a two-word destination "
            "in its first word, so the escape this row is about is not this "
            "shell's behaviour"
        )
    assert not (work / "f_brace_move").exists()
