"""Tests for `scripts/check-extension-load.py` — issue #1544's referent.

The defect this tool exists for is a guard that asserts the **shape** of the macOS
entitlement and never the **effect** it was added for: a wheel's compiled module
loading under the bundled interpreter. The cost is not hypothetical — another agent
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
import time
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
        assert any(n.endswith("/.emrg/global/install/bin/python") for n in names)
        assert any(n.endswith("/.emrg/global/install/bin/python.exe") for n in names)
        assert any(n.endswith("/.emrg/global/install/bin/python-dist/bin/python3") for n in names)

    def test_an_install_that_is_not_there_raises_unmeasured(self, tmp_path):
        with pytest.raises(check.Unmeasured) as exc:
            check.find_interpreter(tmp_path, None)
        assert "no bundled interpreter found" in str(exc.value)

    def test_an_explicit_python_that_names_nothing_raises_unmeasured(self, tmp_path):
        with pytest.raises(check.Unmeasured) as exc:
            check.find_interpreter(tmp_path, str(tmp_path / "not-a-python"))
        assert "--python names nothing" in str(exc.value)

    def test_the_first_existing_candidate_wins(self, tmp_path):
        install = tmp_path / ".emrg" / "global" / "install" / "bin"
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


class TestOfflineNamesTheFetchInsteadOfWaitingForIt:
    """The release leg's offline smoke drops :80/:443 and then runs item 15.

    Against a dropped port the wheel fetch cannot be answered at all, and pip does not
    see a refusal - it waits out six connections and its retries (measured 98.1 s on
    this host against a listener that accepts and stalls, which is the shape a DROP
    produces). `--offline` states that instead of paying it, and these arms pin the
    three halves that matter: it refuses (exit 2, never 0), it names the caller's own
    decision rather than a pip failure, and it does not spend the ladder.
    """

    def test_offline_refuses_by_name_rather_than_waiting_out_pip(self):
        started = time.monotonic()
        proc = _run("--offline", "--python", sys.executable)
        elapsed = time.monotonic() - started
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "could not measure" in proc.stdout
        assert "--offline" in proc.stdout
        assert "unmeasurable" in proc.stdout
        # The discriminating reading, and the reason this arm is about time: pip's
        # ladder against a dropped port is 98.1 s (measured), so a run under 10 s
        # cannot have been that ladder. Without this, an `--offline` that quietly did
        # nothing would still pass the three assertions above.
        assert elapsed < 10, f"took {elapsed:.1f}s - the refusal did not skip the fetch"

    def test_the_refusal_is_not_a_pip_failure(self):
        """A blocked network and a broken index answer different questions.

        Folding the first into the second would report the caller's own decision as a
        fault of the wheel, which is the class of mistake this repository keeps paying
        for - so the refusal may not mention pip at all.
        """
        proc = _run("--offline", "--python", sys.executable)
        combined = proc.stdout + proc.stderr
        assert "pip install" not in combined, combined
        assert "no `.so` to load" in combined, combined


def _offline_step_block(workflow: str) -> str:
    """The `Smoke offline (Linux only, R96)` step, from its name to the next step."""
    lines = workflow.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "- name: Smoke offline (Linux only, R96)")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("- name:")),
        len(lines),
    )
    return "\n".join(lines[start:end])


class TestTheReleaseLegReachesThisFlag:
    """A flag nothing passes has no reader, and this probe's only offline caller is CI.

    The defect this guards is the one the step had: it ran the script behind `|| true`
    and then ended on its last `iptables -D`, so the script's own exit code reached
    nothing. Both carriers are pinned here because either one alone would leave the
    verdict unread - the flag without the step, or the step without the flag.
    """

    def test_the_workflow_sets_the_offline_switch_and_the_script_forwards_it(self, tmp_path):
        workflow = (REPO_ROOT / ".github" / "workflows" / "build-release.yml").read_text(encoding="utf-8")
        block = _offline_step_block(workflow)
        assert "EMRG_SMOKE_OFFLINE=1" in block, block

        script = (REPO_ROOT / "packaging" / "smoke-test.sh").read_text(encoding="utf-8")
        assert "EMRG_SMOKE_OFFLINE" in script
        assert "ext_args+=(--offline)" in script

    def test_the_offline_step_does_not_discard_the_scripts_exit_code(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "build-release.yml").read_text(encoding="utf-8")
        block = _offline_step_block(workflow)
        assert "smoke-test.sh dist/runtime || true" not in block, (
            "the script's exit code is discarded again: `|| true` after the run"
        )
        assert "rc=$?" in block, "the script's exit code is not captured"
        assert "GITHUB_STEP_SUMMARY" in block, "the verdict is not written where a reader sees it"
        assert "::warning" in block, "a failure is not announced in the run"
