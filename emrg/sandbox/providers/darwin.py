"""macOS Seatbelt backend.

Mirrors ``packages/sandbox/sandbox-local`` in deepseek-harness: the profile
builder is ``profiles.ts`` (``seatbeltProfileArgs``) and the three facts around
it are the darwin rows of the tables in ``sandbox-local/src/index.ts`` — the
static enforcement table (``:177-187``), the denial dialect (``:205-213``) and
the runner-failure rules (``:231-240``).

Two properties of this backend are load-bearing and were measured on this host
rather than assumed (blueprint §3.3, §3.4):

* the ``/dev/null`` line goes in **unconditionally**.  Without it almost every
  real command dies (``fatal: could not open '/dev/null' for reading and
  writing: Operation not permitted``) and the error points at ``/dev/null``
  rather than at the sandbox — the hardest possible failure to attribute.
* nothing here checks that a writable root exists.  Seatbelt answers a
  ``subpath`` that does not exist with silence: it grants nothing and reports
  nothing, and a real root beside it keeps working.  A pre-check would be a gate
  the blueprint does not have (design N2), and its removal is what makes a
  missing root the conservative outcome instead of an error.
"""

from __future__ import annotations

from emrg.sandbox.contract import Runner, RunnerFailureRule
from emrg.sandbox.policy import SandboxPolicy
from emrg.sandbox.roots import writable_roots

#: ``sandbox-exec`` ships with every macOS at a fixed path; Apple marks the CLI
#: deprecated but still ships and still enforces it (measured on macOS 26.6.2).
#: If it is ever removed, the *spawn* fails and the run fails closed — which is
#: why this backend needs no runtime probe (design N1).
SEATBELT_EXEC = "/usr/bin/sandbox-exec"

#: Seatbelt governs every promised file effect by construction, so the claim is
#: a profile fact rather than a probe result — this is the static table's darwin
#: row.  The half of that fact a sentence cannot carry is *removal*: an
#: operation set named "file-write" reaching a directory-entry change is a
#: property of this kernel, not of its name, and a confinement that covered
#: contents alone would leave the path free.
#: ``tests/test_bash_v2_boundary.py::test_deletion_is_governed_by_the_same_grant_as_a_write``
#: measures it.
ENFORCEMENT = "full"

#: Seatbelt's denial dialect: the string a refused write produces (EPERM).
#: Dialect, not a cross-backend union — the union would claim denials this
#: backend never produces.
DENIAL_SIGNATURES: tuple[str, ...] = ("operation not permitted",)

#: ``sandbox-exec`` publishes no launcher-failure exit status, so the rule is
#: signature-only, exactly as the blueprint has it.  A profile the kernel
#: refuses exits 65 with ``sandbox-exec: <detail>`` on stderr — measured, and the
#: only evidence that the command never ran.
#:
#: The lines, recorded rather than paraphrased because ``fatal_signatures`` is
#: matched against them: ``/usr/bin/sandbox-exec`` on macOS 26.6.2 (build 25G83),
#: each profile passed with ``-p``, the command ``/bin/echo hi``; all five cases
#: exited 65 and printed the prefix first —
#:
#: * ``(version 1`` → ``sandbox-exec: syntax error: expecting ')'``
#: * ``(version 1)(allow default)(bogus-op)`` → ``sandbox-exec: unbound variable:
#:   bogus-op at <input string>, line 1, column 28``
#: * ``(version 1)(allow default)(allow (bogus-filter))`` → the same, naming
#:   ``bogus-filter``
#: * ``(version 1)(allow default)(deny file-write* (literal "unterminated))`` →
#:   ``sandbox-exec: Error reading string``
#: * ``(allow default)`` → ``sandbox-exec: no version specified``
#:
#: So a profile ``seatbelt_profile_args`` builds that the kernel will not compile
#: is a runner failure and not a denial, which is the distinction the rule exists
#: for (a generator bug must not read as the policy saying no).
#:
#: The prefix is the half the rule matches on: unlike ``bwrap`` (whose ``linux``
#: sibling records the same standard) no measured fatal line lacks it, and the
#: bare detail is not a line this runner prints.  The exit status is *not* part of
#: the rule even though 65 held in all five cases: that is one macOS version's
#: behaviour, while the prefix is the launcher's own contract — and a gate would
#: silently stop classifying a runner failure on a version that exits differently,
#: turning the event this rule names into the denial it must not be read as.
RUNNER_FAILURE_RULES: tuple[RunnerFailureRule, ...] = (
    RunnerFailureRule(fatal_signatures=("sandbox-exec: ",)),
)


def sbpl_string(path: str) -> str:
    """Quote one path as an SBPL string literal.

    :param path: the path as it will be compared by the kernel.
    :returns: the quoted literal, backslash and double quote escaped.
    """
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile_args(policy: SandboxPolicy) -> list[str]:
    """Build the ``sandbox-exec`` arguments and SBPL profile for one policy.

    The writable roots come from the shared :func:`~emrg.sandbox.roots.writable_roots`
    helper (canonical, deduplicated), so this grant and the in-process fence
    ``write``/``edit`` use cannot drift apart.

    :param policy: the file-effect policy to express as an SBPL profile.
    :returns: the runner arguments before the trailing ``--`` and caller argv.
    """
    forms = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f"(allow file-write* (literal {sbpl_string('/dev/null')}))",
    ]
    roots = writable_roots(policy)
    if roots:
        grants = " ".join(f"(subpath {sbpl_string(root)})" for root in roots)
        forms.append(f"(allow file-write* {grants})")
    return ["-p", " ".join(forms)]


def runner_argv(policy: SandboxPolicy) -> list[str]:
    """The runner invocation (program plus profile arguments) for one policy.

    :param policy: the file-effect policy to confine under.
    :returns: the argv the seam prepends to the caller's own.
    """
    return [SEATBELT_EXEC, *seatbelt_profile_args(policy)]


#: The backend as the chain tables carry it.
SEATBELT = Runner(
    name="seatbelt",
    enforcement=ENFORCEMENT,
    denial_signatures=DENIAL_SIGNATURES,
    runner_failure_rules=RUNNER_FAILURE_RULES,
    runner_argv=runner_argv,
)
