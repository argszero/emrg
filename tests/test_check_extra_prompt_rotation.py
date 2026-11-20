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
  that advanced the table itself would record a classification nobody made;
* **a host with no table has a next step** - `--init` prints a skeleton carrying each rendering
  block's length, it writes nothing itself, and what it prints is a valid table: written to the
  table's path it reads back clean, which is what closes the loop a first reading dead-ends on.

Loading
-------
By path (`spec_from_file_location`), the shape `test_check_memory_index.py` uses: the
filename is not importable by name (the dash).
"""

from __future__ import annotations

import hashlib
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


def _sha(text: str) -> str:
    """The digest a row records for `text`, computed here rather than by the tool.

    The column's contract is "the sha256 of the block's UTF-8 text", and a fixture that asked
    the tool for the number would agree with the tool about a wrong one.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _row(text: str, chars: int | None = None) -> tuple[int, str]:
    """An `(chars, sha256)` row for `text` - its own length, or `chars` when they differ."""
    return (len(text) if chars is None else chars, _sha(text))


def _table(
    tmp_path: Path, rows: dict[str, tuple[int, str | None]], name: str = "rotation.md"
) -> Path:
    """Write a rotation table carrying the given `task -> (chars, sha256)` rows.

    A `None` digest is written as an empty cell: a row that records no text and so cannot
    answer whether the block moved.
    """
    lines = ["| task | chars | sha256 | class | evidence |", "|---|---|---|---|---|"]
    for task, (chars, sha) in rows.items():
        lines.append(
            f"| `{task}` | {chars} | {sha or ''} | **already in template** | measured earlier |"
        )
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
    table = _table(tmp_path, {"zeta": _row(body)})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 0
    out = capsys.readouterr().out
    assert "8 unchanged" not in out  # the count is the fixture's, not this host's
    assert "1 unchanged" in out


def test_a_moved_length_names_both_numbers(mod, tmp_path, capsys):
    """The one state the rotation exists to catch, and it has to be readable without a diff."""
    body = "x" * 40
    tasks = _tasks(tmp_path, [_record("zeta", text=body)])
    table = _table(tmp_path, {"zeta": _row(body, chars=25)})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 1
    out = capsys.readouterr().out
    assert "changed" in out
    assert "table says 25" in out and "block is 40" in out


def test_a_block_with_no_row_needs_classifying(mod, tmp_path, capsys):
    """A new block cannot be silently outside the rotation."""
    tasks = _tasks(tmp_path, [_record("zeta", text="x" * 12)])
    table = _table(tmp_path, {"other": _row("x" * 12)})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 1
    out = capsys.readouterr().out
    assert "unclassified" in out and "zeta" in out


def test_a_row_whose_block_is_gone_is_reported(mod, tmp_path, capsys):
    """A row left behind keeps a classification alive for text that no longer renders."""
    tasks = _tasks(tmp_path, [_record("zeta")])
    table = _table(tmp_path, {"zeta": _row("x" * 40)})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 1
    out = capsys.readouterr().out
    assert "gone" in out and "nothing renders" in out


def test_a_top_level_block_renders_when_config_carries_none(mod, tmp_path, capsys):
    """The carrier a probe of this question once missed, and it read as 'no blocks at all'."""
    body = "top-level rule"
    tasks = _tasks(tmp_path, [_record("zeta", top=body)])
    table = _table(tmp_path, {"zeta": _row(body)})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 0
    out = capsys.readouterr().out
    assert "1 render" in out, "a top-level block is a block: it has to be counted"


def test_the_config_copy_renders_and_the_duplicate_is_named(mod, tmp_path, capsys):
    """Precedence, read as precedence: the config copy is measured, the other one is reported."""
    config_body = "y" * 30
    tasks = _tasks(tmp_path, [_record("zeta", text=config_body, top="z" * 99)])
    table = _table(tmp_path, {"zeta": _row(config_body)})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 0
    out = capsys.readouterr().out
    assert "duplicate" in out, "a second carrier is reported, never silently ignored"
    assert "both carry a copy" in out


def test_the_subjects_are_named_before_the_verdict(mod, tmp_path, capsys):
    """A reading names what it read; a count without its subject is not reusable."""
    tasks = _tasks(tmp_path, [_record("zeta", text="x")])
    table = _table(tmp_path, {"zeta": _row("x")})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 0
    out = capsys.readouterr().out
    assert str(tasks) in out and str(table) in out
    assert "1 record(s)" in out and "1 row(s)" in out


def test_a_missing_task_table_is_not_a_pass(mod, tmp_path, capsys):
    table = _table(tmp_path, {"zeta": _row("x")})
    assert mod.main(["--tasks", str(tmp_path / "absent.yml"), "--table", str(table)]) == 2
    assert "cannot measure" in capsys.readouterr().err


def test_a_missing_rotation_table_is_not_a_pass(mod, tmp_path, capsys):
    tasks = _tasks(tmp_path, [_record("zeta", text="x")])
    assert mod.main(["--tasks", str(tasks), "--table", str(tmp_path / "absent.md")]) == 2
    assert "cannot measure" in capsys.readouterr().err


def test_yaml_that_does_not_parse_is_not_a_pass(mod, tmp_path, capsys):
    broken = tmp_path / "broken.yml"
    broken.write_text("name: [unclosed\n", encoding="utf-8")
    table = _table(tmp_path, {"zeta": _row("x")})

    assert mod.main(["--tasks", str(broken), "--table", str(table)]) == 2
    assert "cannot measure" in capsys.readouterr().err


def test_records_that_are_not_a_list_are_not_a_pass(mod, tmp_path, capsys):
    """A mapping where a list of records belongs must not read as an empty rotation."""
    wrong = tmp_path / "wrong.yml"
    wrong.write_text("zeta:\n  config:\n    extra_prompt: hi\n", encoding="utf-8")
    table = _table(tmp_path, {"zeta": _row("xx")})

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
    table = _table(tmp_path, {"zeta": _row("x" * 40, chars=25)})
    tasks_before = tasks.read_bytes()
    table_before = table.read_bytes()

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 1

    assert tasks.read_bytes() == tasks_before
    assert table.read_bytes() == table_before


def test_all_lists_the_unchanged_blocks_too(mod, tmp_path, capsys):
    """`--all` prints the length list, which is what a cycle reads out loud."""
    body = "x" * 40
    tasks = _tasks(tmp_path, [_record("zeta", text=body), _record("eta", text="y" * 7)])
    table = _table(tmp_path, {"zeta": _row(body), "eta": _row("y" * 7)})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 0
    default_out = capsys.readouterr().out
    assert "zeta" not in default_out, "the default run is silent about an unchanged block"

    assert mod.main(["--tasks", str(tasks), "--table", str(table), "--all"]) == 0
    all_out = capsys.readouterr().out
    assert "zeta" in all_out and "eta" in all_out
    assert "40 chars" in all_out and "7 chars" in all_out


def test_init_prints_a_row_per_rendering_block(mod, tmp_path, capsys):
    """The bootstrap: a host that never wrote the table gets the shape, with its own numbers."""
    tasks = _tasks(
        tmp_path,
        [_record("zeta", text="x" * 40), _record("eta"), _record("omega", top="y" * 9)],
    )

    assert mod.main(["--tasks", str(tasks), "--init"]) == 0
    out = capsys.readouterr().out
    assert "| task | chars | sha256 | class | evidence |" in out
    assert "| `zeta` | 40 |" in out
    assert "| `omega` | 9 |" in out, "a top-level block is a block here too"
    assert "`eta`" not in out, "nothing renders for eta, so there is nothing to classify"


def test_init_writes_nothing(mod, tmp_path, capsys):
    """It prints the shape; writing a classification nobody made is not its job."""
    tasks = _tasks(tmp_path, [_record("zeta", text="x" * 12)])
    table = tmp_path / "absent.md"

    assert mod.main(["--tasks", str(tasks), "--table", str(table), "--init"]) == 0
    capsys.readouterr()
    assert not table.exists(), "the tool may not create the table it is asked to read"


def test_what_init_prints_reads_back_as_unclassified(mod, tmp_path, capsys):
    """The loop closes on a table that still owes its reading.

    This test used to assert the opposite - that `--init`'s printed skeleton read back **clean**,
    `2 render, 2 unchanged`, exit 0. That verdict was the defect: the skeleton carries a valid
    fingerprint per row and no classification, so "unchanged" answered a question nobody had
    asked and the table sat half-unclassified while the guard said there was nothing to do
    (issue #3115; measured on this host, 12 of 25 rows). What survives from the old assertion is
    its real subject - the skeleton is a table this file can read, naming every block - and the
    verdict is now the one the rows earn.
    """
    tasks = _tasks(tmp_path, [_record("zeta", text="x" * 40), _record("omega", top="y" * 9)])

    assert mod.main(["--tasks", str(tasks), "--init"]) == 0
    printed = capsys.readouterr().out

    table = tmp_path / "rotation.md"
    table.write_text(printed, encoding="utf-8")
    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 1
    out = capsys.readouterr().out
    assert "2 render, 0 unchanged" in out, out
    assert "2 need classifying" in out, out
    assert "zeta" in out and "omega" in out, out


def test_a_row_still_carrying_the_placeholder_needs_classifying(mod, tmp_path, capsys):
    """The regression, at its smallest: one row, a fingerprint that matches, no classification."""
    body = "x" * 40
    tasks = _tasks(tmp_path, [_record("zeta", text=body)])
    table = tmp_path / "rotation.md"
    table.write_text(
        "| task | chars | sha256 | class | evidence |\n|---|---|---|---|---|\n"
        f"| `zeta` | {len(body)} | {_sha(body)} | **unclassified** | printed by --init |\n",
        encoding="utf-8",
    )

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 1
    out = capsys.readouterr().out
    assert "placeholder" in out and "zeta" in out, out
    assert "`--init`" in out, "the detail must say whose placeholder it is: " + out
    assert "unchanged numbers are not evidence" in out, (
        "the remedy must not read as 're-classify the drifted ones': " + out
    )


def test_a_real_class_is_clean_even_beside_an_init_evidence_cell(mod, tmp_path, capsys):
    """The other direction: the class cell is read, and the evidence cell is not.

    A row that says `printed by --init` as its *evidence* but carries a real classification is
    a row someone did go back to. Reading the phrase anywhere in the line rather than in the
    class cell would report it, which is why this arm names the string that would trip a
    substring matcher.
    """
    body = "x" * 40
    tasks = _tasks(tmp_path, [_record("zeta", text=body)])
    table = tmp_path / "rotation.md"
    table.write_text(
        "| task | chars | sha256 | class | evidence |\n|---|---|---|---|---|\n"
        f"| `zeta` | {len(body)} | {_sha(body)} | **instance configuration** | "
        "printed by --init, then classified |\n",
        encoding="utf-8",
    )

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 0
    out = capsys.readouterr().out
    assert "1 unchanged" in out, out
    assert "placeholder" not in out, out


def test_an_empty_class_cell_keeps_its_reading(mod, tmp_path, capsys):
    """An absent marker is not a claim.

    A table written before the class column existed, or a cell someone cleared, carries no
    evidence either way. This file can see the marker `--init` prints and cannot see the absence
    of a classification, so it reports what it can measure and does not invent the rest.
    """
    body = "x" * 40
    tasks = _tasks(tmp_path, [_record("zeta", text=body)])
    table = tmp_path / "rotation.md"
    table.write_text(
        "| task | chars | sha256 | class | evidence |\n|---|---|---|---|---|\n"
        f"| `zeta` | {len(body)} | {_sha(body)} |  |  |\n",
        encoding="utf-8",
    )

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 0
    out = capsys.readouterr().out
    assert "1 unchanged" in out, out
    assert "placeholder" not in out, out


def test_the_missing_table_remedy_names_init(mod, tmp_path, capsys):
    """A dead end that names its own next step, which is what the first reading used to be."""
    tasks = _tasks(tmp_path, [_record("zeta", text="x")])

    assert mod.main(["--tasks", str(tasks), "--table", str(tmp_path / "absent.md")]) == 2
    err = capsys.readouterr().err
    assert "cannot measure" in err and "--init" in err, err


def test_a_same_length_edit_needs_classifying(mod, tmp_path, capsys):
    """The hole a length-only clock left: the text moved and the number did not.

    `cyc20261117-181812` found it by reading `classify` - `rows[task] != length` was the only
    text-side test - so a block rewritten to the same number of characters answered `unchanged`
    and was never re-classified.
    """
    before = "x" * 40
    after = "y" * 40
    tasks = _tasks(tmp_path, [_record("zeta", text=after)])
    table = _table(tmp_path, {"zeta": _row(before)})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 1
    out = capsys.readouterr().out
    assert "rewritten" in out
    assert "40 chars unchanged, but the text moved" in out
    assert _sha(before) in out and _sha(after) in out, "both digests, so the row can be fixed"


def test_a_row_with_no_digest_is_not_a_pass(mod, tmp_path, capsys):
    """A row that records no text cannot answer the question, so it is reported, never clean.

    This is the other direction of the arm above: there the digest was present and disagreed,
    here it is absent - and absence reads as `undigested` with the digest that belongs in the
    row, which is the difference between "nothing moved" and "could not measure".
    """
    body = "x" * 40
    tasks = _tasks(tmp_path, [_record("zeta", text=body)])
    table = _table(tmp_path, {"zeta": (len(body), None)})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 1
    out = capsys.readouterr().out
    assert "undigested" in out
    assert "the row carries no sha256" in out
    assert _sha(body) in out, "the remedy is the digest that belongs in that row"


def test_an_undigested_row_is_not_counted_as_needing_classification(mod, tmp_path, capsys):
    """Two jobs, two tallies: a row that lacks a digest is classified already.

    Measured 2026-11-17 on a copy of a real rotation table — 22 rows, every one carrying its
    class and evidence, none carrying a digest — the guard printed `22 need classifying` and
    told the reader to classify them. Following that sentence re-reads every block, which is
    the cost the rotation exists to avoid; what those rows need is the digest the same output
    prints beside each one.
    """
    body = "x" * 40
    tasks = _tasks(tmp_path, [_record("zeta", text=body)])
    table = _table(tmp_path, {"zeta": (len(body), None)})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 1
    out = capsys.readouterr().out

    assert "0 need classifying" in out, out
    assert "1 need a digest" in out, out
    assert "22 need classifying" not in out and "1 need classifying" not in out, out


def test_the_undigested_remedy_names_the_digest_not_a_classification(mod, tmp_path, capsys):
    """The tally is half of it; the instruction under it is what a reader acts on.

    The remedy has to name the row edit — add the printed sha256, keep the class — and must
    not carry the classification sentence, whose subject is a block that moved or has no row.
    """
    body = "x" * 40
    tasks = _tasks(tmp_path, [_record("zeta", text=body)])
    table = _table(tmp_path, {"zeta": (len(body), None)})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 1
    out = capsys.readouterr().out

    assert "add the sha256 printed beside each undigested row" in out, out
    assert "keeping its class and evidence" in out, out
    assert "classify the block(s) above" not in out, (
        "an undigested row is classified already; telling the reader to classify it is the "
        "re-reading this clock exists to save"
    )
    assert _sha(body) in out, "the remedy is the digest that belongs in that row"


def test_a_moved_block_still_asks_for_a_classification(mod, tmp_path, capsys):
    """The other direction: separating the tallies must not silence the reading they came from.

    A block whose length moved needs a decision nobody has made yet, so it keeps the
    classification sentence — and it must not be reported as needing a digest, which would
    send the reader to edit a row whose number is the thing that disagrees.
    """
    body = "x" * 40
    tasks = _tasks(tmp_path, [_record("zeta", text=body)])
    table = _table(tmp_path, {"zeta": _row(body, chars=25)})

    assert mod.main(["--tasks", str(tasks), "--table", str(table)]) == 1
    out = capsys.readouterr().out

    assert "1 need classifying" in out, out
    assert "0 need a digest" in out, out
    assert "classify the block(s) above" in out, out
    assert "add the sha256 printed beside each undigested row" not in out, out


def test_init_with_no_rendering_block_is_not_a_pass(mod, tmp_path, capsys):
    """A skeleton with no rows would be an empty table, and an empty table is not a reading."""
    tasks = _tasks(tmp_path, [_record("zeta"), _record("eta")])

    assert mod.main(["--tasks", str(tasks), "--init"]) == 2
    assert "no record with a rendering extra_prompt" in capsys.readouterr().err
