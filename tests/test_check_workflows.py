"""`scripts/check-workflows.py` - the host-side counterpart of the actionlint CI gate.

What this file is about, and the leg each test carries
-----------------------------------------------------
The tool exists because the convention ("a CI check needs a host-side counterpart") was
unmet for the one gate that guards workflow files, and because the command the loop
instructs - `actionlint .github/workflows/*.yml` - is **not on this host's PATH**
(measured 2026-10-04, `cyc20261004-084338`: neither is shellcheck, node or npm, while
`brew` and `docker` are). So the interesting question is not "does it lint" but "does it
ever answer *yes* without having asked, and *no* without saying so" - the two ways an
instrument of this family goes wrong:

* `test_no_actionlint_on_path_is_not_measurable` - the host without the binary gets
  `could not measure` (rc 2) plus the install line. The defect this file was written
  against is an instruction that answers nothing while looking like it answered, so the
  missing-binary case is the first thing to pin.
* `test_a_different_build_that_found_nothing_is_not_a_pass` - the CI gate runs a
  **pinned** version, so another build's clean answer is not that gate's verdict. This is
  the tool's one piece of judgement beyond "run the tool": the pin comes from the
  workflow, the local version from the binary, and a disagreement is unmeasurable rather
  than green.
* `test_a_finding_from_a_different_build_is_still_a_finding` - the control on the leg
  above, in the other direction: the agreement rule must not be able to suppress a real
  finding, or "wrong version" would become a way to make failures disappear.

Everything is driven through two module seams - `_which` (the binary) and `_run` (its
invocations) - so no test needs actionlint installed, and no test depends on PATH, on a
`shebang`, or on the ability to make a file executable in a `tmp_path` (which is exactly
where a POSIX assumption would pass here and fail the `test-windows` leg). The one
end-to-end run is the tree-naming test below, which needs no binary at all.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-workflows.py"
DEV = REPO_ROOT / "DEVELOPMENT.md"

#: A minimal workflow file. The tool reads these for the `uses:` line and hands them to
#: actionlint; nothing else about them matters to it.
_CLEAN_YML = "name: t\non: push\njobs:\n  g:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: rhysd/actionlint@v1.7.12\n"
_RETIRED_YML = "name: t\non: push\njobs:\n  g:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_workflows", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


class FakeActionlint:
    """`_which` + `_run` replaced: a binary that answers whatever the test says.

    The seam is the module's own, not `subprocess.run`, because `mod.subprocess` is the
    shared stdlib module - replacing its attribute would replace `subprocess.run` for
    everything else alive in the process. Asserting on the argv here is the point: what
    the tool passes to actionlint is the claim under test.
    """

    def __init__(self, version="1.7.12", rc=0, output="", version_rc=0, on_path=True):
        self.version = version
        self.rc = rc
        self.output = output
        self.version_rc = version_rc
        self.on_path = on_path
        self.calls: list[list[str]] = []
        self.cwds: list[str] = []

    def which(self, name):
        assert name == "actionlint", name
        return "/usr/local/bin/actionlint" if self.on_path else None

    def run(self, argv, cwd):
        self.calls.append(list(argv))
        self.cwds.append(str(cwd))
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, self.version_rc, f"{self.version}\n", "")
        return subprocess.CompletedProcess(argv, self.rc, self.output, "")


def _install(mod, monkeypatch, fake: FakeActionlint) -> FakeActionlint:
    monkeypatch.setattr(mod, "_which", fake.which)
    monkeypatch.setattr(mod, "_run", fake.run)
    return fake


def _tree(tmp_path: Path, name: str = "test.yml", text: str = _CLEAN_YML,
          extra: dict[str, str] | None = None) -> Path:
    directory = tmp_path / ".github" / "workflows"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")
    for filename, body in (extra or {}).items():
        (directory / filename).write_text(body, encoding="utf-8")
    return tmp_path


def _run_main(mod, capsys, root: Path, *args: str) -> tuple[int, str, str]:
    code = mod.main(["--root", str(root), *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- the tree is named, first -------------------------------------------------


def test_the_tree_it_read_is_named_first(tmp_path) -> None:
    """Run for real, as a subprocess: the convention is about the output, not the source.

    No fake is possible here and none is needed - the tool prints the tree before it
    looks for a binary, so the assertion holds on a host with actionlint and on one
    without (this one). `--root` is spelled so the test states which tree it means
    instead of inheriting the suite's cwd.
    """
    tree = _tree(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tree)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    first = (proc.stdout or "").splitlines()[:1]
    assert first == [f"tree: {tree.resolve()}"], (
        f"the first line of stdout must name the tree that answered, got {first!r}"
    )


def test_the_tree_line_precedes_the_verdict_under_a_pipe(tmp_path) -> None:
    """The family's ordering promise, measured in the one reading mode it is made for.

    stdout is block-buffered under a pipe while stderr is not, so a gate that prints its
    identity line to stdout and its verdict to stderr can hand a merged reader the verdict
    first - which is the shape a cycle uses (`2>&1`), and the shape this file pins in
    `tests/test_guard_report.py` for the whole family. The case is reachable without
    actionlint installed, which is the state of this host, so the assertion is about a run
    that really happens here rather than about a fixture.
    """
    tree = _tree(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tree)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    assert lines and lines[0] == f"tree: {tree.resolve()}", (
        "the identity line must reach a merged reader first; got " + repr(lines[:2])
    )


def test_the_real_checkout_pins_actionlint_in_its_own_workflow() -> None:
    """The pin is read from the tree, and this tree has one - the tool's premise.

    Asserted against the file rather than a literal, because a literal here would be the
    second copy of the version this script exists to avoid: if the gate is moved or the
    pin is bumped, the reader of this test should see which file says so.
    """
    mod = _load_module()
    pin = mod.pinned_version(REPO_ROOT)
    assert pin, (
        "no workflow in this checkout runs `rhysd/actionlint`, so check-workflows.py's "
        "question has no gate to be the counterpart of"
    )
    text = (REPO_ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    assert f"rhysd/actionlint@v{pin}" in text, (
        f"the tool read {pin!r} from somewhere other than the workflow that runs the gate"
    )


# --- the three verdicts -------------------------------------------------------


def test_a_clean_run_from_the_pinned_build_is_exit_0(mod, monkeypatch, capsys, tmp_path) -> None:
    """The working case: both versions are printed, so the answer names its instrument."""
    fake = _install(mod, monkeypatch, FakeActionlint(version="1.7.12"))
    code, out, err = _run_main(mod, capsys, _tree(tmp_path))
    assert code == 0, err
    assert "OK: actionlint 1.7.12 reports no problem" in out
    assert "pins v1.7.12" in out, "the version it agreed with must be visible, not implied"


def test_a_finding_is_exit_1_and_its_output_is_quoted(mod, monkeypatch, capsys, tmp_path) -> None:
    """A finding is reported with actionlint's own words, not a summary of them."""
    offline = "test.yml:3:9: property \"secrets\" is not allowed here [expression]"
    fake = _install(mod, monkeypatch, FakeActionlint(rc=1, output=offline))
    code, out, _ = _run_main(mod, capsys, _tree(tmp_path))
    assert code == 1
    assert "FAIL: actionlint 1.7.12 reports problem(s)" in out
    assert offline in out, "the tool must quote the finding rather than paraphrase it"


def test_a_different_build_that_found_nothing_is_not_a_pass(
    mod, monkeypatch, capsys, tmp_path
) -> None:
    """The pin is what makes a local answer about CI, so agreement is the gate for rc 0."""
    _install(mod, monkeypatch, FakeActionlint(version="1.7.10"))
    code, out, err = _run_main(mod, capsys, _tree(tmp_path))
    assert code == 2, "a different build's clean answer is not the pinned gate's verdict"
    assert "not measurable" in err
    assert "1.7.10" in err and "1.7.12" in err, (
        "the reader has to see both builds to know which one answered"
    )
    assert "OK:" not in out, "an unmeasured run must not print the success line"


def test_a_finding_from_a_different_build_is_still_a_finding(
    mod, monkeypatch, capsys, tmp_path
) -> None:
    """The control on the leg above: the agreement rule must not swallow real findings.

    Without this the version check would be a way to make failures vanish - a mutated
    tool that turned every run into `not measurable` would live happily beside a suite
    that only pinned the green path.
    """
    _install(mod, monkeypatch, FakeActionlint(version="1.7.10", rc=1, output="boom"))
    code, out, _ = _run_main(mod, capsys, _tree(tmp_path))
    assert code == 1
    assert "boom" in out


# --- the ways it cannot measure -----------------------------------------------


def test_no_actionlint_on_path_is_not_measurable(mod, monkeypatch, capsys, tmp_path) -> None:
    """The measured state of this host, and the reason the tool exists at all.

    An absent binary produces no output, and "no output" is exactly what a clean run
    produces - so the distinction has to be made here or nowhere.

    Asserted on stdout *being the tree line and nothing else*, not on the word
    "actionlint" being absent from it: the first version of this test did the latter and
    failed, because `tmp_path` carries this test's own name and the name contains the
    word. A substring probe that the fixture's own path can satisfy measures the fixture.
    """
    _install(mod, monkeypatch, FakeActionlint(on_path=False))
    code, out, err = _run_main(mod, capsys, _tree(tmp_path))
    assert code == 2
    assert "not measurable" in err and "not on PATH" in err
    assert "brew install actionlint" in err, "an unmeasurable answer owes the remedy"
    assert "v1.7.12" in err, "the version the host has to match belongs in the remedy"
    assert [line for line in out.splitlines() if line.strip()] == [
        f"tree: {tmp_path.resolve()}"
    ], "stdout may carry the tree line and nothing that could read as a verdict"


def test_a_tree_with_no_workflow_file_is_not_measurable(mod, monkeypatch, capsys, tmp_path) -> None:
    """An empty subject is not a clean subject."""
    _install(mod, monkeypatch, FakeActionlint())
    code, _, err = _run_main(mod, capsys, tmp_path)
    assert code == 2
    assert "no workflow file" in err


def test_a_tree_whose_workflows_run_no_actionlint_is_not_measurable(
    mod, monkeypatch, capsys, tmp_path
) -> None:
    """No pin means no gate to be the counterpart of - said out loud, not defaulted."""
    _install(mod, monkeypatch, FakeActionlint())
    code, _, err = _run_main(mod, capsys, _tree(tmp_path, text=_RETIRED_YML))
    assert code == 2
    assert "runs `rhysd/actionlint`" in err


def test_a_binary_that_cannot_report_its_version_is_not_measurable(
    mod, monkeypatch, capsys, tmp_path
) -> None:
    """`--version` failing means the verdict cannot be attributed to a known build."""
    _install(mod, monkeypatch, FakeActionlint(version_rc=1))
    code, _, err = _run_main(mod, capsys, _tree(tmp_path))
    assert code == 2
    assert "could not report its own version" in err


# --- what it hands the tool ---------------------------------------------------


def test_every_workflow_file_is_linted_and_the_run_happens_in_the_tree(
    mod, monkeypatch, capsys, tmp_path
) -> None:
    """Both spellings, all files, one invocation, from the tree's own root.

    Linting one file would leave an unlinted sibling in the same directory, and running
    from the caller's cwd would make actionlint's paths resolve against the wrong tree -
    the same "which tree answered" failure the `tree:` convention exists for.
    """
    tree = _tree(tmp_path, extra={"other.yaml": _CLEAN_YML})
    fake = _install(mod, monkeypatch, FakeActionlint())
    code, out, _ = _run_main(mod, capsys, tree)
    assert code == 0
    linted = [call for call in fake.calls if "--version" not in call]
    assert len(linted) == 1, fake.calls
    assert linted[0][0].endswith("actionlint")
    assert linted[0][1:] == [str(tree / ".github" / "workflows" / name)
                             for name in ("other.yaml", "test.yml")], linted[0]
    assert fake.cwds[-1] == str(tree.resolve())
    assert "2 file(s)" in out


def test_the_pin_comes_from_the_tree_and_not_from_this_script(
    mod, monkeypatch, capsys, tmp_path
) -> None:
    """A tree pinning something else must be judged against that pin.

    The control is the negative: the version this script was written against must not
    appear anywhere in the answer about a tree that pins a different one, which is what a
    second, hardcoded copy of the version would do.
    """
    tree = _tree(tmp_path, text=_CLEAN_YML.replace("v1.7.12", "v9.9.9"))
    _install(mod, monkeypatch, FakeActionlint(version="9.9.9"))
    code, out, err = _run_main(mod, capsys, tree)
    assert code == 0, err
    assert "pins v9.9.9" in out
    assert "1.7.12" not in out


def test_json_is_one_document_carrying_the_reason(mod, monkeypatch, capsys, tmp_path) -> None:
    """The machine-readable form of an unmeasured run: no prose, no exit code guessing."""
    _install(mod, monkeypatch, FakeActionlint(on_path=False))
    code, out, _ = _run_main(mod, capsys, _tree(tmp_path), "--json")
    assert code == 2
    payload = json.loads(out)
    assert payload["measured"] is False
    assert payload["tree"] == str(tmp_path.resolve())
    assert "not on PATH" in payload["reason"]


# --- the host has to be able to find it ---------------------------------------


def test_development_md_documents_the_counterpart_and_its_contract() -> None:
    """The half that decays: a host-side counterpart nobody can reach is prose.

    Measured 2026-10-04: this host has no actionlint (nor shellcheck, node or npm), so
    the loop's instructed `actionlint .github/workflows/*.yml` answers nothing here - and
    the document is where a host looks. Exit 2 is pinned with the command because the
    contract is what a bare path cannot carry: folding "could not measure" into a pass is
    the defect this tool was written against.
    """
    text = DEV.read_text(encoding="utf-8")
    assert "scripts/check-workflows.py" in text, (
        "DEVELOPMENT.md does not name scripts/check-workflows.py, so the CI gate that "
        "guards workflow files still has no reachable host-side counterpart"
    )
    assert "actionlint" in text
    assert "never a pass" in text, (
        "the documented exit code 2 is stated without its contract"
    )
