"""A move the walk cannot place is refused, not read as "inside" (issue #1357).

Both walks that place a move end in a *join*: a destination that is not absolute
is joined onto the directory in effect, and ``os.path.join(cwd, "$D")`` is a path
**inside** the workspace. So an unresolved destination did not read as "unknown"
— it read as "inside", which is the one direction this guard must never drift
in. Measured on master (all ``workspace-write``, predicate only):

| command | master | the shell reaches |
|---|---|---|
| `D=../outside && cd "$D" && cat > f` | ALLOW | outside the workspace |
| `for d in <outside>; do cd "$d" && cat > f; done` | ALLOW | outside the workspace |
| `cd "$(mktemp -d)" && cat > f` | ALLOW | a directory nobody can name from the text |

The three rows are the issue's own; this file pins them plus the two considerations
the fix has to get right and could get wrong:

* **the class is a lexeme, not a list of names.** ``$(mktemp -d)`` reaches the walk
  as the token ``$`` followed by ``mktemp`` and ``-d)`` — ``(`` and ``)`` are
  tokenizer punctuation — so a rule written against complete expansions refuses the
  quoted spelling and leaves the unquoted one open (measured while writing this).
* **the price is stated, not hidden.** A legitimate computed move is refused too,
  and so is a destination known to land in an allowed write root. Both are pinned
  below as residuals, so a later change has to lift them deliberately.

The rows that must keep their old verdict are here as well: a placeable relative
move, an assignment-decided move, the bare-variable *target* rule (`cp $SRC
$DST`, which is deliberately still allowed) and the literal move out.
"""

import os
import shutil
import subprocess
import tempfile

import pytest

from emrg.tools.bash_tool import (
    _check_sandbox,
    _cwd_at_write_site,
    _cwd_left_workspace,
    _is_absolute_path,
    _is_within,
    _temp_write_roots,
    _trusted_write_zones,
)

# Synthetic paths: the checks are textual, so the directories need not exist.
WORKDIR = os.path.join(os.path.expanduser("~"), "Documents", "emrg-1357-ws")
OUTSIDE = os.path.join(os.path.expanduser("~"), "Documents", "emrg-1357-outside")
WW = "workspace-write"


def spelled(path: str) -> str:
    """The path as a command line spells it (forward slashes)."""
    return path.replace(os.sep, "/") if os.sep != "/" else path


def names(path: str, text: str) -> bool:
    """Whether a block's message names ``path`` — in plain or in repr form.

    Both forms are needed, and the reason is a Windows-only measurement rather than a
    guess: the refusal interpolates the directory with ``{…!r}``, so on Windows every
    separator arrives **doubled** while the plain spelling has none, and the first run
    of this file's row was red there and green on POSIX for exactly that reason. This
    is the same shape as the ``spelled()`` helper above (one path, two spellings), so
    it is folded into a helper instead of being spelled out at each row.
    """
    forms = {path, os.path.realpath(path), spelled(path), spelled(os.path.realpath(path))}
    return any(f in text for f in forms | {repr(f) for f in forms})


def _verdict(cmd: str, mode: str = WW, workdir: str | None = WORKDIR) -> bool:
    allowed, _reason, _enforcement = _check_sandbox(cmd, mode, workdir)
    return allowed


def test_premise_the_fixture_is_outside_every_allowed_root():
    """The escape rows only measure anything if ``OUTSIDE`` is really outside."""
    assert _is_absolute_path(spelled(OUTSIDE))
    assert not _is_within(OUTSIDE, WORKDIR)
    roots = [_temp_write_roots(), _trusted_write_zones()]
    assert not any(_is_within(OUTSIDE, r) for group in roots for r in group)
    assert _is_absolute_path(spelled(WORKDIR))


# ---------------------------------------------------------------------------
# The class itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        'D=../outside && cd "$D" && cat > f',
        f'for d in {spelled(OUTSIDE)}; do cd "$d" && cat > f; done',
        'cd "$(mktemp -d)" && cat > f',
        'cd $(mktemp -d) && cat > f',
        'cd `mktemp -d` && cat > f',
        'D=../outside && cd "$D" && T=.emrg/tmp && cat > "$T/f"',
        'cd "${D}" && cat > f',
    ],
)
def test_a_move_no_scope_can_place_is_refused(cmd):
    """Each row's file really lands where no allowed root covers.

    The refusal must not be a coincidence of the *target* rule: the target is a
    bare relative name (`f`), which the tier allows, so the verdict can only come
    from the move walk.
    """
    allowed, reason, _enforcement = _check_sandbox(cmd, WW, WORKDIR)
    assert allowed is False, cmd
    assert "sandbox" in (reason or ""), (cmd, reason)
    # The block names the destination it could not place, so the caller can see
    # which part of the command to respell.
    assert "changing directory to" in (reason or ""), (cmd, reason)


@pytest.mark.parametrize(
    "cmd",
    [
        "cd sub && echo x > ../back.txt",
        "cd sub && cd sub2 && echo x > ../../deep.txt",
        f'D={spelled(WORKDIR)}/sub && cd "$D" && echo x > ../back1357.txt',
    ],
)
def test_a_decidable_move_is_still_placed(cmd):
    """The other direction: a walk that refused every `cd` would pass the rows above.

    ``sub`` is a relative destination joined onto the start directory, and the
    assignment-decided row is the scope issue #1316 added — both are moves the walk
    *can* place, so both keep the verdict the join gives them. The assignment row's
    destination is `<ws>/sub` and not `<ws>` on purpose: from the workspace root
    itself, `../back1357.txt` is a genuine climb out and the row would measure the
    `..` rule instead of this one.
    """
    assert _verdict(cmd) is True, cmd


@pytest.mark.parametrize(
    "cmd, operand",
    [
        ('D=../outside && cd "$D" && cat > f', "$D"),
        ('cd "$(mktemp -d)" && cat > f', "$(mktemp -d)"),
        ('cd $(mktemp -d) && cat > f', "$"),
    ],
)
def test_the_walk_reports_the_destination_it_could_not_place(cmd, operand):
    """`_cwd_left_workspace`'s answer, apart from the tier's verdict.

    A destination it cannot decide is reported by its own token — the treatment
    `cd -` already gets — rather than by a directory joined onto the cwd, which is
    what made the move invisible. The write-site walk's mirror answers the other
    way for the same text: `None`, i.e. "keep the start directory", the join base
    it falls back on whenever it cannot prove where the shell writes from.

    The split spelling reports the bare ``$`` it was handed, which is the token the
    tokenizer leaves of ``$(mktemp -d)`` — the same class, named by the fragment
    that reached it.
    """
    assert _cwd_left_workspace(cmd, WORKDIR) == operand, cmd
    assert _cwd_at_write_site(cmd, WORKDIR, "f") is None, cmd


@pytest.mark.parametrize(
    "cmd, operand",
    [
        (f'for d in {spelled(OUTSIDE)}; do cd "$d" && cat > f; done', "$d"),
        ('env -C "$D" cat > f', "$D"),
    ],
)
def test_the_cwd_walk_refuses_shapes_the_write_site_walk_does_not_read(cmd, operand):
    """Two rows where the *other* walk answers differently, and correctly.

    The write-site walk places a *statement*, so it does not read a `cd` behind the
    `do` of a loop (a grouping boundary its docstring bails on) and it does not read
    `env -C` at all — that one moves the *child*, while the redirect this walk is
    asked about is set up by the shell, which did not move. So its answer for both
    rows is the start directory (or `None` where the target is in a grouping), and
    the refusal comes from `_cwd_left_workspace` alone: a child that chdirs to an
    unplaceable directory can still write there (`env -C "$D" rm -rf build`).
    Refusing is this walk's side for the same reason the *placeable* spelling
    `env -C <outside> cat > f` is already refused on master — measured here as the
    control, so this is the existing rule extended rather than a new one.
    """
    assert _cwd_left_workspace(cmd, WORKDIR) == operand, cmd
    assert _verdict(cmd) is False, cmd
    # The control: the placeable spelling of the same over-approximation.
    assert _verdict(f"env -C {spelled(OUTSIDE)} cat > f") is False


def test_the_split_spelling_does_not_slip_past_the_class():
    """The tokenizer splits `$(…)`, so the class must not be written on whole lexemes.

    ``cd $(mktemp -d)`` reaches the walk as the token ``$`` and not as
    ``$(mktemp -d)``; a rule that only matched complete expansions refused the
    quoted spelling and left this one ALLOW (measured while writing this rule, and
    the reason the helper's test is "any surviving ``$`` or backtick"). Both
    spellings are asked here so a narrower rule cannot pass by fixing one.
    """
    for cmd in ('cd $(mktemp -d) && cat > f', 'cd "$(mktemp -d)" && cat > f'):
        assert _verdict(cmd) is False, cmd
    # The backquote spelling is one token here, and it is the same class.
    assert _verdict("cd `mktemp -d` && cat > f") is False


# ---------------------------------------------------------------------------
# The price, and the rules that must not move
# ---------------------------------------------------------------------------


def test_the_legitimate_computed_move_is_the_stated_price():
    """A computed destination that is *fine* is refused with the escapes.

    ``cd "$(git rev-parse --show-toplevel)"`` is a move a task may genuinely want,
    and the token stream cannot tell it from the escapes above without running it.
    Pinned as a residual: the change that lifts it — by resolving command
    substitution, or by letting a caller declare such a destination — has to lift
    it deliberately, and the caller's work-around until then is to spell the target
    absolutely.
    """
    cmd = 'cd "$(git rev-parse --show-toplevel)" && cat > f'
    assert _verdict(cmd) is False
    assert _cwd_left_workspace(cmd, WORKDIR) == "$(git rev-parse --show-toplevel)"


def test_a_destination_in_an_allowed_root_is_refused_with_the_rest():
    """``mktemp -d`` lands in the OS temp root, which the tier *does* allow.

    So this row's old ALLOW was arguably right and its refusal is friction — but
    *where it lands* is exactly what the text does not say, and the tier's own rule
    for an unresolvable root is to fail closed. Pinned so the friction is recorded
    rather than discovered, and paired with the literal `$TMPDIR` spelling, which
    stays allowed because the environment decides it without executing anything.
    """
    assert _verdict('cd "$(mktemp -d)" && cat > f') is False
    assert _verdict(f"cd {spelled(tempfile.gettempdir())} && cat > f1357.txt") is True


def test_the_target_side_rules_are_unchanged():
    """Two neighbours this change must not reach.

    ``cp $SRC $DST`` keeps its allowance: for a *target*, a bare variable operand
    is read as "relative, therefore inside" deliberately, because refusing it would
    refuse the copy idiom itself. A target rooted in an unknown variable
    (``$UNKNOWN/repo/out.txt``) was already refused by that rule, for the reason
    this change now applies to moves — the two sides agreeing is the point.
    """
    assert _verdict("cp $SRC $DST") is True
    assert _verdict("echo x > $UNKNOWN/repo/out.txt") is False


def test_the_literal_move_out_is_refused_for_its_own_reason():
    """A placeable destination outside keeps the issue #1244 refusal.

    The two reasons are distinguishable, and they should stay that way: this one
    names the *directory*, the unplaceable one names the *text*.
    """
    allowed, reason, _enforcement = _check_sandbox(
        f"cd {spelled(OUTSIDE)} && echo x > f", WW, WORKDIR
    )
    assert allowed is False
    assert names(OUTSIDE, reason or ""), reason
    assert _cwd_left_workspace(f"cd {spelled(OUTSIDE)} && echo x > f", WORKDIR) == os.path.realpath(OUTSIDE)


# ---------------------------------------------------------------------------
# Ground truth: the shell really moves, and the file really lands outside
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "the ground truth needs a POSIX shell: the point of the arm is where the "
        "file really lands, so it is gated to the platform that has one."
    ),
)
def test_the_unplaceable_move_really_writes_outside(tmp_path):
    """Measured, not asserted: the escape rows put their file outside the workspace.

    A verdict mismatch alone is not a bug, so the claim "the shell moved and the
    file left the workspace" is driven in `/bin/sh` inside a tree this test builds.
    The workspace is `<tmp>/ws` and the outside directory is `<tmp>/outside`; the
    file is looked for in both, and the arm fails if the shell did not escape, since
    then it would witness nothing.
    """
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("no POSIX shell is available for the ground-truth run")
    work = tmp_path / "ws"
    outside = tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    rows = {
        "assigned": (f'D=../outside && cd "$D" && cat > f_assigned', "outside"),
        "loop": (
            f'for d in {outside} ; do cd "$d" && cat > f_loop; done',
            "outside",
        ),
    }
    for name, (cmd, where) in rows.items():
        subprocess.run([shell, "-c", cmd], cwd=work, capture_output=True, check=False)
        assert (tmp_path / "outside" / f"f_{name}").exists(), (
            f"{name}: the shell did not write outside, so this arm has no job"
        )
        assert not (work / f"f_{name}").exists(), name
    # And the guard's verdict for the same two commands, so the arm and the
    # predicate are read against one another rather than separately.
    for name, (cmd, _where) in rows.items():
        assert _verdict(cmd, WW, os.path.join(spelled(str(tmp_path)), "ws")) is False, name
