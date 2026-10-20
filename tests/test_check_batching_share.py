"""`check-batching-share.py` measures two different kinds of thing, and both are measured here.

The tool exists because a wording change asked for a before/after number twice — the share of
rounds carrying more than one tool call, and the share of *lone* rounds whose `intent` names no
answer they wait for — and a number a cycle re-derives from scratch each time is not the same
number. So the corpus is built here the way the daemon writes one (`history_YYYYMMDD.jsonl`, a
JSON object per line, `tool_calls[]` at the top level or under a `content` object), the counts
are known by construction, and every reading the tool prints is checked against them.

Two properties are worth more than the arithmetic:

  * **the exact reading stays exact** — the share and the call-count histogram come from
    `len(tool_calls)` alone, so a fixture with one, two and three calls must move both;
  * **the proxy says it is a proxy** — the lone-`intent` share is lexical, so both directions
    are built: an intent carrying a dependency marker counts as naming an answer, an intent
    carrying none counts against, and the marker list is readable from the tool itself rather
    than restated here (a copy in this file would drift from the list it describes).

And the family's third answer: a corpus that does not exist is `2`, a refusal to measure,
never a `0` that reads as "no rounds, so nothing to fix".
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "scripts" / "check-batching-share.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_batching_share", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool():
    return _load()


def _call(name: str, intent: str) -> dict:
    """One tool call in the daemon's shape: `arguments` is JSON text, `intent` rides inside."""
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps({"intent": intent})},
    }


def _round(*calls: dict, at: str = "2026-10-20T10:00:00", nested: bool = False) -> dict:
    """An assistant record carrying `calls`, in either of the two shapes the reader accepts."""
    record = {"timestamp": at, "type": "message", "role": "assistant", "content": ""}
    if nested:
        record["content"] = {"tool_calls": list(calls)}
    else:
        record["tool_calls"] = list(calls)
    return record


def _corpus(root: Path, records: list[dict], name: str = "history_261020.jsonl") -> Path:
    """Write `records` as one session's history file and answer the directory holding it."""
    target = root / "s_000000_0000_test"
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )
    return root


def _run(tool, *args: str, capsys) -> tuple[int, str, str]:
    rc = tool.main(list(args))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_the_share_and_the_histogram_count_what_the_records_carry(tool, tmp_path, capsys):
    """Three rounds — one lone, one double, one triple — answer 2 of 3, and the histogram says so."""
    root = _corpus(
        tmp_path,
        [
            _round(_call("bash", "read the config")),
            _round(_call("bash", "read the config"), _call("read", "read the tests")),
            _round(
                _call("bash", "read the config"),
                _call("read", "read the tests"),
                _call("grep", "find the callers"),
            ),
        ],
    )

    rc, out, _err = _run(tool, str(root), capsys=capsys)

    assert rc == 0, out
    assert "rounds:  3 assistant record(s)" in out
    assert "share with two or more: 2 (66.7%)" in out
    assert "share with exactly one:  1 (33.3%)" in out
    assert "1 -> 1, 2 -> 1, 3 -> 1" in out


def test_a_record_without_tool_calls_is_not_a_round(tool, tmp_path, capsys):
    """The denominator is rounds, not messages: prose and tool results are neither."""
    root = _corpus(
        tmp_path,
        [
            _round(_call("bash", "read the config")),
            {"timestamp": "2026-10-20T10:00:01", "type": "message", "role": "assistant",
             "content": "an answer with no call in it"},
            {"timestamp": "2026-10-20T10:00:02", "type": "message", "role": "user",
             "content": "a question"},
            {"timestamp": "2026-10-20T10:00:03", "type": "tool_result", "content": "output"},
        ],
    )

    rc, out, _err = _run(tool, str(root), capsys=capsys)

    assert rc == 0, out
    assert "rounds:  1 assistant record(s)" in out


def test_a_nested_content_object_is_read_the_same_way(tool, tmp_path, capsys):
    """Two shapes exist in the corpus, so both are counted — one reader, not two."""
    root = _corpus(
        tmp_path,
        [
            _round(_call("bash", "a"), _call("read", "b"), nested=True),
            _round(_call("bash", "a")),
        ],
    )

    rc, out, _err = _run(tool, str(root), capsys=capsys)

    assert rc == 0, out
    assert "rounds:  2 assistant record(s)" in out
    assert "share with two or more: 1 (50.0%)" in out


def test_the_lone_intent_proxy_reads_both_directions(tool, tmp_path, capsys):
    """An intent that names an answer counts for the rule; one that does not counts against it."""
    root = _corpus(
        tmp_path,
        [
            _round(_call("bash", "check whether the suite is green before deciding")),
            _round(_call("read", "look at the settings file")),
        ],
    )

    rc, out, _err = _run(tool, str(root), capsys=capsys)

    assert rc == 0, out
    assert "lone intents naming no awaited answer (lexical proxy): 1 of 2 (50.0%)" in out


def test_the_marker_list_is_readable_from_the_tool_itself(tool, capsys):
    """The proxy is auditable: `--markers` prints the list the reading was taken with."""
    rc, out, _err = _run(tool, "--markers", capsys=capsys)

    assert rc == 0
    printed = out.splitlines()
    assert printed == list(tool.DEPENDENCY_MARKERS)
    assert printed, "an empty marker list would make every lone intent count against the rule"


def test_an_intent_that_is_not_json_counts_against_the_rule(tool, tmp_path, capsys):
    """A call whose `arguments` cannot be read has no intent to name an answer with."""
    broken = {"id": "x", "type": "function",
              "function": {"name": "bash", "arguments": "not json"}}
    root = _corpus(tmp_path, [_round(broken)])

    rc, out, _err = _run(tool, str(root), capsys=capsys)

    assert rc == 0, out
    assert "lone intents naming no awaited answer (lexical proxy): 1 of 1 (100.0%)" in out


def test_since_counts_only_records_from_that_date_on(tool, tmp_path, capsys):
    """The after reading is taken over a window, so the window has to actually exclude."""
    root = _corpus(
        tmp_path,
        [
            _round(_call("bash", "a"), at="2026-10-19T23:59:59"),
            _round(_call("bash", "a"), _call("read", "b"), at="2026-10-20T00:00:01"),
        ],
    )

    rc, out, _err = _run(tool, str(root), "--since", "2026-10-20", capsys=capsys)

    assert rc == 0, out
    assert "rounds:  1 assistant record(s)" in out
    assert "naming no awaited answer (lexical proxy): 0 of 0" in out
    assert "share with two or more: 1 (100.0%)" in out
    assert "records before 2026-10-20 not counted" in out


def test_no_history_file_answers_two_and_names_the_roots(tool, tmp_path, capsys):
    """Nothing measurable is a refusal, never a zero that reads as a clean corpus."""
    empty = tmp_path / "sessions"
    empty.mkdir()

    rc, out, err = _run(tool, str(empty), capsys=capsys)

    assert rc == 2, out
    assert "not measurable" in err
    assert str(empty) in err


def test_a_corpus_of_no_rounds_still_names_its_subject(tool, tmp_path, capsys):
    """A real corpus with nothing in it is a `0` reading that says so — not a silent pass."""
    root = _corpus(tmp_path, [{"timestamp": "2026-10-20T10:00:00", "type": "message",
                               "role": "user", "content": "hello"}])

    rc, out, _err = _run(tool, str(root), capsys=capsys)

    assert rc == 0, out
    assert "1 history file(s)" in out
    assert "rounds:  0" in out
    assert "nothing measurable" in out


def test_json_carries_the_same_reading(tool, tmp_path, capsys):
    """Another program reads the same numbers, so the two forms must not disagree."""
    root = _corpus(tmp_path, [_round(_call("bash", "a"), _call("read", "b"))])

    rc, out, _err = _run(tool, str(root), "--json", capsys=capsys)

    assert rc == 0, out
    payload = json.loads(out)
    assert payload["rounds"] == 1
    assert payload["multi"] == 1
    assert payload["counts"] == {"2": 1}
    assert payload["files"] == 1


def test_a_file_named_for_the_corpus_is_read_directly(tool, tmp_path, capsys):
    """A subject may be one history file, which is how a single session is measured."""
    root = _corpus(tmp_path, [_round(_call("bash", "a"), _call("read", "b"))])
    single = root / "s_000000_0000_test" / "history_261020.jsonl"

    rc, out, _err = _run(tool, str(single), capsys=capsys)

    assert rc == 0, out
    assert "1 history file(s)" in out
    assert "share with two or more: 1 (100.0%)" in out
