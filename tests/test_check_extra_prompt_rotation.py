"""The section 1.7 rotation reading: `scripts/check-extra-prompt-rotation.py`.

What this file holds
--------------------
The tool answers section 1.7's question - which task `extra_prompt` blocks moved since the rotation
table classified them - and it exists because prose asking a reader to compare eight numbers
by eye decayed three times (issue #2850). So the assertions are the three halves of "does it
answer that question, and can a wrong answer read as clean":

* **each state is read in both directions, and the fixtures are built from the tool's own
  contract rather than from this host** - an unchanged block answers 0, and a block whose
  length moved answers 1 naming both numbers, so the failure a cycle is meant to act on is
  the one the arms exercise;
* **both carriers of the field are read** - `config.extra_prompt` renders and a top-level
  `extra_prompt` renders when `config` carries none, which is the defect a probe of the same
  question produced while reporting "no blocks at all"; a record carrying both renders the
  `config` copy and says so;
* **a reading that could not be taken is never a pass** - no task table, no rotation table,
  YAML that does not parse, records that are not a list, and a table with no parsable row all
  answer **2**, and `0` is reserved for a reading that covered every block;
* **it measures and never repairs** - a run leaves both files byte-identical, because a tool
  that advanced the table itself would record a classification nobody made.

Loading
-------
By path (`spec_from_file_location`), the shape `test_check_memory_index.py` uses: the
filename is not importable by name (the dash).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-extra-prompt-rotation.py"


def _load():
    """The tool, loaded by path with its own module name."""
    spec = importlib.util.spec_from_file_location("check_extra_prompt_rotation", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def _tasks(tmp_path: Path, records: list[dict], name: str = "tasks.yml") -> Path:
    """Write a task table and return its path."""
    path = tmp_path / name
    path.write_text(
        yaml.safe_dump(records, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def _table(tmp_path: Path, rows: dict[str, int], name: str = "rotation.md") -> Path:
    """Write a rotation table whose rows carry the given lengths, and return its path."""
    lines = ["| task | chars | class | evidence |", "|---|---|---|---|"]
    for task, chars in rows.items():
        lines.append(f"| `{task}` | {chars} | **already in template** | measured earlier |")
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _record(name: str, text: str | None = None, top: str | None = None) -> dict:
    """A task record carrying `text` under `config` and/or `top` at the top level."""
    record: dict = {"name": name, "type": "opensource"}
    config: dict = {}
    if text is not None:
        config["extra_prompt"] = text
    record["config"] = config
    if top is not None:
        record["extra_prompt"] = top
    return record


def test_an_unchanged_block_reads_as_unchanged(mod, tmp_path, capsys):
    """The ordinary cycle: the length list is the reading, and it costs one command."""
    body = "x" * 40
    tasks = _tasks(tmp_path, [_record("zeta", text=body)])
    table = _table(tmp_path, {"zeta": len(body)})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 0
    out = capsys.readouterr().out
    assert "8 unchanged" not in out  # the count is the fixture's, not this host's
    assert "1 unchanged" in out


def test_a_moved_length_names_both_numbers(mod, tmp_path, capsys):
    """The one state the rotation exists to catch, and it has to be readable without a diff."""
    body = "x" * 40
    tasks = _tasks(tmp_path, [_record("zeta", text=body)])
    table = _table(tmp_path, {"zeta": 25})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 1
    out = capsys.readouterr().out
    assert "changed" in out
    assert "table says 25" in out and "block is 40" in out


def test_a_block_with_no_row_needs_classifying(mod, tmp_path, capsys):
    """A new block cannot be silently outside the rotation."""
    tasks = _tasks(tmp_path, [_record("zeta", text="x" * 12)])
    table = _table(tmp_path, {"other": 12})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 1
    out = capsys.readouterr().out
    assert "unclassified" in out and "zeta" in out


def test_a_row_whose_block_is_gone_is_reported(mod, tmp_path, capsys):
    """A row left behind keeps a classification alive for text that no longer renders."""
    tasks = _tasks(tmp_path, [_record("zeta")])
    table = _table(tmp_path, {"zeta": 40})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 1
    out = capsys.readouterr().out
    assert "gone" in out and "nothing renders" in out


def test_a_top_level_block_renders_when_config_carries_none(mod, tmp_path, capsys):
    """The carrier a probe of this question once missed, and it read as 'no blocks at all'."""
    body = "top-level rule"
    tasks = _tasks(tmp_path, [_record("zeta", top=body)])
    table = _table(tmp_path, {"zeta": len(body)})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 0
    out = capsys.readouterr().out
    assert "1 render" in out, "a top-level block is a block: it has to be counted"


def test_the_config_copy_renders_and_the_duplicate_is_named(mod, tmp_path, capsys):
    """Precedence, read as precedence: the config copy is measured, the other one is reported."""
    config_body = "y" * 30
    tasks = _tasks(tmp_path, [_record("zeta", text=config_body, top="z" * 99)])
    table = _table(tmp_path, {"zeta": len(config_body)})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 0
    out = capsys.readouterr().out
    assert "duplicate" in out, "a second carrier is reported, never silently ignored"
    assert "both carry a copy" in out


def test_the_subjects_are_named_before_the_verdict(mod, tmp_path, capsys):
    """A reading names what it read; a count without its subject is not reusable."""
    tasks = _tasks(tmp_path, [_record("zeta", text="x")])
    table = _table(tmp_path, {"zeta": 1})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 0
    out = capsys.readouterr().out
    assert str(tasks) in out and str(table) in out
    assert "1 record(s)" in out and "1 row(s)" in out


def test_a_missing_task_table_is_not_a_pass(mod, tmp_path, capsys):
    table = _table(tmp_path, {"zeta": 1})
    assert mod.main(["--tasks", str(tmp_path / "absent.yml"), "--table", str(table)]) == 2
    assert "cannot measure" in capsys.readouterr().err


def test_a_missing_rotation_table_is_not_a_pass(mod, tmp_path, capsys):
    tasks = _tasks(tmp_path, [_record("zeta", text="x")])
    assert mod.main(["--tasks", str(tasks), "--table", str(tmp_path / "absent.md")]) == 2
    assert "cannot measure" in capsys.readouterr().err


def test_yaml_that_does_not_parse_is_not_a_pass(mod, tmp_path, capsys):
    broken = tmp_path / "broken.yml"
    broken.write_text("name: [unclosed\n", encoding="utf-8")
    table = _table(tmp_path, {"zeta": 1})

    assert mod.main(["--tasks", str(broken), "--table", str(table)]) == 2
    assert "cannot measure" in capsys.readouterr().err


def test_records_that_are_not_a_list_are_not_a_pass(mod, tmp_path, capsys):
    """A mapping where a list of records belongs must not read as an empty rotation."""
    wrong = tmp_path / "wrong.yml"
    wrong.write_text("zeta:\n  config:\n    extra_prompt: hi\n", encoding="utf-8")
    table = _table(tmp_path, {"zeta": 2})

    assert mod.main(["--tasks", str(wrong), "--table", str(table)]) == 2
    assert "not a list of records" in capsys.readouterr().err


def test_a_table_with_no_parsable_row_is_not_a_pass(mod, tmp_path, capsys):
    """An empty table would make every block 'unclassified'; it is 'could not measure'."""
    tasks = _tasks(tmp_path, [_record("zeta", text="x")])
    empty = tmp_path / "empty.md"
    empty.write_text("# a table that carries no row\n", encoding="utf-8")

    assert mod.main(["--tasks", str(tasks), "--table", str(empty)]) == 2
    assert "no row this file can parse" in capsys.readouterr().err


def test_it_measures_and_never_repairs(mod, tmp_path):
    """Neither file may move: recording a classification needs a reader's judgement."""
    tasks = _tasks(tmp_path, [_record("zeta", text="x" * 40)])
    table = _table(tmp_path, {"zeta": 25})
    tasks_before = tasks.read_bytes()
    table_before = table.read_bytes()

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 1

    assert tasks.read_bytes() == tasks_before
    assert table.read_bytes() == table_before


def test_all_lists_the_unchanged_blocks_too(mod, tmp_path, capsys):
    """`--all` prints the length list, which is what a cycle reads out loud."""
    body = "x" * 40
    tasks = _tasks(tmp_path, [_record("zeta", text=body), _record("eta", text="y" * 7)])
    table = _table(tmp_path, {"zeta": len(body), "eta": 7})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 0
    default_out = capsys.readouterr().out
    assert "zeta" not in default_out, "the default run is silent about an unchanged block"

    assert mod.main(["--tasks", str(tasks), "--table", str(table), "--all"]) == 0
    all_out = capsys.readouterr().out
    assert "zeta" in all_out and "eta" in all_out
    assert "40 chars" in all_out and "7 chars" in all_out
