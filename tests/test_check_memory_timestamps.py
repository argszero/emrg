"""Tests for `scripts/check-memory-timestamps.py`.

The tool answers one question - is a frontmatter timestamp in the future - and this file
tests it in both directions, because a check that only ever sees violations reports a
clean tree for the wrong reason: a file whose block was never parsed, a value compared as
text, a tolerance that swallowed every stamp.

The controls that keep it honest are the ones that must stay **silent**: a memory file
with no frontmatter at all (`MEMORY.md` is a title and rows), a value that is not a date
(`id: pending001`), and a date written in the *body* rather than the block.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_memory_timestamps", REPO_ROOT / "scripts" / "check-memory-timestamps.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load()


def _memory(tmp_path: Path, name: str, frontmatter: str, body: str = "Body.") -> Path:
    """A memory file with the given frontmatter block, at the conventional path."""
    directory = tmp_path / ".emrg" / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return path


def _stamp(offset_seconds: float) -> str:
    """A local ISO timestamp `offset_seconds` from now, positive being the future."""
    return (datetime.now() + timedelta(seconds=offset_seconds)).isoformat(timespec="seconds")


# ── the defect, and the states that must not be read as one ───────────────────


def test_a_stamp_in_the_future_is_reported_with_its_file_field_and_value(
    mod, tmp_path, capsys
) -> None:
    """The defect this tool exists for, named where its reader can open it."""
    path = _memory(
        tmp_path,
        "cycle-a.md",
        f"id: a1\nevent_at: {_stamp(-3600)}\ncreated_at: {_stamp(-3600)}\n"
        f"updated_at: {_stamp(600)}",
    )
    assert mod.main([str(path)]) == 1
    out = capsys.readouterr().out
    # Line 5, and the number is the file's own: the opening fence is line 1, the block's
    # first field is line 2. Reported so a reader can open the file at the finding.
    assert f"{path}:5" in out, f"the finding must carry file and line:\n{out}"
    assert "updated_at" in out, out
    assert _stamp(600)[:16] in out, "the value as written must be quoted back:\n" + out
    assert "is in the future" in out, out


def test_a_stamp_in_the_past_is_within_the_rule(mod, tmp_path, capsys) -> None:
    """The direction that keeps the report worth reading: silence when it is right."""
    path = _memory(
        tmp_path, "ok.md", f"id: b1\nevent_at: {_stamp(-7200)}\nupdated_at: {_stamp(-60)}"
    )
    assert mod.main([str(path)]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "OK:" in out, out
    assert "is in the future" not in out, out


def test_the_tolerance_has_both_edges(mod, tmp_path, capsys) -> None:
    """One number, read from the module, so the fixture cannot drift from the rule.

    Inside the tolerance is a pass and outside it is a finding - asserted on the
    constant rather than on 120, because a fixture that hardcoded the margin would keep
    passing if the margin moved and the test did not.
    """
    slack = mod.CLOCK_SKEW_TOLERANCE_SECONDS
    inside = _memory(tmp_path, "inside.md", f"updated_at: {_stamp(slack - 30)}")
    assert mod.main([str(inside)]) == 0, capsys.readouterr().out

    outside = _memory(tmp_path, "outside.md", f"updated_at: {_stamp(slack + 30)}")
    assert mod.main([str(outside)]) == 1, capsys.readouterr().out
    assert "updated_at" in capsys.readouterr().out


def test_a_file_with_no_frontmatter_is_not_a_finding(mod, tmp_path, capsys) -> None:
    """`MEMORY.md` is a title and rows, and the instructions require that shape.

    The absence of a block is not a false date. Without this leg, a tool that reported
    every file it could not parse would fire on the one file this repository's memory
    format writes without frontmatter - and a guard that fires on its own subject's
    format is how its reader learns to ignore it.
    """
    directory = tmp_path / ".emrg" / "memory"
    directory.mkdir(parents=True)
    (directory / "MEMORY.md").write_text(
        "# Memory Index\n\n- [a](a.md) — rec: 2026-10-01, evt: 2026-10-01\n", encoding="utf-8"
    )
    assert mod.main([str(directory / "MEMORY.md")]) == 0, capsys.readouterr().out


def test_a_value_that_is_not_a_date_is_not_a_finding(mod, tmp_path, capsys) -> None:
    """Only what parses as a date-time is compared; the rest is another reader's.

    `id: pending001` and a `status:` line are values of the same block, and a tool that
    compared their text with the clock would report nonsense on every file.
    """
    path = _memory(
        tmp_path,
        "mixed.md",
        "id: pending001\nstatus: active\nscope: project\nupdated_at: 2026-10-01T00:00:00",
    )
    assert mod.main([str(path)]) == 0, capsys.readouterr().out


def test_a_date_in_the_body_is_not_read(mod, tmp_path, capsys) -> None:
    """The body is prose: a date there is a citation, not a claim about this file.

    Two shapes, because one of them passes a reader that only reads the first date it
    finds: a date in a sentence, and **a whole frontmatter block quoted as an example** -
    which is what a record describing this format carries. Both are the body, and the
    block this tool reads is the leading one. Measured 2026-10-01: with the block read
    from anywhere in the file rather than from the top, the quoted example below is
    reported as this file's own stamp.
    """
    quoted = f"Here is the shape, quoted from a record:\n\n---\nid: x\nupdated_at: {_stamp(86400 * 30)}\n---\n"
    for name, body in (
        ("sentence.md", f"A cycle stamped this {_stamp(86400 * 30)} and that is a quotation."),
        ("quoted.md", quoted),
    ):
        path = _memory(
            tmp_path, name, "id: c1\nupdated_at: 2026-10-01T00:00:00", body=body
        )
        assert mod.main([str(path)]) == 0, f"{name}: " + capsys.readouterr().out


def test_a_zoned_stamp_is_compared_in_its_own_zone(mod, tmp_path, capsys) -> None:
    """A `Z` stamp is UTC, not local, and an hour of difference is the whole question.

    Asserted in the direction that discriminates: a UTC stamp 30 minutes ahead of UTC is
    in the future whatever the local offset, so a comparison that dropped the zone would
    pass it on a host behind UTC and fail it ahead of UTC - the same fixture, two
    verdicts, decided by where the machine is.
    """
    future = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = _memory(tmp_path, "zoned.md", f"updated_at: {future}")
    assert mod.main([str(path)]) == 1, capsys.readouterr().out
    assert "is in the future" in capsys.readouterr().out


def test_a_quoted_block_in_a_file_that_has_none_is_not_read(mod, tmp_path, capsys) -> None:
    """The deciding fixture for "the block is the leading one": a file with none at all.

    The prose leg above cannot tell the two readings apart, because the leftmost `---`
    block in a file with frontmatter *is* the real one - measured 2026-10-01, making the
    block read from anywhere an **equivalent** mutant for every file that has a block.
    They differ for exactly this shape: no frontmatter, and a fenced block quoted in the
    body (a record showing the format it is written in). Read from the top, this file has
    no stamps; read from anywhere, the quotation becomes the file's own - which is the
    defect, and this is the fixture that separates the two readings.
    """
    directory = tmp_path / ".emrg" / "memory"
    directory.mkdir(parents=True)
    path = directory / "quoted-only.md"
    path.write_text(
        "# A record with no frontmatter\n\n"
        "The shape it describes:\n\n"
        f"---\nid: x\nupdated_at: {_stamp(86400 * 30)}\n---\n",
        encoding="utf-8",
    )
    assert mod.main([str(path)]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "is in the future" not in out, (
        "a block quoted in the body was read as this file's own frontmatter:\n" + out
    )


# ── nothing to read is not a pass ─────────────────────────────────────────────


def test_an_empty_tree_is_unmeasurable(mod, tmp_path, capsys) -> None:
    """No subject at all must answer `could not measure`, never a green verdict."""
    # The subject has to be given: with no argument the tool reads its own checkout,
    # whose memory directory is not what this test is about.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert mod.main([str(empty)]) == 2
    assert "could not measure" in capsys.readouterr().err


def test_a_named_file_that_cannot_be_read_is_unmeasurable(mod, tmp_path, capsys) -> None:
    """One unreadable subject makes the whole reading incomplete, not clean."""
    missing = tmp_path / "gone.md"
    assert mod.main([str(missing)]) == 2
    assert "could not measure" in capsys.readouterr().err


def test_a_smaller_reading_is_not_a_pass(mod, tmp_path, capsys) -> None:
    """The summary counts what was read, so a run over one file cannot read as coverage.

    The same rule the sibling guards state: a number with no subject beside it is a
    number that will be read as a clean tree.
    """
    path = _memory(tmp_path, "one.md", f"updated_at: {_stamp(-60)}")
    assert mod.main([str(path)]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "1 memory file(s) read" in out, out


# ── the conventions this family imposes ───────────────────────────────────────


def test_the_first_line_names_the_tree_before_any_verdict(mod, tmp_path, capsys) -> None:
    """Every guard in this family names its tree first; this one derives it from itself."""
    path = _memory(tmp_path, "one.md", f"updated_at: {_stamp(-60)}")
    assert mod.main([str(path)]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first == f"tree: {REPO_ROOT}", first


def test_the_help_text_is_ascii(mod, capsys) -> None:
    """This family's printed literals are ASCII: a `--help` with a glyph breaks a leg."""
    with pytest.raises(SystemExit) as exit_code:
        mod.main(["--help"])
    assert exit_code.value.code == 0
    help_text = capsys.readouterr().out
    assert help_text.isascii(), "the family's printed literals are ASCII"
    assert "could not measure" not in help_text
