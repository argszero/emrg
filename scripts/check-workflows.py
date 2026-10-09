#!/usr/bin/env python3
"""Does this tree's `.github/workflows/` pass the actionlint gate CI runs?

The class this exists for
-------------------------
This repository states the convention twice - `Agent.md` and the evolution loop - that a
CI check needs a **host-side counterpart**, or a host cannot self-check before CI and pays
a wasted round. The actionlint gate is the one gate here with no counterpart at all: CI
lints `.github/workflows/` (`.github/workflows/test.yml`, added by #441 after a workflow
that referenced the `secrets` context in an `if:` reached a push) and the loop that edits
workflows is told to "run `actionlint .github/workflows/*.yml` locally".

On this host that command does not exist, so the instruction answers nothing:

    $ command -v actionlint
    (nothing - rc 1)

Measured 2026-10-04 (`cyc20261004-084338`): actionlint, shellcheck, node and npm are all
absent from this host's PATH, while `brew` and `docker` are present, so the tool is one
install away. `bash: actionlint: command not found` (rc 127) is also the kind of answer
that is easy to read past, which is why the missing-binary case here is a **named**
`could not measure` carrying the install command, never a silent success.

The instrument is the gate's own
--------------------------------
The CI gate runs a **pinned** actionlint release, so a local reading is about that gate
only when the local binary *is* that build. Two consequences, both deliberate:

* the pin is read out of the workflow file that runs the gate, never spelled here - a
  second copy of "v1.7.12" is a copy that would not move when the pin does, and the
  workflow is the file the gate itself reads;
* a local actionlint that found nothing but is a **different build** is exit 2, not 0:
  the question a workflow change asks is "will the gate accept this", and another
  instrument's clean answer is not that answer. The reverse direction keeps its teeth - a
  finding is a finding whatever version produced it, so that case is exit 1.

Nothing here re-implements actionlint: it runs the same tool, at the same version CI
pins, over the same files. CI remains authoritative, because a local pass is only as good
as the version it came from.

Exit codes (the family's contract - "clean" and "could not measure" are never the same)
--------------------------------------------------------------------------------------
    0  the pinned actionlint ran over every workflow file in this tree and reported
       nothing
    1  actionlint reported at least one problem; its own output is quoted verbatim
    2  could not measure, with the reason and never as a pass: actionlint is not on PATH,
       no workflow file was found, the binary could not be run, or the build found is not
       the one CI pins

Usage
-----
    uv run --no-sync python3 scripts/check-workflows.py
    uv run --no-sync python3 scripts/check-workflows.py --root <checkout>
    uv run --no-sync python3 scripts/check-workflows.py --json

`--root` defaults to the checkout the caller is *standing in*, and the tree it read is
printed before any verdict, as every guard in this family does.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

#: Where a workflow lives, relative to the tree's root.
WORKFLOW_DIR = Path(".github") / "workflows"

#: How a workflow declares the actionlint build the gate runs. Two spellings, both read
#: from the tree rather than spelled here - the workflow is the file the gate itself
#: reads, and a second copy of the version in this script is a copy free to disagree
#: with it:
#:
#: * `uses: rhysd/actionlint@v1.7.12` - the Docker action this gate ran first. It builds
#:   its own image at run time, pulling alpine/golang/shellcheck-alpine from Docker Hub
#:   anonymously, and the per-IP throttle that hits reddens the whole leg (issue #2019);
#: * `ACTIONLINT_VERSION: "1.7.12"` - the pinned release binary `test.yml` downloads
#:   today, with no registry in the path.
#:
#: Both are kept, so a tree that has not moved yet still gets a reading instead of "no
#: gate found".
PIN = re.compile(
    r"(?:uses:\s*rhysd/actionlint@|ACTIONLINT_VERSION:\s*[\"']?)(?P<version>v?\d[\w.\-]*)"
)

#: What `actionlint --version` prints: the bare release, or a line naming where it is
#: installed. The first dotted triple is the version either way.
VERSION = re.compile(r"(\d+\.\d+\.\d+)")

#: How long one actionlint run may take. It parses a handful of YAML files; a run that
#: outlives this is a hung process, and a measurement is worth less than the wait.
_TIMEOUT = 120


def _resolve_root(given: str | None) -> Path:
    """The tree to read: the argument, else the checkout the caller is standing in.

    Derived from the cwd when *that* is a checkout (it holds `.github/workflows`), so a
    caller who is standing somewhere else is told which tree answered rather than being
    given a verdict about a checkout they were not looking at - the incident this
    family's `tree:` convention comes from. The script's own root is the fallback, which
    keeps `python3 scripts/check-workflows.py` working from anywhere.
    """
    if given:
        return Path(given).resolve()
    cwd = Path.cwd()
    if (cwd / WORKFLOW_DIR).is_dir():
        return cwd
    return Path(__file__).resolve().parent.parent


def workflow_files(root: Path) -> list[Path]:
    """Every workflow file in `root`, sorted, with the directory it came from.

    Both spellings are collected (`.yml` and `.yaml`): GitHub runs either, so a reading
    that covered one would report a clean tree while an unlinted file sat beside it.
    """
    directory = root / WORKFLOW_DIR
    if not directory.is_dir():
        return []
    return sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")])


def pinned_version(root: Path) -> str | None:
    """The actionlint version the tree's own workflows run, or `None` if none names one.

    Scanned across every workflow file rather than a hardcoded path, so moving the gate
    to another file does not silently stop this reading from finding it. `None` is a real
    answer: a tree with no gate has no gate verdict to give, which the caller reports as
    unmeasurable rather than as a pass.
    """
    for path in workflow_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = PIN.search(text)
        if found:
            return found.group("version").lstrip("v")
    return None


def _which(name: str) -> str | None:
    """`shutil.which`, behind a seam a test can drive without touching the host's PATH."""
    return shutil.which(name)


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """One actionlint invocation, captured. Its `returncode` is the reading."""
    return subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_TIMEOUT,
    )


def _local_version(exe: str, root: Path) -> str | None:
    """What the binary on PATH says it is, or `None` when it will not say.

    A binary that cannot answer `--version` cannot be trusted to have produced the verdict
    either, so its absence is unmeasurable rather than a default.
    """
    try:
        proc = _run([exe, "--version"], root)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    found = VERSION.search(proc.stdout or "")
    return found.group(1) if found else None


def main(argv: list[str] | None = None) -> int:
    # The family's buffering remedy (#1633 asserts it over `scripts/check-*.py`): stdout is
    # block-buffered under a pipe while stderr is not, so without this the "not measurable"
    # verdict on stderr overtakes the identity line the family promises comes first -
    # measured here as well, since the missing-binary case is this tool's likeliest run.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(
        prog="check-workflows.py",
        description="Does this tree's .github/workflows/ pass the actionlint gate CI runs?",
    )
    parser.add_argument("--root", default=None, help="the checkout to read (default: the cwd's)")
    parser.add_argument("--quiet", action="store_true", help="the verdict line only")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of prose")
    args = parser.parse_args(argv)

    root = _resolve_root(args.root)
    # First, before any verdict: the family's rule, and here also the only way a reader
    # learns which checkout answered. Under `--json` the same fact travels as the
    # document's own `tree` field, so the JSON stays one document.
    if not args.json:
        # The family's convention; under `--json` the same fact travels as the document's
        # own `tree` field, so the JSON stays one document.
        print(f"tree: {root}")

    files = workflow_files(root)
    pin = pinned_version(root)

    if not files:
        reason = (
            f"no workflow file under {root / WORKFLOW_DIR}, so there is no gate input to "
            "lint - this is not a pass"
        )
        return _unmeasured(args, root, reason, pin, 0)

    if pin is None:
        reason = (
            f"none of this tree's {len(files)} workflow file(s) runs a pinned actionlint "
            "(`uses: rhysd/actionlint@vX` or `ACTIONLINT_VERSION: \"X\"`), so there is no "
            "pinned build for a local run to be the counterpart of - this is not a pass"
        )
        return _unmeasured(args, root, reason, pin, len(files))

    exe = _which("actionlint")
    if exe is None:
        reason = (
            "actionlint is not on PATH, so the gate CI runs was not measured here - this "
            "is not a pass. Install it and re-run (the gate's own version is the one that "
            "counts):\n"
            f"  brew install actionlint   # or download the pinned release\n"
            f"  https://github.com/rhysd/actionlint/releases/tag/v{pin}\n"
            "  CI runs the gate itself on every push: "
            f"{WORKFLOW_DIR / 'test.yml'}"
        )
        return _unmeasured(args, root, reason, pin, len(files))

    local = _local_version(exe, root)
    if local is None:
        reason = (
            f"{exe} could not report its own version, so the verdict it would give cannot "
            "be read as the pinned gate's - this is not a pass"
        )
        return _unmeasured(args, root, reason, pin, len(files))

    try:
        proc = _run([exe, *[str(path) for path in files]], root)
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001 - a report
        reason = f"{exe} could not be run: {type(exc).__name__}: {exc}"
        return _unmeasured(args, root, reason, pin, len(files))

    output = ((proc.stdout or "") + (proc.stderr or "")).strip()

    if args.json:
        print(
            json.dumps(
                {
                    "tree": str(root),
                    "workflows": [path.name for path in files],
                    "pinned": pin,
                    "found": local,
                    "actionlint_rc": proc.returncode,
                    "output": output,
                },
                indent=2,
            )
        )
    elif not args.quiet:
        print(f"workflows: {len(files)} file(s) under {WORKFLOW_DIR}")
        print(f"actionlint: {exe} {local} (the gate pins v{pin})")

    if proc.returncode != 0:
        if not args.json:
            print(f"FAIL: actionlint {local} reports problem(s) in this tree's workflows:")
            for line in output.splitlines():
                print(f"  {line}")
        return 1

    if local != pin:
        reason = (
            f"actionlint {local} found nothing, but the gate CI runs is v{pin} - a "
            "different build's clean answer is not that gate's verdict, so this is not a "
            "pass. Install v" + pin + " and re-run."
        )
        return _unmeasured(args, root, reason, pin, len(files))

    if not args.json:
        print(f"OK: actionlint {local} reports no problem in {len(files)} workflow file(s)")
    return 0


def _unmeasured(
    args: argparse.Namespace, root: Path, reason: str, pin: str | None, count: int
) -> int:
    """Report an unmeasured question: the reason on stderr, the code the family reserves.

    Exit 2 and never 0, which is the whole point of the tool existing: the defect it was
    written against is an instruction that answers nothing while looking like it answered.
    """
    if args.json:
        print(
            json.dumps(
                {"tree": str(root), "measured": False, "reason": reason, "pinned": pin,
                 "workflows": count},
                indent=2,
            )
        )
    else:
        print(f"not measurable: {reason}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
