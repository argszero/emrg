"""`inside` has one home, and it answers the root case the same way on both platforms.

``emrg/tools/file_policy.py`` names :func:`is_within` as the definition of "what
``inside`` means", and ``emrg/sandbox/fence.py`` decides whether a write target
sits under a granted root.  The fence used to carry a private copy
(``_is_within_root``), and the copies had drifted by exactly one input:

    is_within("/etc/hosts", "/")      master: False   fence: True

— and on **Windows** the same call was already ``True``, because that arm of the
home normalised the root's separator (``rr.rstrip("/")``) while the POSIX arm
spelled the boundary as ``rr + os.sep``, so a root of ``/`` became ``//``.  The
copy that was right said so in its own docstring; the copy that was wrong was a
*different function*, so no reading compared them.

These tests are the reading that compares them, in the two shapes that survive
the fix: the boundary matrix, and the fence really reading the home (a negative
"no twin" assertion on its own would be satisfied by a fence that stopped
containing anything at all).
"""

from __future__ import annotations

import os

import pytest

from emrg.sandbox import fence
from emrg.sandbox.policy import resolve_policy
from emrg.sandbox.fence import file_refusal
from emrg.tools import file_policy
from emrg.tools.file_policy import is_within

#: An absolute path on every platform, outside every temp root a tier grants —
#: the same constant ``tests/test_file_policy_has_one_home.py`` uses, for the
#: same reason (``/tmp`` is not absolute on Windows, and is granted on Linux).
ABSOLUTE_ELSEWHERE = os.path.join(os.path.abspath(os.sep), "emrg-not-granted")


# ── the boundary, from both sides ────────────────────────────────────────


@pytest.mark.parametrize(
    "target,root,expected,why",
    [
        ("/a/b", "/a", True, "a child is inside"),
        ("/a", "/a", True, "the root itself is inside"),
        ("/a/b", "/a/", True, "a trailing separator on the root is not part of the boundary"),
        ("/other/b", "/a", False, "an unrelated path is outside"),
        ("/etc/hosts", "/", True, "everything absolute is inside the filesystem root"),
        ("/", "/", True, "and the root is inside itself"),
        ("/ab", "/a", False, "a shared string prefix is not a containment"),
        ("/a", "/ab", False, "and neither is the reverse"),
    ],
)
def test_is_within_reads_the_boundary(target, root, expected, why):
    assert is_within(target, root) is expected, why


def test_the_filesystem_root_contains_every_absolute_path():
    """The case the two homes disagreed on, named on its own so it cannot be lost.

    ``root.rstrip(os.sep)`` of ``/`` is the empty string, and every absolute path
    starts with ``/`` — that is the whole content of the rule. Spelling the
    boundary as ``root + os.sep`` makes it ``//``, which nothing starts with.
    """
    for target in ("/etc/hosts", "/Users/somebody/.zshrc", "/a", "/"):
        assert is_within(target, "/") is True, target


def test_the_windows_arm_answers_the_same_matrix(monkeypatch):
    """The platform substitution must not change which path contains which.

    That claim is in the function's docstring, and it is the reason the arm
    rewrites separators at all — so it is measured here rather than trusted:
    the same matrix, through the Windows spelling of the same paths, must give
    the same answers as the POSIX arm gave above.
    """
    monkeypatch.setattr(file_policy, "WINDOWS_SHELL", True)
    w = lambda p: p.replace("/", "\\")  # noqa: E731 — a spelling, not a path
    for target, root, expected, why in (
        ("/a/b", "/a", True, "a child is inside"),
        ("/a", "/a", True, "the root itself is inside"),
        ("/other/b", "/a", False, "an unrelated path is outside"),
        ("/etc/hosts", "/", True, "everything absolute is inside the root"),
        ("/ab", "/a", False, "a shared string prefix is not a containment"),
    ):
        assert is_within(w(target), w(root)) is expected, why


# ── one home: the fence reads it, and has no copy ────────────────────────


def test_the_fence_has_no_copy_of_the_rule():
    assert not hasattr(fence, "_is_within_root"), "the fence grew a second spelling again"


def test_the_fence_asks_the_home(monkeypatch):
    """The positive half: the fence's verdict follows the home's answer.

    Asserted by replacing the home's answer and watching a write that was allowed
    become refused — the reading that makes "no copy" mean *reads it* rather than
    *stopped checking*. ``/etc/hosts`` under a policy granting an unrelated root
    is the refused case; the same call with the home answering True is the allowed
    one, so the pair discriminates in both directions.
    """
    policy = resolve_policy(mode="workspace-write", workspace_root=ABSOLUTE_ELSEWHERE)
    assert file_refusal("/etc/hosts", policy) is not None, "the control: refused"

    monkeypatch.setattr(fence, "is_within", lambda target, root: True)
    assert file_refusal("/etc/hosts", policy) is None, "the fence did not ask the home"


def test_a_granted_root_of_the_filesystem_root_permits_any_absolute_target(monkeypatch):
    """The fence's own use of the fixed rule, through its real entry point.

    A tier that granted ``/`` is what the root case is about (the home's docstring
    says "which no tier grants but a policy could"). The granted root is replaced
    the way ``tests/test_file_policy_has_one_home.py`` replaces it — the fence's
    single input is the roots list — and the same target is measured under both
    roots, so the reading discriminates: contained under ``/``, refused under an
    unrelated root.
    """
    policy = resolve_policy(mode="workspace-write", workspace_root=ABSOLUTE_ELSEWHERE)
    target = "/etc/hosts"

    monkeypatch.setattr(fence, "writable_roots", lambda policy: ["/"])
    assert file_refusal(target, policy) is None, "a root of / grants every absolute path"

    monkeypatch.setattr(fence, "writable_roots", lambda policy: [ABSOLUTE_ELSEWHERE])
    assert file_refusal(target, policy) is not None, "and an unrelated root grants none"
