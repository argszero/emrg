"""Issue #1261 — a Windows path is absolute for the shell that will run it.

The defect
----------
The guard reads write targets and directory operands from a POSIX token stream
(``shlex`` in POSIX mode, where a backslash is an *escape*). A Windows shell
(``cmd.exe`` — the bash tool's subprocess shell on that platform) treats the same
character as a *path separator*, so a Windows spelling arrived at every rule with
its separators deleted::

    echo x > C:\\Users\\x\\out.txt   ->  ['echo', 'x', '>', 'C:Usersxout.txt']

``C:Usersxout.txt`` is not absolute, so the ``workspace-write`` boundary read it
as "relative, therefore inside the workspace" and allowed it — while the same
write spelled ``/Users/x/out.txt`` was refused. Measured on master ``cca0b8dc``:
ten spellings (redirect, append, ``rm -rf``, ``mv``, ``cp``, ``sed -i``,
``find -delete``, chained) allowed at ``workspace-write``, 0 of them refused.
``read-only`` was unaffected, because that tier refuses every target without
asking where it resolves.

What is asserted here
---------------------
Two properties, and both are driven in **both** platform states rather than in
the one the test happens to run on:

* one write gets one verdict whatever the separator style of the workspace
  (the containment property), and
* the POSIX reading is *unchanged* — on a POSIX shell ``C:\\Users\\x`` really
  does name the relative file ``C:Usersx``, so repairing it there would refuse an
  ordinary in-workspace write. The forced-POSIX arm is therefore expected to
  reproduce the pre-fix verdicts exactly; that equality, not a hand-written list,
  is the regression guard.

The platform axis is a module constant (``_WINDOWS_SHELL``), so the Windows arm
is exercisable anywhere; the Windows CI leg additionally runs the corpus with the
natural flag, which is the only arm that measures ``ntpath`` itself.
"""
import os

import pytest

from emrg.tools import bash_tool as bt

WINDOWS_WS = r"C:\Users\x\repo"
OUT = "/etc"

# Writes whose target an absolute reading places outside the workspace.
OUTSIDE_WINDOWS = [
    r"echo x > C:\Users\x\out.txt",
    r"echo x >> C:\Users\x\out.txt",
    r"rm -rf C:\Users\x\important",
    r"rm -rf C:\Users",
    r"rmdir C:\Users\x",
    r"mv C:\Users\x\a.txt C:\Users\x\b.txt",
    r"cp -r C:\src C:\dst",
    "cp -r C:\\src C:\\dst\\",
    r"sed -i s/a/b/ C:\Users\x\f.txt",
    r"find C:\Users\x -delete",
    r'rm -rf "C:\Program Files\x"',
    r"cd /tmp && echo x > C:\Users\x\out.txt",
    r"echo x > \\server\share\out.txt",
    r"rm -rf \\server\share\dir",
    r"sh -c 'rm -rf C:\Users\x\important'",
]

# The same kind of write, spelled inside the workspace: it must stay allowed,
# or the rule would be a blanket refusal of Windows spellings.
INSIDE_WINDOWS = [
    r"echo x > C:\Users\x\repo\out.txt",
    r"rm -rf C:\Users\x\repo\build",
    r"sed -i s/a/b/ C:\Users\x\repo\f.txt",
]

# POSIX spellings whose verdicts must not move when the Windows rule is on;
# built in `_posix_corpus` against a real workspace.


@pytest.fixture(autouse=True)
def _pinned_write_roots(monkeypatch):
    """Pin the guard's write roots, so this file's verdict is about the tree.

    The corpus spells its workspace as a Windows path (`C:\\Users\\x\\repo`), and
    on a POSIX arm `os.path.realpath` resolves *both* sides under the cwd. A write
    root that contains the cwd therefore swallows the target, and the guard then
    allows the write by its own rule — correctly, because the resolved file really
    is inside a root it permits.

    That makes the file's verdict depend on where the tree was materialised, which
    is not a property of the tree. Measured before this pin: the file passes in a
    worktree inside the repository and reports **15 failed** for the identical
    tree when it is materialised under `tempfile.gettempdir()`. That is not a
    hypothetical — `scripts/check-merge-plan-suite.py` builds the tree a merge
    would land under exactly that root, so every plan on this master read FAILED
    while the product was correct.

    Pinning removes the ambient variable rather than the claim: what these tests
    measure is the Windows *spelling* — an absolute path outside the workspace —
    and the temp/trusted-root policy has its own tests. Both roots are pinned, not
    just the temp one, because the trusted zone is cwd-dependent the same way.
    """
    monkeypatch.setattr(bt, "_temp_write_roots", lambda: set(), raising=True)
    monkeypatch.setattr(bt, "_trusted_write_zones", lambda: set(), raising=True)


def _windows(monkeypatch, value):
    monkeypatch.setattr(bt, "_WINDOWS_SHELL", value, raising=True)


def _verdict(cmd, ws):
    allowed, _reason, _enforcement = bt._check_sandbox(cmd, "workspace-write", ws)
    return "ALLOW" if allowed else "BLOCK"


# ── the token stream itself ────────────────────────────────────────────────


def test_windows_separators_survive_the_split(monkeypatch):
    _windows(monkeypatch, True)
    for tokenize in (bt._split_command_tokens, bt._tokenize_command):
        tokens = tokenize(r"echo x > C:\Users\x\out.txt")
        assert tokens[-1] == r"C:\Users\x\out.txt", (tokenize.__name__, tokens)
        assert r"C:\Users\x" in tokenize(r"cd C:\Users\x; echo y"), tokens


def test_posix_shell_keeps_the_escape_reading(monkeypatch):
    """The gate, from the other side: on a POSIX shell the backslash escapes."""
    _windows(monkeypatch, False)
    assert bt._split_command_tokens(r"echo a\ b") == ["echo", "a b"]
    assert bt._split_command_tokens(r"echo x > C:\Users\x\out.txt")[-1] == (
        "C:Usersxout.txt"
    )


def test_quoted_arg_with_redirect_is_still_data(monkeypatch):
    """Issue #1162's cases must not move: `>` inside quotes is not a redirect."""
    for value in (True, False):
        _windows(monkeypatch, value)
        assert bt._extract_write_targets('echo "a > b"') == []
        assert bt._extract_write_targets('python3 -c "print(1 > 0)"') == []


# ── the boundary ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("cmd", OUTSIDE_WINDOWS)
def test_windows_write_outside_the_workspace_is_refused(cmd, monkeypatch):
    _windows(monkeypatch, True)
    assert _verdict(cmd, WINDOWS_WS) == "BLOCK", cmd


@pytest.mark.parametrize("cmd", INSIDE_WINDOWS)
def test_windows_write_inside_the_workspace_is_allowed(cmd, monkeypatch):
    _windows(monkeypatch, True)
    assert _verdict(cmd, WINDOWS_WS) == "ALLOW", cmd


@pytest.mark.parametrize("cmd", OUTSIDE_WINDOWS)
def test_read_only_refuses_them_too(cmd, monkeypatch):
    """#1261 records read-only as unaffected; pin that rather than assume it."""
    _windows(monkeypatch, True)
    allowed, _reason, _enforcement = bt._check_sandbox(cmd, "read-only", WINDOWS_WS)
    assert not allowed, cmd


def test_windows_rule_is_idempotent_on_its_own_output(monkeypatch):
    """Tokens come back out of `_nested_command_texts` and are re-tokenised."""
    _windows(monkeypatch, True)
    once = bt._split_command_tokens(r"rm -rf C:\Users\x\important")
    twice = bt._split_command_tokens(" ".join(once))
    assert once == twice


def test_the_write_roots_are_pinned_for_this_file():
    """A silent removal of the pin must fail here, not pass everywhere else."""
    assert bt._temp_write_roots() == set()
    assert bt._trusted_write_zones() == set()


def test_a_root_containing_the_tree_would_swallow_the_corpus(monkeypatch):
    """The mechanism the pin defends against, driven with the root made explicit.

    A write root containing the cwd turns the case into a permitted file, because
    the Windows spelling resolves *under* the cwd on a POSIX arm. Stated as a
    measurement rather than as a comment, so that a later change which makes the
    resolution cwd-independent fails here instead of quietly retiring the pin.
    """
    cmd = OUTSIDE_WINDOWS[0]
    _windows(monkeypatch, True)
    assert _verdict(cmd, WINDOWS_WS) == "BLOCK", "the pinned reading"
    monkeypatch.setattr(
        bt, "_temp_write_roots", lambda: {os.path.realpath(os.getcwd())}
    )
    assert _verdict(cmd, WINDOWS_WS) == "ALLOW", (
        "a write root containing the tree is what flips this verdict, so the pin "
        "is what keeps the corpus about the tree rather than about the directory"
    )


# ── no POSIX regression, measured by equality rather than by a list ─────────


def _posix_corpus(ws):
    return [
        (f"echo x > {OUT}/emrg-out.txt", "BLOCK"),
        (f"rm -rf {OUT}/emrg-dir", "BLOCK"),
        ("echo x > out.txt", "ALLOW"),
        ("rm -rf build", "ALLOW"),
        ("ls -la", "ALLOW"),
        ('echo "a > b"', "ALLOW"),
        ("grep -n 'rm -rf' notes.md", "ALLOW"),
    ]


def test_posix_shell_verdicts_are_unchanged(monkeypatch, tmp_path):
    """Both platform arms must agree on POSIX spellings — and on the expectation."""
    _windows(monkeypatch, False)
    before = [(c, _verdict(c, str(tmp_path))) for c, _ in _posix_corpus(tmp_path)]
    _windows(monkeypatch, True)
    after = [(c, _verdict(c, str(tmp_path))) for c, _ in _posix_corpus(tmp_path)]
    assert before == after, (before, after)
    for (cmd, verdict), (_cmd, expected) in zip(before, _posix_corpus(tmp_path)):
        assert verdict == expected, (cmd, verdict, expected)


@pytest.mark.skipif(
    os.name != "nt",
    reason="the natural-state arm only exists where the shell really is cmd.exe",
)
@pytest.mark.parametrize("cmd", OUTSIDE_WINDOWS)
def test_natural_windows_shell_refuses_without_patching(cmd):
    """The only arm that measures `ntpath` itself: no flag is injected here."""
    assert bt._WINDOWS_SHELL is True, "the module's own platform read"
    assert _verdict(cmd, WINDOWS_WS) == "BLOCK", cmd
