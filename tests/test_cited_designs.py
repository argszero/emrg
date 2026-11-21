"""The guard that asks whether a cited design exists, in all three of its answers.

The subject is `scripts/check-cited-designs.py`, and the reading it exists for was measured
on 2026-11-18: the shipped prompt sends a cycle to a design under the host's home that this
machine does not have, and every existing test passed, because the only arm that names the
citation pins its *spelling* rather than its existence.

Every test here passes a home it created. The host's real home is never read: a guard about
a host-side document is exactly the place where reaching for `Path.home()` would make the
suite's verdict depend on the machine it runs on.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-cited-designs.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_cited_designs", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _templates(root: Path, files: dict[str, str]) -> Path:
    """A checkout whose `emrg/server/` holds the given prompt templates."""
    directory = root / "emrg" / "server"
    directory.mkdir(parents=True)
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")
    return directory


def _home(tmp_path: Path, with_emrg: bool = True) -> Path:
    """A home, optionally one this machine would call an EMRG host."""
    home = tmp_path / "home"
    home.mkdir()
    if with_emrg:
        (home / ".emrg").mkdir()
    return home


def _design(home: Path, name: str, text: str = "# design\n") -> Path:
    target = home / ".emrg" / "global" / "designs" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def test_a_cited_design_that_is_present_reads_clean(tmp_path, capsys):
    mod = _load()
    root = tmp_path / "repo"
    _templates(root, {"p.md": "see `~/.emrg/global/designs/alpha.md` for the rule\n"})
    home = _home(tmp_path)
    _design(home, "alpha.md")

    rc = mod.check(root, home)
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "OK" in out, out
    assert "ABSENT" not in out, f"a present design is not a fault:\n{out}"


def test_an_absent_design_is_named_with_the_line_that_cites_it(tmp_path, capsys):
    """The reader needs to find the rule that depended on the document."""
    mod = _load()
    root = tmp_path / "repo"
    _templates(
        root,
        {"evolution_prompt.md": "first line\nsee `~/.emrg/global/designs/gone.md` §0-§2\n"},
    )
    home = _home(tmp_path)

    rc = mod.check(root, home)
    out = capsys.readouterr().out

    assert rc == 1, f"this machine has an EMRG home and the design is not in it:\n{out}"
    assert "ABSENT" in out, out
    assert "gone.md" in out, out
    assert "evolution_prompt.md:2" in out, f"the citing line is what locates the rule:\n{out}"
    assert "OK" not in out, f"an absent design is never clean:\n{out}"


def test_a_machine_with_no_emrg_home_is_not_measurable(tmp_path, capsys):
    """A CI runner has never run EMRG: absent host documents there say nothing."""
    mod = _load()
    root = tmp_path / "repo"
    _templates(root, {"p.md": "see `~/.emrg/global/designs/alpha.md`\n"})
    home = _home(tmp_path, with_emrg=False)

    rc = mod.check(root, home)
    out = capsys.readouterr().out

    assert rc == 2, f"a runner is not a fault and not a pass:\n{out}"
    assert "could not measure" in out, out
    assert "ABSENT" not in out, f"nothing was asked, so nothing is absent:\n{out}"
    assert "OK" not in out, f"could not measure is never clean:\n{out}"


def test_a_template_that_cites_no_design_is_not_measurable(tmp_path, capsys):
    mod = _load()
    root = tmp_path / "repo"
    _templates(root, {"p.md": "no host document is named here\n"})
    home = _home(tmp_path)

    rc = mod.check(root, home)
    out = capsys.readouterr().out

    assert rc == 2, out
    assert "could not measure" in out, out


def test_a_tree_with_no_templates_is_not_measurable(tmp_path, capsys):
    mod = _load()
    root = tmp_path / "repo"
    root.mkdir()
    home = _home(tmp_path)

    rc = mod.check(root, home)
    out = capsys.readouterr().out

    assert rc == 2, out
    assert "could not measure" in out, out


def test_host_state_the_prompt_also_cites_is_out_of_scope(tmp_path, capsys):
    """The scope limit, pinned: state whose absence is normal is not a fault.

    `~/.emrg/pause` is a file signal that exists only while paused, and a notarization env
    file exists only on a host that signs releases. A sweep over every `~/` path would call
    both of them faults on a healthy machine, which is how a guard becomes noise.
    """
    mod = _load()
    root = tmp_path / "repo"
    _templates(
        root,
        {
            "p.md": (
                "the signal is `.emrg/pause` and `~/.emrg/pause` when paused\n"
                "`~/.emrg/tasks.yml` and `~/.emrg/notary.env` may be absent\n"
                "the blueprint is `~/.emrg/global/designs/alpha.md`\n"
            )
        },
    )
    home = _home(tmp_path)
    _design(home, "alpha.md")

    rc = mod.check(root, home)
    out = capsys.readouterr().out

    assert rc == 0, f"none of those is a design, so none is asked about:\n{out}"
    assert "designs cited: 1 distinct" in out, out


def test_one_design_cited_twice_is_one_design(tmp_path, capsys):
    """The count is of designs, not of mentions, and both places are kept."""
    mod = _load()
    root = tmp_path / "repo"
    _templates(
        root,
        {
            "a.md": "`~/.emrg/global/designs/alpha.md`\n",
            "b.md": "again: `~/.emrg/global/designs/alpha.md`\n",
        },
    )
    home = _home(tmp_path)
    _design(home, "alpha.md")

    rc = mod.check(root, home)
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "1 distinct citation(s) on 2 line(s)" in out, out


def test_the_first_line_names_the_tree_it_read(tmp_path, capsys):
    """Every guard in this family says what it read before it says what it found."""
    mod = _load()
    root = tmp_path / "repo"
    _templates(root, {"p.md": "`~/.emrg/global/designs/alpha.md`\n"})
    home = _home(tmp_path)
    _design(home, "alpha.md")

    mod.check(root, home)
    out = capsys.readouterr().out

    assert out.splitlines()[0] == f"tree: {root}", out
