"""Tests for `scripts/check-extension-load.py` — issue #1544's referent.

The defect this tool exists for is a guard that asserts the **shape** of the macOS
entitlement and never the **effect** it was added for: a wheel's compiled module
loading under the bundled interpreter. The cost is not hypothetical — another project
on this host carried "no compiled C extension can load here" as an environment fact
for weeks and worked around it.

What these tests can and cannot drive
------------------------------------
They pin the **deciding** logic with pure calls — the three `codesign` readings, the
compiled-suffix predicate, the interpreter candidates — and then drive the tool
end-to-end in the two worlds that need no network: a `--python` that names nothing and
a `--python` that exists but cannot run, both of which must be **2** ("could not
measure"), never **0**. That asymmetry is the family's whole contract: a check that
cannot answer its question must not read as a pass, and this tool's question needs a
venv plus a wheel, so its unmeasurable states are reachable by ordinary accidents
(no install, no wheel for the platform, no network).

The wheel-loading path itself is *not* driven here: it installs from PyPI, and a test
that needs the network is a test that goes red for reasons unrelated to the code. It is
driven where the network is available — `packaging/smoke-test.sh` item 15 in CI, and
by hand on a host (`uv run python3 scripts/check-extension-load.py`, measured
2026-09-24 on this host: exit 0, `msgpack._cmsgpack.cpython-313-darwin.so`).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-extension-load.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_extension_load", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


check = _load()


def _run(*args: str) -> subprocess.CompletedProcess:
    """The tool as a user runs it — the exit code is part of the contract."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--quiet", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


class TestTheShapeReading:
    """Three readings, because the wrapper and the binary answer differently.

    Both transcripts below are verbatim from this host, 2026-09-24, and they are why
    the reading is a three-way decision rather than `KEY in output`:

    * `bin/python-dist/bin/python3.13` (the signed binary) prints the `[Key]` line;
    * `bin/python` (the POSIX wrapper, a shell script) prints ``code object is not
      signed at all`` — folding that into "absent" reported the install's shape from
      the wrong object, which is false about the install (measured: the first run of
      this tool said `absent` for an install whose binary carries the key).
    """

    SIGNED_WITH_KEY = (
        "Executable=/Users/x/.emrg/install/bin/python-dist/bin/python3.13\n"
        "[Dict]\n"
        "\t[Key] com.apple.security.cs.disable-library-validation\n"
        "\t[Value]\n"
        "\t\t[Bool] true\n"
    )
    SIGNED_WITHOUT_KEY = (
        "Executable=/opt/homebrew/Cellar/python@3.13/3.13.12/Frameworks/"
        "Python.framework/Versions/3.13/bin/python3.13\n"
    )
    NOT_SIGNED = "/Users/x/.emrg/install/bin/python: code object is not signed at all\n"

    def test_the_binary_reads_present(self):
        assert check.entitlement_reading(self.SIGNED_WITH_KEY) == "present"

    def test_a_signed_binary_without_the_key_reads_absent(self):
        """The defect's shape — and it must stay distinguishable from unsigned."""
        assert check.entitlement_reading(self.SIGNED_WITHOUT_KEY) == "absent"

    def test_a_script_reads_unsigned_rather_than_absent(self):
        assert check.entitlement_reading(self.NOT_SIGNED) == "unsigned"
        assert check.entitlement_reading(self.NOT_SIGNED) != "absent", (
            "a wrapper that carries no signature must not be reported as an install "
            "whose binary is signed without the entitlement"
        )


class TestTheCompiledPredicate:
    """The referent is a *compiled* module, not an import that happens to succeed.

    `import msgpack` succeeds without its C extension (the package falls back to
    `msgpack.fallback`), so a tool that imported the package would report a load
    that never happened. The suffix check is what makes the module name mean what it
    says — the C module's file is a real one from this host's arm run.
    """

    def test_a_wheel_so_is_compiled(self):
        assert check.is_compiled(
            "/tmp/venv/lib/python3.13/site-packages/msgpack/_cmsgpack.cpython-313-darwin.so"
        )

    def test_a_windows_pyd_is_compiled(self):
        assert check.is_compiled(r"C:\venv\Lib\site-packages\msgpack\_cmsgpack.cp313-win_amd64.pyd")

    def test_a_pure_python_module_is_not(self):
        assert not check.is_compiled(
            "/tmp/venv/lib/python3.13/site-packages/msgpack/fallback.py"
        )


class TestInterpreterDiscovery:
    """Discovery is a list, and an install it cannot find is unmeasurable — not green.

    The candidates are the layout `packaging/smoke-test.sh` item 14 spells (POSIX
    wrapper, Windows copy, and the `python-dist/` interpreter they resolve to), and
    the order is the tool's data rather than an accident of `glob` sorting.
    """

    def test_candidates_cover_the_documented_layout(self, tmp_path):
        names = [p.as_posix() for p in check.interpreter_candidates(tmp_path)]
        assert any(n.endswith("/.emrg/install/bin/python") for n in names)
        assert any(n.endswith("/.emrg/install/bin/python.exe") for n in names)
        assert any(n.endswith("/.emrg/install/bin/python-dist/bin/python3") for n in names)

    def test_an_install_that_is_not_there_raises_unmeasured(self, tmp_path):
        with pytest.raises(check.Unmeasured) as exc:
            check.find_interpreter(tmp_path, None)
        assert "no bundled interpreter found" in str(exc.value)

    def test_an_explicit_python_that_names_nothing_raises_unmeasured(self, tmp_path):
        with pytest.raises(check.Unmeasured) as exc:
            check.find_interpreter(tmp_path, str(tmp_path / "not-a-python"))
        assert "--python names nothing" in str(exc.value)

    def test_the_first_existing_candidate_wins(self, tmp_path):
        install = tmp_path / ".emrg" / "install" / "bin"
        install.mkdir(parents=True)
        (install / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        assert check.find_interpreter(tmp_path, None) == install / "python"


class TestUnmeasurableIsNeverAPass:
    """The contract, driven end-to-end: exit 2 for a question that was not answered."""

    def test_a_python_that_names_nothing_is_unmeasurable(self, tmp_path):
        proc = _run("--python", str(tmp_path / "missing-python"))
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "could not measure" in proc.stdout
        assert "unmeasurable" in proc.stdout

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="/bin/ls is a POSIX path; this arm drives a binary that exists but is "
        "not an interpreter, and the missing-path arm above already covers Windows",
    )
    def test_a_python_that_exists_but_cannot_run_is_unmeasurable(self):
        """`--python /bin/ls` was measured on this host: it is not a pass.

        The step that catches it is the one that asks the interpreter for its own
        path (`binary_of`), which is also the first proof that the interpreter runs.
        On Windows `/bin/ls` does not exist, so the run would answer through the
        other unmeasurable path — true, but a different claim, which is why it is
        skipped there rather than counted.
        """
        proc = _run("--python", "/bin/ls")
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "could not measure" in proc.stdout
        assert "unmeasurable" in proc.stdout
