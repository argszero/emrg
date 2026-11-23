"""The interval reading: `scripts/check-task-interval.py`.

What this file holds
--------------------
The tool answers one question - does a task block state the pace the competition template
requires? - and it exists because that question was answered by eye every cycle and came
out wrong twice inside one cycle (issue #3171). So the assertions here are the shapes that
made the hand reading wrong, plus the boundaries between the verdicts:

* **the first `interval = N` a block states is not its answer** - a block writes its
  no-allowance fallback before the value it computed, and a reading that takes the first
  match reports the fallback. The block's own value is the last one, and the trap is built
  from a block that carries both, so a tool reading either end of the list is caught.
* **the word `floor(` decides nothing** - a block that writes it and divides evenly is
  ``divides evenly``, not a fault, and a block that never writes it but states the
  truncated number **is** the fault. Both directions are asserted, because on the host this
  was written for the two sets were disjoint and a report built on the word named the wrong
  blocks.
* **a value that is neither neighbour of the division is not a defect** - a host order may
  cap the pace, and the template's precedence rule governs that case rather than the
  arithmetic. It is its own verdict and it does not fail the run; counting it as agreement
  would hide an order, and counting it as a fault would report a correct block.
* **a reading that could not be taken is never a pass** - a table that is missing, that does
  not parse, or a block that states no interval answers exit **2** with the reason named,
  and `0` is reserved for a run that read every block and found none short.
* **it measures and never repairs** - the table's bytes are read before and after a run,
  because a tool that rewrote the host's task table would be editing the file this reading
  exists to describe.

Loading
-------
By path (`spec_from_file_location`), the shape `test_check_memory_rotation.py` uses: the
filename is not importable by name (the dash).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-task-interval.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_task_interval", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


mod = _load()


#: A record whose block states an allowance and a pace, in the running-text shape the host
#: table uses: the values are found by name, not by line position.
def _record(name: str, block: str, *, in_config: bool = True) -> dict:
    if in_config:
        return {"name": name, "config": {"extra_prompt": block}}
    return {"name": name, "config": {}, "extra_prompt": block}


def _table(tmp_path: Path, records: list[dict]) -> Path:
    import yaml

    path = tmp_path / "tasks.yml"
    path.write_text(yaml.safe_dump(records, allow_unicode=True), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- the four verdicts


def test_an_exact_division_is_not_a_fault_whatever_the_block_writes():
    """86400 / (5 + 1) is exact, so the word `floor` cannot change the answer."""
    n, stated, expected, verdict, _ = mod.classify("n = 5 ... interval = floor(86400 / (n+1)) ... interval = 14400")
    assert (n, stated, expected) == (5, 14400, 14400)
    assert verdict == mod.EVEN


def test_the_floor_of_a_non_exact_division_is_the_defect():
    """86400 / 11 is 7854.54..., the template rounds up to 7855, and 7854 is its floor."""
    _, stated, expected, verdict, _ = mod.classify("n = 10 ... interval = 7854")
    assert (stated, expected) == (7854, 7855)
    assert verdict == mod.SHORT


def test_the_ceiling_of_a_non_exact_division_is_what_the_template_asks_for():
    _, _, expected, verdict, _ = mod.classify("n = 10 ... interval = 7855")
    assert expected == 7855
    assert verdict == mod.UP


def test_neither_neighbour_of_the_division_is_its_own_verdict():
    """A host cap is not the formula's result, and it is not a fault either."""
    _, stated, expected, verdict, why = mod.classify("n = 10 ... interval = 43200")
    assert (stated, expected) == (43200, 7855)
    assert verdict == mod.OTHER
    assert "neither" in why


def test_a_truncating_block_that_never_writes_the_word_is_still_caught():
    """The set of blocks that write `floor(` and the set that lose a second are disjoint."""
    worded = "n = 5 ... interval = floor(86400 / (n+1)) ... interval = 14400"
    silent = "n = 10 ... interval = 7854"
    assert "floor(" in worded and mod.classify(worded)[3] == mod.EVEN
    assert "floor(" not in silent and mod.classify(silent)[3] == mod.SHORT


# ------------------------------------------------------------------- the fallback-sentence trap


def test_the_blocks_own_value_is_the_last_one_it_states():
    """A no-allowance fallback is stated first; reading it as the answer is the trap."""
    block = "no daily allowance -> interval = 86400. This competition: n = 10 -> interval = 7854"
    _, stated, _, verdict, _ = mod.classify(block)
    assert stated == 7854, "the first match is the fallback, not this block's pace"
    assert verdict == mod.SHORT


def test_a_block_with_no_allowance_and_a_day_is_exact():
    n, stated, expected, verdict, _ = mod.classify("no daily allowance -> interval = 86400")
    assert (n, stated, expected) == (None, 86400, 86400)
    assert verdict == mod.EVEN


def test_a_block_with_no_allowance_and_another_number_is_not_called_a_fault():
    n, stated, expected, verdict, _ = mod.classify("no daily allowance, host capped it -> interval = 28800")
    assert (n, stated, expected) == (None, 28800, 86400)
    assert verdict == mod.OTHER


# ------------------------------------------------------------------------- unmeasurable is not 0


def test_a_block_that_states_no_interval_could_not_be_measured():
    _, stated, _, verdict, why = mod.classify("n = 5 but no pace written at all")
    assert stated is None
    assert verdict == mod.UNREADABLE
    assert "no pace" in why


def test_a_missing_table_is_exit_2_and_says_so(tmp_path, capsys):
    code = mod.main(["--tasks", str(tmp_path / "nope.yml")])
    out = capsys.readouterr().out
    assert code == mod.EXIT_UNMEASURABLE
    assert "could not measure" in out


# ---------------------------------------------------- the default follows the host it asks about


def _home(tmp_path: Path, monkeypatch, *, migrated: bool) -> Path:
    """A stand-in home holding the task table in one of the two spellings the split left."""
    home = tmp_path / "home"
    if migrated:
        (home / ".emrg" / "global").mkdir(parents=True)
        (home / ".emrg" / "global" / "tasks.yml").write_text(
            "- name: t\n  config:\n    extra_prompt: 'allowance n = 1, interval = 43200'\n",
            encoding="utf-8",
        )
    else:
        (home / ".emrg").mkdir(parents=True)
        (home / ".emrg" / "tasks.yml").write_text(
            "- name: t\n  config:\n    extra_prompt: 'allowance n = 1, interval = 43200'\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("HOME", str(home))
    return home


def test_the_default_is_the_table_a_migrated_host_keeps(tmp_path, monkeypatch, capsys):
    """The daemon reads `config_dir() / "tasks.yml"`, which is under `global/` once the home
    root has moved. A default frozen at import asked for the pre-migration spelling instead, so
    every run on a migrated host answered exit 2 with "No such file" and the pace was never
    measured at all (issue #3181)."""
    home = _home(tmp_path, monkeypatch, migrated=True)

    assert mod.resolve_tasks_path() == home / ".emrg" / "global" / "tasks.yml"
    code = mod.main([])
    out = capsys.readouterr().out
    assert str(home / ".emrg" / "global" / "tasks.yml") in out, "the file read is named first"
    assert code == mod.EXIT_CLEAN


def test_a_host_that_has_not_migrated_keeps_the_old_spelling(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch, migrated=False)

    assert mod.resolve_tasks_path() == home / ".emrg" / "tasks.yml"


def test_the_choice_is_the_file_and_not_the_directory(tmp_path, monkeypatch):
    """A `global/` directory with no table in it is not a migrated host: the move is
    all-or-nothing, but a host can also have created `global/` from some other start."""
    home = _home(tmp_path, monkeypatch, migrated=False)
    (home / ".emrg" / "global").mkdir()

    assert mod.resolve_tasks_path() == home / ".emrg" / "tasks.yml"


def test_an_override_is_measured_where_the_caller_put_it(tmp_path, monkeypatch):
    """A caller naming a path is measuring that path - resolving its post-split twin would
    answer a file the caller did not name."""
    _home(tmp_path, monkeypatch, migrated=True)
    named = tmp_path / "elsewhere.yml"

    assert mod.resolve_tasks_path(str(named)) == named


def test_a_table_that_is_not_yaml_is_exit_2_not_an_empty_clean_run(tmp_path, capsys):
    path = tmp_path / "tasks.yml"
    path.write_text("this: is: not: yaml\n", encoding="utf-8")
    code = mod.main(["--tasks", str(path)])
    assert code == mod.EXIT_UNMEASURABLE
    assert "could not measure" in capsys.readouterr().out


def test_a_block_with_no_pace_makes_the_whole_run_unmeasurable(tmp_path, capsys):
    path = _table(tmp_path, [_record("fine", "n = 5 ... interval = 14400"), _record("silent", "n = 5")])
    code = mod.main(["--tasks", str(path)])
    out = capsys.readouterr().out
    assert code == mod.EXIT_UNMEASURABLE
    assert "not a block that agrees" in out


# ------------------------------------------------------------------------------ the whole run


def test_a_short_block_fails_the_run_and_a_capped_one_does_not(tmp_path, capsys):
    path = _table(
        tmp_path,
        [
            _record("exact", "n = 5 ... interval = 14400"),
            _record("up", "n = 10 ... interval = 7855"),
            _record("capped", "n = 10 ... host order -> interval = 43200"),
        ],
    )
    code = mod.main(["--tasks", str(path)])
    out = capsys.readouterr().out
    assert code == mod.EXIT_CLEAN
    assert "1 rounded up" in out and "0 one second short" in out and "1 from another source" in out


def test_a_truncating_block_is_exit_1_and_names_the_round_up(tmp_path, capsys):
    path = _table(tmp_path, [_record("truncating", "n = 10 ... interval = 7854")])
    code = mod.main(["--tasks", str(path)])
    out = capsys.readouterr().out
    assert code == mod.EXIT_SHORT
    assert "one second short" in out
    assert "rounds the interval UP" in out


def test_a_top_level_block_renders_when_config_carries_none(tmp_path):
    """The precedence the reader applies: config first, the record's own top level after."""
    assert mod.block_of({"name": "t", "config": {}, "extra_prompt": "n = 5 ... interval = 7854"}).strip()
    assert mod.block_of({"name": "t", "config": {"extra_prompt": "n = 5 ... interval = 7855"},
                         "extra_prompt": "n = 5 ... interval = 7854"}).endswith("7855")


def test_the_table_is_never_written(tmp_path, capsys):
    path = _table(tmp_path, [_record("truncating", "n = 10 ... interval = 7854")])
    before = path.read_bytes()
    mod.main(["--tasks", str(path)])
    assert path.read_bytes() == before, "a measuring tool must not edit the file it measures"


def test_quiet_shows_only_what_is_not_exact(tmp_path, capsys):
    path = _table(tmp_path, [_record("alpha-exact", "n = 5 ... interval = 14400"),
                             _record("beta-up", "n = 10 ... interval = 7855")])
    mod.main(["--tasks", str(path), "--quiet"])
    out = capsys.readouterr().out
    assert "alpha-exact" not in out, "an exact block is not what --quiet is for"
    assert "beta-up" in out


def test_a_block_that_makes_no_pacing_claim_is_its_own_class(tmp_path, capsys):
    """A promote or scouting block states neither, and exit 0 must stay reachable."""
    path = _table(tmp_path, [_record("promote", "read the docs and post one update a day")])
    code = mod.main(["--tasks", str(path)])
    out = capsys.readouterr().out
    assert code == mod.EXIT_CLEAN
    assert "not a pacing block" in out
    assert "0 could not measure" in out, "the class is counted, and counted at zero"


def test_an_allowance_with_no_pace_is_still_unmeasurable(tmp_path, capsys):
    """The distinction: a claim about an allowance with no pace beside it cannot be checked."""
    path = _table(tmp_path, [_record("claims-a-pace", "n = 5 and nothing else")])
    code = mod.main(["--tasks", str(path)])
    out = capsys.readouterr().out
    assert code == mod.EXIT_UNMEASURABLE
    assert "states an allowance and no pace" in out


# --------------------------------------------------------------------------- the output path


def test_the_output_survives_a_hostile_stdout_codec(tmp_path):
    """A guard that crashes on its own output reports the defect it was checking for."""
    path = _table(tmp_path, [_record("truncating", "n = 10 ... interval = 7854")])
    env = dict(os.environ, PYTHONIOENCODING="ascii")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--tasks", str(path)],
        capture_output=True, env=env, encoding="ascii", errors="strict",
    )
    # A UnicodeDecodeError raised by the call above *is* the defect: the child wrote a byte
    # its own stdout could not carry, which is how a passing guard reports the fault it checks.
    assert proc.returncode == mod.EXIT_SHORT, proc.stderr
    assert "one second short" in proc.stdout


def test_help_is_ascii_and_exits_zero(tmp_path):
    env = dict(os.environ, PYTHONIOENCODING="ascii")
    proc = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True,
                          env=env, encoding="ascii", errors="strict")
    assert proc.returncode == 0
    assert "task" in proc.stdout
