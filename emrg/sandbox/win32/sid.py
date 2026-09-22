"""The per-directory write identities and the directory-boundary checks.

Ported from the blueprint's ``workspace-sid.ts`` and ``path-boundary.ts``
(``packages/sandbox/sandbox-windows-acl/src/``, dsh 0.1.6-alpha.2).

Two identities, deliberately not one:

* a **workspace** write SID, deterministic from the canonical workspace path,
  so the workspace-root ACE materializes once per workspace per machine (the
  grant's exact-ACE skip then makes every later provision O(1)) instead of once
  per session — the whole point of the per-workspace derivation;
* a **private temp** write SID, derived from the random per-session temp path,
  so sibling sessions sharing a workspace cannot enter one another's temp
  trees.

The SID's power is defined solely by the ACEs that name it, and the SID string
itself is not a secret: nothing here is a credential, it is an identity.
"""

from __future__ import annotations

import hashlib
import os

#: The sub-authority ceiling the derivation reduces into (30-bit, matching the
#: capability-SID shape the token and ACE layers carry).
_SUB_AUTHORITY_MOD = 2**30 - 1


def _derive(seed: bytes) -> tuple[int, int]:
    """Reduce a digest to the two sub-authorities of one capability SID.

    :param seed: the bytes that identify the directory.
    :returns: the two sub-authorities, each in ``1 .. 2**30-1``.
    """
    digest = hashlib.sha256(seed).digest()
    first = int.from_bytes(digest[0:4], "little") % _SUB_AUTHORITY_MOD + 1
    second = int.from_bytes(digest[4:8], "little") % _SUB_AUTHORITY_MOD + 1
    return first, second


def workspace_write_sid(workspace_root: str) -> str:
    """Derive the workspace's write SID (``S-1-4-x-y``).

    The input MUST be the canonical workspace path: canonicalization converges
    case and alias spellings, so two spellings of one workspace derive one SID
    (an as-spelled fallback would mint a second identity for the same
    directory — self-healing, at the cost of one extra tree propagation).

    :param workspace_root: the canonical workspace path.
    :returns: the SDDL string form.
    """
    first, second = _derive(workspace_root.encode("utf-8"))
    return f"S-1-4-{first}-{second}"


def temp_write_sid(temp_dir: str) -> str:
    """Derive one private temp directory's write SID (``S-1-4-x-y-1``).

    The random directory path *is* the capability identity; the fixed third
    sub-authority domain-separates the result from every two-sub-authority
    workspace SID.

    :param temp_dir: the private temp directory's absolute path.
    :returns: the SDDL string form.
    """
    first, second = _derive(b"temp\0" + temp_dir.encode("utf-8"))
    return f"S-1-4-{first}-{second}-1"


def contains_directory(root: str, candidate: str) -> bool:
    """Whether ``root`` is the same canonical directory as ``candidate`` or contains it.

    :param root: the prospective container.
    :param candidate: the path to test.
    :returns: true when ``candidate`` is ``root`` or lives beneath it.
    """
    resolved_root = os.path.realpath(root)
    resolved_candidate = os.path.realpath(candidate)
    if resolved_root == resolved_candidate:
        return True
    return resolved_candidate.startswith(resolved_root.rstrip("\\/") + os.sep)


def assert_temp_root_outside_workspace(workspace_root: str, temp_root: str) -> None:
    """Reject a temp parent that is inside the workspace.

    Every child created below such a parent would inherit the standing
    workspace capability, so the private temp would stop being private.

    :param workspace_root: the canonical workspace root receiving the standing ACE.
    :param temp_root: the existing parent beneath which a private temp child
        would be created.
    :raises ValueError: when the temp root is inside the workspace.
    """
    if contains_directory(workspace_root, temp_root):
        raise ValueError(
            "Windows ACL temp root must be outside the workspace: "
            f"workspace={workspace_root}; temp={temp_root}"
        )


def assert_private_temp_disjoint(writable_dirs: list[str], temp_dir: str) -> None:
    """Reject overlap between the private temp directory and any writable directory.

    Either inheritance direction would merge the two capabilities.

    :param writable_dirs: directories carrying the standing workspace capability.
    :param temp_dir: the directory carrying the revocable temp capability.
    :raises ValueError: when the two overlap.
    """
    for writable_dir in writable_dirs:
        if contains_directory(writable_dir, temp_dir) or contains_directory(temp_dir, writable_dir):
            raise ValueError(
                "AclSandbox private temp directory must be disjoint from writable "
                f"directories: writable={writable_dir}; temp={temp_dir}"
            )
