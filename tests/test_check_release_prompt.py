"""`check-release-prompt.py` answers three ways, and each way is measured here.

The tool asks one question of a shipped product: does it carry the prompt this tree has?
Its contract is the family's — `0` identical, `1` a fault, `2` could not measure — and a
guard whose refusal is untested is a guard whose refusal is a guess. So every direction is
built from a subject this file creates, not from a hosted artifact: a downloaded product
would make these tests depend on the network and on which release happens to be sitting
in a temp directory.

The subjects are made the way the real ones are shaped. The shipped prompt travels at
`runtime/source/emrg/server/prompts/system.j2` inside the Linux runtime archive (measured
on `EMRG-0.3.21-linux-x86_64.tar.gz`), so the pass and fault cases are archives carrying
that member, and the directory case is the same payload expanded — which is also the shape
a `.pkg`'s payload or an AppImage's `squashfs-root` arrives in.

What is deliberately *not* asserted: the content of this tree's prompt. A test that pinned
its length or its first line would redden on every wording change and say nothing about
the tool. The comparison is the tool's job; this file only asks whether it reports the
comparison honestly.
"""

from __future__ import annotations

import importlib.util
import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "scripts" / "check-release-prompt.py"

#: The member every subject carries, spelled the way the real archive spells it.
MEMBER = "runtime/source/emrg/server/prompts/system.j2"

#: The suffix the tool matches on, and this tree's own copy of the file.
RELATIVE = "emrg/server/prompts/system.j2"


def _load():
    spec = importlib.util.spec_from_file_location("check_release_prompt", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool():
    return _load()


def _tree_text() -> str:
    """This tree's copy — the text a passing subject must carry, read from the file."""
    return (REPO_ROOT / RELATIVE).read_text(encoding="utf-8")


def _tarball(path: Path, text: str, member: str = MEMBER) -> Path:
    """A `.tar.gz` carrying `text` at `member`, written byte-for-byte as given."""
    payload = text.encode("utf-8")
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo(member)
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return path


def _directory(root: Path, text: str, member: str = MEMBER) -> Path:
    """The same payload expanded, which is what a `.pkg` or an AppImage arrives as."""
    target = root / member
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return root


def _run(tool, *subjects: Path, extra: list[str] | None = None, capsys) -> tuple[int, str]:
    """Call the tool the way its `__main__` block does, and read what it printed."""
    rc = tool.main([*(extra or []), *[str(s) for s in subjects]])
    return rc, capsys.readouterr().out


def test_a_product_carrying_this_trees_text_answers_zero(tool, tmp_path, capsys):
    """The pass direction: an archive built from this very file."""
    subject = _tarball(tmp_path / "EMRG-0.0.0-linux-x86_64.tar.gz", _tree_text())

    rc, out = _run(tool, subject, capsys=capsys)

    assert rc == 0, out
    assert "identical to this tree" in out
    assert "OK: every product carries" in out


def test_an_expanded_payload_answers_the_same_way_a_tarball_does(tool, tmp_path, capsys):
    """The directory subject: the shape a `.pkg` payload and a `squashfs-root` take.

    Both halves are asserted, because the tool claims they are the same question asked of
    two containers — a directory branch that could only ever fault would pass a
    fault-only test just as well.
    """
    good = _directory(tmp_path / "expanded", _tree_text())
    rc, out = _run(tool, good, capsys=capsys)
    assert rc == 0, out
    assert f"{good}" in out

    stale = _directory(tmp_path / "stale", _tree_text() + "\nan extra line\n")
    rc, out = _run(tool, stale, capsys=capsys)
    assert rc == 1, out


def test_a_product_carrying_a_different_text_is_a_fault(tool, tmp_path, capsys):
    """The fault direction, which is the one the release chain would act on."""
    stale = _tarball(tmp_path / "stale.tar.gz", "an older prompt\n")

    rc, out = _run(tool, stale, capsys=capsys)

    assert rc == 1, out
    assert "DIFFERS from this tree's" in out
    assert "FAULT:" in out


def test_a_fault_prints_both_character_counts(tool, tmp_path, capsys):
    """Two numbers, because "differs" without "how" is a report a reader cannot act on."""
    expected = _tree_text()
    stale = _tarball(tmp_path / "stale.tar.gz", "short\n")

    rc, out = _run(tool, stale, capsys=capsys)

    assert rc == 1, out
    assert f"{len('short\n')} chars" in out
    assert f"{len(expected)} chars" in out


def test_a_fault_names_the_first_differing_line_of_both_sides(tool, tmp_path, capsys):
    """The line number and both spellings, so the reader sees *what* changed.

    Built so the difference is at line **3**, not line 1: a tool that always reported the
    first line would satisfy a difference-at-line-1 fixture, and that is the fixture a
    wrong implementation would also satisfy.
    """
    expected = _tree_text()
    lines = expected.splitlines()
    kept = "\n".join([*lines[:2], "a line neither side shares", *lines[3:]])
    stale = _tarball(tmp_path / "stale.tar.gz", kept)

    rc, out = _run(tool, stale, capsys=capsys)

    assert rc == 1, out
    assert "first difference at line 3" in out
    assert "a line neither side shares" in out


def test_a_subject_whose_member_is_absent_is_unmeasurable_not_identical(tool, tmp_path, capsys):
    """`absent` is not `identical` — the whole point of the tool's third answer."""
    subject = _tarball(tmp_path / "empty.tar.gz", "x", member="runtime/source/other.txt")

    rc, out = _run(tool, subject, capsys=capsys)

    assert rc == 2, out
    assert "carries no member ending in" in out
    assert "UNMEASURED" in out


def test_a_path_that_is_not_there_is_unmeasurable(tool, tmp_path, capsys):
    rc, out = _run(tool, tmp_path / "no-such-product.tar.gz", capsys=capsys)

    assert rc == 2, out
    assert "no such path" in out


def test_a_container_it_does_not_open_names_the_command_that_reaches_one(tool, tmp_path, capsys):
    """A `.pkg` is refused with the extraction that would make it readable.

    The typo-proof half of the key is the case: a real macOS product is named with an
    upper-case suffix, so the lookup lowercases — and this asserts the branch is reached
    rather than falling through to the generic "neither a directory nor an archive".
    """
    for name, expected in (("product.pkg", "pkgutil"), ("EMRG-0.0.0-x86_64.AppImage", "--appimage-extract")):
        subject = tmp_path / name
        subject.write_bytes(b"\x00")

        rc, out = _run(tool, subject, capsys=capsys)

        assert rc == 2, out
        assert expected in out, out
        assert "this tool does not open" in out, out


def test_a_fault_outranks_a_subject_that_could_not_be_read(tool, tmp_path, capsys):
    """One product is stale: the answer is the fault, not "could not measure everything".

    The other order would let an unreadable subject bury a finding about a product that
    *was* read, which is the failure the tool exists to prevent.
    """
    good = _tarball(tmp_path / "good.tar.gz", _tree_text())
    stale = _tarball(tmp_path / "stale.tar.gz", "older\n")

    rc, out = _run(tool, good, stale, tmp_path / "missing.tar.gz", capsys=capsys)

    assert rc == 1, out
    assert "FAULT:" in out
    assert "UNMEASURABLE" in out


def test_an_unreadable_subject_alone_answers_two_even_beside_a_passing_one(tool, tmp_path, capsys):
    """And with no fault, an unmeasured subject is still not a pass."""
    good = _tarball(tmp_path / "good.tar.gz", _tree_text())

    rc, out = _run(tool, good, tmp_path / "missing.tar.gz", capsys=capsys)

    assert rc == 2, out
    assert "OK: every product carries" not in out


def test_the_report_names_the_tree_it_read_before_any_verdict(tool, tmp_path, capsys):
    """The family's convention: a guard that reads a tree says which one, first.

    Asserted against the module's own `REPO`, and against a subject under `tmp_path`, so a
    line that named the caller's cwd would fail here.
    """
    subject = _tarball(tmp_path / "p.tar.gz", _tree_text())

    rc, out = _run(tool, subject, capsys=capsys)

    assert rc == 0, out
    first = out.splitlines()[0]
    assert first == f"tree: {REPO_ROOT}", out


def test_the_default_subject_is_the_file_the_runtime_payload_carries(tool):
    """The default path is a claim about `packaging/build-runtime.sh`, checked against it.

    Two halves, and both are needed: the constant names a file that exists in this tree,
    and that file is the one the build script copies into the payload — the place the
    shipped copy really comes from. A default pointing at any other prompt would report
    `identical` about a file no product carries.
    """
    assert tool.DEFAULT_FILE == RELATIVE
    assert (REPO_ROOT / RELATIVE).is_file()
    build = (REPO_ROOT / "packaging" / "build-runtime.sh").read_text(encoding="utf-8")
    assert "emrg" in build and "source" in build, (
        "packaging/build-runtime.sh no longer copies the package into the payload — the "
        "path this tool reads inside a product was derived from it"
    )


def test_an_unexpected_failure_answers_two_not_one(tool, tmp_path, capsys, monkeypatch):
    """`_entry`'s contract, at this tool: a crash is "could not measure", never a verdict.

    `1` is this tool's FAULT code, so a traceback left to Python's default exit would tell
    a caller a product is stale when the file was never opened. The family-wide leg in
    `tests/test_a_crash_is_a_measurement_error.py` pins the text of `_entry`; this pins
    that *this* tool's crash really comes back as `2`.
    """

    def explode(_relpath: str) -> str:
        raise RuntimeError("the tree copy went missing")

    monkeypatch.setattr(tool, "tree_text", explode)
    monkeypatch.setattr(sys, "argv", [str(TOOL), str(tmp_path / "anything.tar.gz")])

    rc = tool._entry()

    assert rc == 2
    assert "could not measure" in capsys.readouterr().err


def test_a_tar_and_a_tgz_are_both_read_as_archives(tool, tmp_path, capsys):
    """Both suffixes a runtime archive is published with, since one branch decides it.

    `.tar.gz` is read through `Path.suffixes`, which is where an off-by-one in the pair
    check shows up: a branch that only recognised the two-part spelling would send `.tgz`
    down the "neither a directory nor an archive" path, and the message would be about the
    tool rather than the product.
    """
    for name in ("runtime.tar.gz", "runtime.tgz"):
        subject = _tarball(tmp_path / name, _tree_text())
        rc, out = _run(tool, subject, capsys=capsys)
        assert rc == 0, f"{name}: {out}"


# --------------------------------------------------------------------------- the step
#
# The tool's own answers are measured above. What is measured here is that the release job
# *runs* it: a guard wired into a workflow is only as good as the step that invokes it, and
# nothing in this file said so until issue #2411. The gap is not hypothetical — the `v0.3.22`
# release job (run `39373994491`) ran `Assert the artifacts declare the version the tag names`
# and then went straight to `Create GitHub Release`, with no prompt assertion in between,
# because that tag was cut before #2401 landed. Nothing in the repository could report it;
# it took reading the run's step list.
#
# So these read the *parsed job*, never a copy of the text kept here, and the two executed
# arms drive the step's own `run:` block rather than grepping it: an assertion that the text
# "looks right" goes green the moment an edit keeps the words and moves the `exit`.

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-release.yml"

#: The step that invokes this tool, found by the invocation rather than by its wording — a
#: rename is a change to the workflow, not to what this file is about.
INVOCATION = "scripts/check-release-prompt.py"

#: The file that must be named when no product arrived. Spelled as the step spells it.
PATTERN = "EMRG-*-linux-*.tar.gz"

#: `windows-2025` has no `bash` of its own: `C:\Windows\System32\bash.exe` is WSL's
#: launcher, `CreateProcess` finds it before `PATH`, and it answers "Windows Subsystem for
#: Linux has no installed distributions." rather than running a script (measured 2026-10-02,
#: #1812). `shutil.which("bash") is None` is therefore not the gate there — the launcher
#: exists and is found — so the marker carries the platform and the skip below carries the
#: missing shell on a POSIX host. Same shape as the sibling arms in
#: `test_check_release_artifact_version.py` and `test_a_step_without_its_product_refuses.py`.
_WINDOWS = pytest.mark.skipif(
    sys.platform == "win32",
    reason="these arms execute the step's shell text; that step is `shell: bash` on the runners "
           "whose job invokes it, and Windows' `bash` is WSL's launcher",
)


def _release_steps() -> list[dict]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return doc["jobs"]["release"]["steps"]


def _invoking(steps: list[dict]) -> list[int]:
    return [
        index for index, step in enumerate(steps)
        if isinstance(step.get("run"), str) and INVOCATION in step["run"]
    ]


def _the_step() -> dict:
    """The one step that invokes the tool, refusing to guess if that is not the number."""
    steps = _release_steps()
    found = _invoking(steps)
    assert len(found) == 1, (
        f"the release job has to invoke `{INVOCATION}` in exactly one step; found {len(found)} "
        f"step(s). With none, the assertion this guard exists for never runs and a release that "
        f"carries the previous agent prompt is published as success (issue #2411). With two, one "
        f"of them is not being read by anyone."
    )
    return steps[found[0]]


def test_the_release_job_invokes_the_tool_in_exactly_one_step():
    """A tool nobody runs answers nothing — the invocation is the load-bearing line."""
    _the_step()


def test_the_step_runs_after_the_artifacts_arrive_and_before_the_release_exists():
    """Order, on the parsed job: a mismatch found after publication is a host-side incident.

    Both bounds matter and each fails differently. Before `download-artifact` the step has no
    subject at all — `artifacts/` is created by that step, so an earlier placement always
    refuses and would read as a permanently red release. After `Create GitHub Release` the
    product is already published, and the refusal arrives when a host can already install it.
    """
    steps = _release_steps()
    index = _invoking(steps)[0]
    download = [i for i, s in enumerate(steps)
                if str(s.get("uses", "")).startswith("actions/download-artifact")]
    assert download, "the release job downloads no artifacts, so this step has no subject"
    assert index > max(download), (
        "the prompt assertion runs before the artifacts are downloaded: `artifacts/` is created "
        "by that step, so this one would refuse on every release"
    )
    names = [step.get("name") or step.get("uses") or "" for step in steps]
    assert "Create GitHub Release" in names, (
        "the release job no longer has a step named 'Create GitHub Release', so the order this "
        "test is about cannot be read anymore — re-derive it rather than deleting the check"
    )
    assert index < names.index("Create GitHub Release"), (
        "the prompt assertion runs after the release is created: a product carrying the previous "
        "agent prompt would be published before anything reported the mismatch"
    )


def test_the_step_needs_no_uv_and_no_network():
    """The release job installs nothing, so `uv run` there fails the build it guards."""
    body = _the_step()["run"]
    assert "uv " not in body, (
        "the step invokes `uv`, and the release job has no uv environment to run it in"
    )
    assert f"python3 {INVOCATION}" in body, (
        f"the step no longer invokes `python3 {INVOCATION}` — it may call something else under "
        "the same name, which this file cannot vouch for"
    )


def _run_step(
    tmp_path: Path, *, product: str | None, text: str | None = None
) -> subprocess.CompletedProcess:
    """Execute the step's own `run:` block in a tree shaped like the release job's.

    `artifacts/` is the directory `download-artifact` writes, and `scripts/<tool>` is a
    **symlink** to the real tool: it resolves `__file__` to its real path, so the step compares
    against this tree's copy rather than against nothing — copying the file instead would make
    the tool's own `REPO` point into the temporary tree and answer `2` for a reason that is not
    the step's.

    With `product` given, a real archive is written carrying `text` — this tree's copy by
    default, so the passing direction is a passing *comparison* rather than merely a step that
    did not print a refusal. A caller that passes `text` supplies a product whose prompt
    differs, which is the one direction the step's own verdict exists for.
    """
    if shutil.which("bash") is None:  # pragma: no cover - a POSIX-less host
        pytest.skip("no bash on PATH: these arms drive the step's shell text, which they cannot run")

    (tmp_path / "artifacts").mkdir(parents=True, exist_ok=True)
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / TOOL.name).symlink_to(TOOL)
    if product is not None:
        _tarball(tmp_path / "artifacts" / product, _tree_text() if text is None else text)

    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _the_step()["run"]],
        cwd=str(tmp_path), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


@_WINDOWS
def test_the_step_refuses_when_no_product_arrived(tmp_path):
    """Issue #1913's family, on the step that guards the prompt: absent subject → refuse.

    The step before this one in the job located its product and `exit 0`-ed with
    `no pkg found, skipping`, so a tag run could publish a release with no macOS product and
    every step reported success. The same edit here would let a release ship with no readable
    product at all. So the assertion is on the exit status *and* on a refusal naming the
    pattern: a bare non-zero would be satisfied by any failure, including one that says
    nothing a reader can act on.
    """
    result = _run_step(tmp_path, product=None)
    assert result.returncode != 0, (
        "the step exited 0 with an empty `artifacts/`: a release whose product could not be "
        f"read was reported as success\n{result.stdout[-800:]}"
    )
    assert "::error::" in result.stdout, (
        f"the step refused without an ::error:: line, so CI shows no reason\n{result.stdout[-800:]}"
    )
    assert PATTERN in result.stdout, (
        f"the refusal does not name {PATTERN!r}, so a reader cannot tell what was missing\n"
        f"{result.stdout[-800:]}"
    )


@_WINDOWS
def test_the_step_passes_when_the_correct_product_arrived(tmp_path):
    """The other direction: a guard that refuses everything is not measuring its subject."""
    result = _run_step(tmp_path, product="EMRG-0.3.99-linux-x86_64.tar.gz")
    assert PATTERN not in result.stdout, (
        "the step reported the absent-product refusal while a matching product was present, so "
        f"it is not reading `artifacts/`\n{result.stdout[-800:]}"
    )
    assert result.returncode == 0, (
        "a product carrying exactly this tree's prompt failed the step\n"
        f"{result.stdout[-1200:]}\n{result.stderr[-800:]}"
    )


@_WINDOWS
def test_the_step_refuses_when_the_product_carries_a_different_prompt(tmp_path):
    """The direction the step exists for, and the one nothing reached until issue #2411.

    Both arms above stop short of it, and structurally so rather than by oversight. The
    absent-product arm is satisfied by the **early** `exit 1` in the empty-candidates branch,
    which fires before any verdict is produced; the passing arm only ever exercises `rc=0`. So
    the branch taken when the tool answers `1` — a product that arrived and carries the previous
    agent prompt — was executed by nothing, and changing the step's last line from `exit "$rc"`
    to `exit 0` left this whole file green while every release would publish the wrong prompt as
    success. Measured on `f9dcb2cf` by two hosts independently before this arm existed.

    Asserted on both halves, for the reason the second host gave: a bare non-zero exit would
    still pass an edit that made the step refuse every product for an unrelated reason, so the
    refusal has to be the *mismatch* refusal and it has to name the file.
    """
    differs = _tree_text() + "\n# a line this product does not ship\n"
    result = _run_step(
        tmp_path, product="EMRG-0.3.99-linux-x86_64.tar.gz", text=differs
    )
    assert result.returncode != 0, (
        "a product carrying a different prompt exited 0: the release job reported success on an "
        f"installer that hands a host the previous agent prompt\n{result.stdout[-1200:]}"
    )
    assert "::error::" in result.stdout, (
        f"the step refused without an ::error:: line, so CI shows no reason\n{result.stdout[-1200:]}"
    )
    assert "carries a different" in result.stdout, (
        "the failure is not the mismatch refusal — a step that refuses for another reason would "
        f"satisfy a bare non-zero exit\n{result.stdout[-1200:]}"
    )
    assert RELATIVE in result.stdout, (
        f"the refusal does not name {RELATIVE!r}, so a reader cannot tell which file differs\n"
        f"{result.stdout[-1200:]}"
    )
