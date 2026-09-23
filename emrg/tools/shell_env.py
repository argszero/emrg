"""Where a confined run's package caches may live — one definition, two dialects.

Both shell tools (``bash_tool_v2`` for POSIX, ``pwsh_tool_v2`` for Windows) need
this rule and it is dialect-independent: a confined child cannot write the
deployer's home directory, which is where ``uv``, ``pip`` and ``npm`` keep their
caches.  It lives here rather than in either tool for the reason the repo's own
merge discipline keeps proving — a rule with two copies is a rule that drifts —
and ``pwsh_tool_v2`` may not import ``bash_tool_v2`` (design §14.5 item 1: the
dialects are peers, not one layered on the other).

Why the relocation exists at all: measured on the dev host through the bash tool
at ``workspace-write``, the three failure modes differ and all three are real:
``uv run --no-sync python3 -c 'pass'`` **fails** (``error: Failed to initialize
cache at /Users/<host>/.cache/uv``); ``npm``'s default ``~/.npm`` **cannot be
created** (``touch: Operation not permitted``); and ``pip`` **quietly disables
its cache** (``The directory '~/Library/Caches/pip' … is not writable … The
cache has been disabled``), which is the worst of the three to debug because the
command still succeeds.

The blueprint has no mechanism for this because it does not need one: ``dsh`` is
launched from the deployer's shell, so the deployer's environment is where a
cache relocation can be declared.  EMRG's daemon is normally started by the GUI
or by a launcher, inheriting neither, so the same instruction would be an
instruction nobody can carry out.  Pointing these at a directory **inside the
run's own granted temp area** is therefore EMRG's job — and it widens nothing:
the temp area is already a writable root of the ``workspace-write`` policy
(``sandbox.roots.writable_roots``), which a test asserts rather than assumes.

Only *caches* are relocated.  Credentials and configuration (``~/.config``,
``~/.ssh``, the git credential store) stay where they are: a confined run that
needs to write those should fail loudly, not write a second copy somewhere the
host will never look.
"""

from __future__ import annotations

import os
import tempfile

from emrg.sandbox.policy import SandboxPolicy
from emrg.sandbox.roots import canonical_path, writable_roots

#: The directory the relocated caches live in, under the granted temp root.
CACHE_DIR_NAME = "emrg-confined-cache"

#: Environment variable → subdirectory of the relocated cache root.  An empty
#: subdirectory means the root itself (``XDG_CACHE_HOME`` names the directory
#: caches live *in*).  A variable the deployer already set is left alone, so a
#: warm cache stays reachable wherever the deployer put it.
CACHE_ENV: dict[str, str] = {
    "XDG_CACHE_HOME": "",
    "UV_CACHE_DIR": "uv",
    "PIP_CACHE_DIR": "pip",
    "npm_config_cache": "npm",
}


def confined_env(policy: SandboxPolicy, base: str | None = None) -> dict[str, str]:
    """The environment variables a confined run needs to be usable.

    Empty for a policy that grants no writable root: under ``read-only`` there is
    no directory to relocate a cache *into*, and pretending otherwise would name
    a boundary the run does not have.

    :param policy: the policy this run is confined under.
    :param base: the relocated cache root; defaults to
        ``<tempfile.gettempdir()>/emrg-confined-cache``.
    :returns: the variables to add to the child's environment — only those the
        current environment does not already name.
    """
    if not writable_roots(policy):
        return {}
    # Canonical, because that is the identity the policy grants and the profile
    # matches on: on darwin ``gettempdir()`` reports ``/var/folders/...`` while
    # the granted root is ``/private/var/folders/...`` (design §3.5).
    root = canonical_path(base or os.path.join(tempfile.gettempdir(), CACHE_DIR_NAME))
    out: dict[str, str] = {}
    for name, sub in CACHE_ENV.items():
        if name in os.environ:
            continue
        out[name] = os.path.join(root, sub) if sub else root
    return out
