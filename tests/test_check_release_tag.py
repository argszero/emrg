"""`scripts/check-release-tag.py` — a release tag must name the tree's version.

Measured defect (issue #1652, 2026-09-27): the release pipeline takes a release's
*name* from the tag and its artifacts' `version.txt` from the built tree
(`packaging/build-runtime.sh` prints `emrg.__version__`), and nothing compared the
two. A tag cut on the wrong commit therefore publishes a release whose halves
disagree, and every host that installs it re-triggers the upgrade session every
tick — `UpgradeManager.tick` compares `version.txt` with `target.lstrip("v")` —
while the GitHub side of the release looks entirely correct.

The pins below are deliberately of three kinds, because each is blind where the
others see (the shape `tests/test_release_tag_form.py` uses for the annotated-tag
rule):

* the **verdict**, executed in both states under `tmp_path` — a mismatch must be
  refused *and* both sides named, and an unreadable declaration must never read as
  agreement;
* the **reading's identity** — the guard must strip the `v` the way the chain
  strips it (`lstrip`, not one prefix), because a guard that disagrees with the
  chain about what a tag means would measure a release nobody can install;
* the **wiring and the premise** — the workflow must ask *before* any platform
  builds, and `version.txt` must still come from the tree, which is the claim the
  whole comparison rests on.

Everything runs against synthetic trees under `tmp_path`; nothing here reads the
real repository's version or the network.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-release-tag.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-release.yml"


def _run(tag: str, root: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), tag, "--root", str(root)],
        capture_output=True,
        text=True,
        # Pinned, not inherited from the locale: the guard's output is UTF-8, and a
        # cp936/cp1252 host would otherwise decode it wrongly (issue #1132's class).
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout + proc.stderr


def _tree(root: Path, body: str) -> Path:
    """A synthetic checkout whose only load-bearing file is the declaration."""
    (root / "emrg").mkdir(parents=True, exist_ok=True)
    (root / "emrg" / "__init__.py").write_text(body, encoding="utf-8")
    return root


def _declaring(root: Path, version: str) -> Path:
    return _tree(root, f'"""pkg."""\n\n__version__ = "{version}"\n')


# ── The verdict, in both states ───────────────────────────────────────────────


def test_a_tag_that_names_the_declared_version_passes(tmp_path: Path) -> None:
    """Positive state: the guard must be able to say OK, or a red is unreadable."""
    code, out = _run("v1.2.3", _declaring(tmp_path, "1.2.3"))
    assert code == 0, out
    assert "1.2.3" in out, out
    assert "OK" in out, out


def test_a_tag_naming_another_version_is_refused_and_both_are_named(
    tmp_path: Path,
) -> None:
    """The measured defect: v1.2.4 over a tree that declares 1.2.3.

    Both spellings must appear — a refusal that prints only one of them cannot
    tell the host which half to change.
    """
    code, out = _run("v1.2.4", _declaring(tmp_path, "1.2.3"))
    assert code == 1, out
    assert "v1.2.4" in out, out
    assert "1.2.3" in out, out
    assert "MISMATCH" in out, out


def test_the_tree_it_read_is_named_before_the_verdict(tmp_path: Path) -> None:
    """The family's rule: a guard that reads a tree names it before its verdict."""
    code, out = _run("v1.2.3", _declaring(tmp_path, "1.2.3"))
    assert code == 0, out
    assert str(tmp_path.resolve()) in out, out
    assert out.index(str(tmp_path.resolve())) < out.index("OK"), out


# ── An unreadable declaration is never a pass ─────────────────────────────────


@pytest.mark.parametrize(
    "body, why",
    [
        ("", "an empty file declares nothing"),
        ('"""pkg."""\n\nNOT_A_VERSION = "1.2.3"\n', "no declaration at all"),
        ("__version__ = 1.2.3\n", "the value is not a quoted string"),
        ('__version__ = "1.2.3"\n__version__ = "1.2.4"\n', "two declarations name two versions"),
        ('__version__ = ""\n', "an empty version is not a version"),
    ],
)
def test_an_unreadable_declaration_is_unmeasurable_not_a_pass(
    tmp_path: Path, body: str, why: str
) -> None:
    """Exit 2, never 0 — and the tag alone must not be enough to pass.

    A guard that answered "OK" because it could not read its subject would be the
    exact defect it exists to catch, so each of these trees is asked for a tag
    that *would* match the intended version if the declaration were readable.
    """
    code, out = _run("v1.2.3", _tree(tmp_path, body))
    assert code == 2, f"{why}: {out}"
    assert "not measurable" in out, out


def test_the_declaration_must_be_anchored_so_prose_is_not_read_as_one(
    tmp_path: Path,
) -> None:
    """A sentence mentioning `__version__ = "9.9.9"` is not a declaration.

    The unanchored read is the trap: it would take the prose (2.0.0 below) and
    refuse the tag that actually matches this tree.
    """
    body = (
        '"""pkg."""\n'
        "\n"
        "# Historically this was written `__version__ = \"9.9.9\"`; the real one follows.\n"
        '__version__ = "1.2.3"\n'
    )
    code, out = _run("v1.2.3", _tree(tmp_path, body))
    assert code == 0, out
    assert "1.2.3" in out and "9.9.9" not in out, out


# ── The reading's identity: the chain's own expression ────────────────────────


def test_the_guard_strips_the_v_the_way_the_upgrade_chain_does(tmp_path: Path) -> None:
    """`lstrip("v")`, not `removeprefix("v")` — the two differ on `vv1.2.3`.

    The chain reads `target.lstrip("v")`, so the guard must too; a guard that used
    the single-prefix spelling would refuse a tag a host can install.
    """
    code, out = _run("vv1.2.3", _declaring(tmp_path, "1.2.3"))
    assert code == 0, out


def test_the_chain_still_compares_the_way_this_guard_assumes() -> None:
    """The premise, read from the source that owns it.

    If `UpgradeManager.tick` ever stops comparing `version.txt` with the tag, the
    consequence this guard is built on disappears — and the guard, not the chain,
    is what would then be wrong.
    """
    source = (REPO_ROOT / "emrg" / "server" / "upgrade.py").read_text(encoding="utf-8")
    assert 'target.lstrip("v")' in source, (
        "emrg/server/upgrade.py no longer compares version.txt with "
        'target.lstrip("v") — re-derive what a mismatched tag costs before '
        "trusting the guard's docstring"
    )


def test_version_txt_is_still_derived_from_the_built_tree() -> None:
    """The premise's other half: `version.txt` comes from the tree, not the tag."""
    script = (REPO_ROOT / "packaging" / "build-runtime.sh").read_text(encoding="utf-8")
    line = next(
        (ln for ln in script.splitlines() if "version.txt" in ln and "__version__" in ln),
        None,
    )
    assert line is not None, (
        "packaging/build-runtime.sh no longer derives version.txt from the built "
        "tree's emrg.__version__ — the tag/version comparison rests on that"
    )


# ── The wiring: the workflow must ask before any platform builds ──────────────


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def test_verify_tag_runs_the_guard_on_the_tag() -> None:
    """Read from the *parsed job*, not the document (the sibling's lesson)."""
    job = _jobs()["verify-tag"]
    runs = [str(step.get("run", "")) for step in job["steps"]]
    invoking = [r for r in runs if "check-release-tag.py" in r]
    assert invoking, (
        "build-release.yml's verify-tag job no longer invokes "
        "scripts/check-release-tag.py — a tag/version mismatch would then only be "
        "found by a host that has already installed the release"
    )
    assert any("GITHUB_REF_NAME" in r for r in invoking), (
        "the guard is invoked without the tag being released: it must be asked "
        "about ${GITHUB_REF_NAME}, not about a version written into the workflow"
    )


def test_the_guard_asks_before_the_build_not_after() -> None:
    """The job that checks the tag is the job `build` waits on."""
    jobs = _jobs()
    needs = jobs["build"]["needs"]
    needs = [needs] if isinstance(needs, str) else list(needs)
    assert "verify-tag" in needs, (
        "build no longer waits on verify-tag, so a mis-tagged tree would be built "
        "on every platform (and signed) before anything refused it"
    )


def test_the_tag_check_runs_as_a_plain_python_step() -> None:
    """No `|| true`, no `continue-on-error`: exit 2 has to fail this job too.

    A step that swallows the guard's verdict is the decoration this test exists to
    catch — and it is invisible in the guard's own tests, which never read the
    workflow.
    """
    job = _jobs()["verify-tag"]
    step = next(
        s for s in job["steps"] if "check-release-tag.py" in str(s.get("run", ""))
    )
    assert step.get("continue-on-error") in (None, False), (
        "the tag check may not continue on error — an unmeasurable or mismatched "
        "tag must stop the build"
    )
    assert "|| true" not in str(step["run"]), (
        "the tag check's exit code is being discarded"
    )


# ── The instruction carriers, which are what produce the tag ─────────────────


def test_the_releasing_instruction_names_the_guard() -> None:
    """The command that produced `v0.2.97` was the one the repository taught.

    Same lesson as the annotated form: the instruction must carry the check, or the
    artifact is produced without it. The carrier is the Releasing section's own
    "host-side counterparts" line rather than the Tag step, because `Agent.md` is at the
    8000-char `PROJECT_CONTEXT_MAX_CHARS` cap the daemon keeps
    (`tests/test_agent_md_prompt_cap.py`): the line is where this file names the host
    commands that pre-empt a CI failure, so the check goes there instead of extending
    the file past the point where its tail stops reaching the model.
    """
    agent = (REPO_ROOT / "Agent.md").read_text(encoding="utf-8")
    releasing = agent.split("## Releasing", 1)[1].split("## Packaging", 1)[0]
    assert "check-release-tag.py" in releasing, (
        "Agent.md's Releasing section no longer names scripts/check-release-tag.py - "
        "the host would tag without the pre-flight the workflow now performs"
    )


def test_the_bump_tool_names_the_guard_in_both_of_its_surfaces() -> None:
    """`scripts/bump-version.py` teaches the flow twice: docstring and print."""
    tool = (REPO_ROOT / "scripts" / "bump-version.py").read_text(encoding="utf-8")
    assert tool.count("check-release-tag.py") >= 2, (
        "both of bump-version.py's instruction surfaces (the module docstring and "
        "the printed next-steps block) must name the tag check, or one of them "
        "drifts back to teaching the bare `git tag`"
    )
