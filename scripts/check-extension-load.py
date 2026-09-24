#!/usr/bin/env python3
"""Does the install's bundled interpreter actually LOAD a compiled extension?

The class this exists for (issue #1544)
---------------------------------------
`packaging/` fixed "a pip-installed C extension cannot load under the bundled
interpreter" (macOS library validation, Team ID mismatch). **Every guard around that
fix asserts the entitlement, and none asserts the effect**, so the worlds where the
file is right and the load fails anyway are indistinguishable from a good install:

* `packaging/assets/python-entitlements.plist` + `tests/test_installer_stop.py` -
  the plist carries `com.apple.security.cs.disable-library-validation`;
* `packaging/make-installer.sh` - after signing, `codesign -d --entitlements -`
  prints it (fail-loud);
* `packaging/smoke-test.sh` item 14 - a venv is created and `pip --version` runs -
  neither of which loads a `.so`.

The cost of that gap is not hypothetical: a different project on this host carried
"any compiled C extension cannot load here" as a hard environment fact for weeks and
worked around it (a pure-Python brute force replacing an ILP solver, mypy/pyright
dropped). One command that loads a wheel's `.so` decides it in a single step, which
is what this script is.

What it measures, in order
--------------------------
1. which interpreter the install ships (it does **not** guess beyond `--python` and
   the documented install layout - an interpreter it cannot find is exit 2, never a
   pass);
2. the entitlement's **shape** on darwin (`codesign -d --entitlements -`), read from the
   *binary* CPython names as itself (`bin/python` is a wrapper script, and asking
   `codesign` about a script answers "not signed at all") and reported as a reading
   and **never** as the verdict - the shape can be absent while the load still works
   (measured: `/opt/homebrew/bin/python3.13`, whose `codesign` prints no such key,
   loads `msgpack._cmsgpack` all the same), and it can be present while the load
   fails (AMFI changes, quarantine, a wheel the loader rejects for another reason);
3. the **referent**: a venv from that interpreter, a compiled-extension wheel
   installed from a wheel (`--only-binary=:all:`, so the wheel's `.so` is the thing
   under test rather than a local build), and that wheel's C module imported in the
   venv's own interpreter. The module's `__file__` must be a compiled-extension
   suffix - a name that imports to a `.py` answers a different question, so it is
   unmeasurable rather than green.

Exit codes (the family's contract - "clean" and "could not measure" are never the
same value)
--------------------------------------------------------------------------
    0  measured: the wheel's compiled module imported, and its file is a `.so`/`.pyd`
    1  measured: it did NOT import, or imported to something that is not compiled -
       the referent failed, and the child's own message is quoted verbatim
    2  the check could not be made (no interpreter found, venv/pip failed, the wheel
       could not be fetched) - with the reason, never as a pass

Usage
-----
    python3 scripts/check-extension-load.py                     # the installed interpreter
    python3 scripts/check-extension-load.py --python /path/to/python3.13
    python3 scripts/check-extension-load.py --quiet             # verdict lines only
    python3 scripts/check-extension-load.py --keep               # leave the venv in place

The host-side counterpart of `packaging/smoke-test.sh` item 15 (which runs the same
chain in CI): the v0.2.7 lesson is that a CI-side validation with no host-side
spelling leaves the host unable to tell whether *their* install carries the fix.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: The compiled-extension suffixes CPython's own loader accepts (`importlib.machinery.
#: EXTENSION_SUFFIXES` covers `.cpython-313-darwin.so`, `.pyd`, `.abi3.so` - all of
#: which END with one of these). A module whose file ends in `.py` is not the
#: referent, however successfully it imports.
COMPILED_SUFFIXES = (".so", ".pyd")

#: The one wheel this check installs by default. `msgpack` is the wheel the issue
#: measured (`msgpack._cmsgpack`, a small unsigned third-party `.so`), and the module
#: is named **explicitly**: `import msgpack` alone succeeds without the C extension
#: (the package falls back to `msgpack.fallback`), so importing the package would
#: report a load that never happened.
DEFAULT_WHEEL = "msgpack"
DEFAULT_MODULE = "msgpack._cmsgpack"

#: The entitlement the fix is about, and the two readings of `codesign`'s output.
ENTITLEMENT_KEY = "com.apple.security.cs.disable-library-validation"

#: A wheel fetch is network-bound; a venv + install on a cold pip is the slowest
#: step here by an order of magnitude (measured: ~25 s for msgpack on this host).
INSTALL_TIMEOUT = 600


class Unmeasured(Exception):
    """The question cannot be answered - exits 2, never 0. The message is the reason."""


def out(quiet: bool, message: str) -> None:
    """Print a step line, unless the caller asked for the verdict lines only."""
    if not quiet:
        print(message)


def interpreter_candidates(home: Path) -> list[Path]:
    """Where the install's bundled interpreter lives, in the order to try it.

    The layout `packaging/` produces (read off `smoke-test.sh` item 14, which had to
    spell the same list): POSIX gets the wrapper at `bin/python`, Windows the copy at
    `bin/python.exe`, and the interpreter those wrappers resolve to sits in
    `bin/python-dist/`. The list is data rather than a decision so a candidate that
    appears later cannot silently lose to one that exists but is not the bundle's.
    """
    install = home / ".emrg" / "install" / "bin"
    return [
        install / "python",
        install / "python3",
        install / "python-dist" / "bin" / "python3",
        install / "python.exe",
        install / "python-dist" / "python.exe",
    ]


def find_interpreter(home: Path, explicit: str | None) -> Path:
    """The interpreter to measure, or :class:`Unmeasured` naming what was tried."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise Unmeasured(f"--python names nothing: {path}")
        return path
    tried = interpreter_candidates(home)
    for candidate in tried:
        if candidate.exists():
            return candidate
    raise Unmeasured(
        "no bundled interpreter found; tried "
        + ", ".join(str(p) for p in tried)
        + " (pass --python for an install in another location)"
    )


def entitlement_reading(blob: str) -> str:
    """What `codesign -d --entitlements -` said: ``present`` / ``absent`` / ``unsigned``.

    Pure, because the readings are what a reader has to check against a transcript, and
    there are **three** of them - not two. Measured 2026-09-24 while writing this tool,
    on this host's install:

    * the bundled interpreter *binary* (`bin/python-dist/bin/python3.13`) prints the
      `[Key]` line -> ``present``;
    * a Mach-O that is signed but does not carry this entitlement prints an `[Dict]`
      with other keys (or none) -> ``absent``, which is the defect's shape;
    * `bin/python` - the POSIX **wrapper**, a shell script - answers
      ``code object is not signed at all`` -> ``unsigned``. Folding that into
      ``absent`` would report the install's shape from the wrong object, which is the
      class of mistake this repository keeps paying for: measured, that reading is
      *false* about the install, since the binary next to it carries the entitlement.
    """
    if ENTITLEMENT_KEY in blob:
        return "present"
    if "not signed at all" in blob:
        return "unsigned"
    return "absent"


def binary_of(interpreter: Path) -> Path:
    """The executable CPython names as itself - the object a signature belongs to.

    Not the path the caller handed in: `bin/python` is a wrapper script on POSIX, and
    asking `codesign` about a script answers "not signed at all" rather than answering
    the question (measured above). The interpreter knows its own path, and asking it is
    also the first proof that it *runs* - a `--python` that cannot start is exit 2.
    """
    proc = run([str(interpreter), "-c", "import sys; print(sys.executable)"])
    if proc.returncode != 0 or not proc.stdout.strip():
        raise Unmeasured(
            f"`{interpreter} -c 'import sys; print(sys.executable)'` failed: "
            f"{combined(proc) or 'no output'}"
        )
    return Path(proc.stdout.strip().splitlines()[-1])


def is_compiled(module_file: str) -> bool:
    """Does this imported module's file end in a compiled-extension suffix?"""
    return module_file.endswith(COMPILED_SUFFIXES)


def run(argv: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run a step, capturing both streams (a step's reason is often on stderr).

    ``encoding="utf-8"`` is pinned rather than left to the locale: the child's output is
    read to quote it back to the user, and a non-UTF-8 host (cp936/cp1252) would raise
    ``UnicodeDecodeError`` on a loader message that is perfectly valid UTF-8 - the class
    `tests/test_script_decode_is_locale_independent.py` guards repo-wide (issues
    #1119/#1121/#1132).
    """
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def combined(proc: subprocess.CompletedProcess) -> str:
    """A step's own words, stderr first - the loader's message is what a reader needs."""
    return ((proc.stderr or "") + (proc.stdout or "")).strip()


def measure(interpreter: Path, wheel: str, module: str, quiet: bool, keep: bool) -> int:
    """The three steps, in order. Returns the exit code (0/1/2)."""
    print(f"check-extension-load: measuring {interpreter}")
    print(f"  tree: {Path.cwd()}")

    binary = binary_of(interpreter)
    shape = "not applicable"
    if sys.platform == "darwin" and shutil.which("codesign"):
        codesign = run(["codesign", "-d", "--entitlements", "-", str(binary)])
        shape = entitlement_reading(combined(codesign))
        out(quiet, f"  binary: {binary}")
        out(quiet, f"  entitlement: {shape} ({ENTITLEMENT_KEY})")
        if shape != "present":
            out(
                quiet,
                "  note: the shape is not the verdict - a wheel's .so can load with the "
                "entitlement absent, and can fail with it present; what follows measures "
                "the load itself",
            )
    else:
        out(quiet, "  entitlement: not applicable (the entitlement is a macOS restriction)")

    workdir = Path(tempfile.mkdtemp(prefix="emrg-ext-load-"))
    venv = workdir / "venv"
    try:
        proc = run([str(interpreter), "-m", "venv", str(venv)])
        if proc.returncode != 0:
            raise Unmeasured(f"`{interpreter} -m venv` failed: {combined(proc) or 'no output'}")
        venv_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not venv_python.exists():
            raise Unmeasured(f"the venv has no interpreter at {venv_python}")
        out(quiet, f"  venv: {venv_python}")

        proc = run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--quiet",
                "--only-binary=:all:",
                wheel,
            ],
            timeout=INSTALL_TIMEOUT,
        )
        if proc.returncode != 0:
            raise Unmeasured(
                f"`pip install --only-binary=:all: {wheel}` failed (a wheel for this "
                f"platform, or the network, is what this step needs): "
                f"{combined(proc) or 'no output'}"
            )
        out(quiet, f"  wheel: {wheel} installed from a wheel")

        proc = run([str(venv_python), "-c", f"import {module} as m; print(m.__file__)"])
        if proc.returncode != 0:
            print(f"  import: {module} FAILED to import")
            print(f"VERDICT: measured - the bundled interpreter did NOT load a wheel's "
                  f"compiled module ({module})")
            print("  the loader's own message, verbatim:")
            for line in (combined(proc) or "no output").splitlines():
                print(f"    {line}")
            if shape == "absent":
                print(
                    f"  the shape reading above ({ENTITLEMENT_KEY} absent) is one "
                    "candidate cause of exactly this failure - the pair is the defect "
                    "this check exists for, and it is now measured rather than inferred"
                )
            else:
                print(
                    "  the shape reading above is not `absent`, so the cause is "
                    "something else (a quarantined or mismatched Mach-O, a loader "
                    "refusal with its own reason)"
                )
            return 1

        module_file = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        if not is_compiled(module_file):
            raise Unmeasured(
                f"`{module}` imported from {module_file or 'an unnamed file'}, which is not "
                f"a compiled extension ({'/'.join(COMPILED_SUFFIXES)}); the question - does "
                "a wheel's .so load - was therefore not asked"
            )
        out(quiet, f"  import: {module} -> {module_file}")
        print("VERDICT: measured - a wheel's compiled module loads under the interpreter")
        return 0
    finally:
        if keep:
            out(quiet, f"  kept: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure whether the bundled interpreter loads a wheel's .so "
        "(the referent of the macOS disable-library-validation entitlement, issue #1544).",
    )
    parser.add_argument(
        "--python",
        default=None,
        help="the interpreter to measure (default: the bundled one under ~/.emrg/install)",
    )
    parser.add_argument("--wheel", default=DEFAULT_WHEEL, help=f"the wheel to install (default: {DEFAULT_WHEEL})")
    parser.add_argument("--module", default=DEFAULT_MODULE, help=f"its C module to import (default: {DEFAULT_MODULE})")
    parser.add_argument("--quiet", action="store_true", help="print the verdict lines only")
    parser.add_argument("--keep", action="store_true", help="leave the venv in place for inspection")
    args = parser.parse_args()

    try:
        interpreter = find_interpreter(Path.home(), args.python)
        return measure(interpreter, args.wheel, args.module, args.quiet, args.keep)
    except Unmeasured as exc:
        print(f"check-extension-load: could not measure - {exc}")
        print("VERDICT: unmeasurable (this is not a pass)")
        return 2
    except subprocess.TimeoutExpired as exc:
        print(f"check-extension-load: could not measure - {exc}")
        print("VERDICT: unmeasurable (this is not a pass)")
        return 2


if __name__ == "__main__":
    sys.exit(main())
