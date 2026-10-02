"""The one home for "is this JSON a mapping?" — ``emrg.server.json_object``.

Several readers hold a JSON file (or one JSONL line) that is meant to be an
object and then index it with ``.get``. Each guarded a different subset of the
ways that can fail, and the subset all of them missed is the same: the text
parses and yields something that is not an object. The four states and the two
entry points are the whole contract; the call sites' own tests live beside the
call sites.
"""

from __future__ import annotations

import json
from pathlib import Path

from emrg.server.json_object import parse_json_object, read_json_object

# The states, as bytes on disk. `undecodable` is not JSON at all — it is not
# even UTF-8 — which is the state a reader that only catches JSONDecodeError
# still meets as UnicodeDecodeError.
NOT_A_MAPPING = {
    "unparseable": b'{"timestamp": "2026-10-02"',
    "empty": b"",
    "blank": b"   \n",
    "list": b"[]",
    "list-of-objects": b'[{"timestamp": "2026-10-02"}]',
    "null": b"null",
    "string": b'"2026-10-02"',
    "int": b"5",
    "true": b"true",
    "undecodable": b"\xff\xfe\x00\x01",
}


class TestReadJsonObject:
    def test_returns_the_mapping_the_file_holds(self, tmp_path):
        p = tmp_path / "log.json"
        p.write_text('{"timestamp": "2026-10-02T00:00:00", "impact": ["a"]}', encoding="utf-8")
        assert read_json_object(p) == {"timestamp": "2026-10-02T00:00:00", "impact": ["a"]}

    def test_an_empty_object_is_a_mapping(self, tmp_path):
        """`{}` is a payload with nothing in it — a different answer from None."""
        p = tmp_path / "log.json"
        p.write_text("{}", encoding="utf-8")
        assert read_json_object(p) == {}

    def test_a_missing_file_is_not_a_mapping(self, tmp_path):
        assert read_json_object(tmp_path / "nope.json") is None

    def test_a_directory_is_not_a_mapping(self, tmp_path):
        """`read_text` on a directory raises IsADirectoryError — an OSError."""
        assert read_json_object(tmp_path) is None

    def test_every_state_that_is_not_a_mapping_answers_none(self, tmp_path):
        p = tmp_path / "log.json"
        for label, payload in NOT_A_MAPPING.items():
            p.write_bytes(payload)
            assert read_json_object(p) is None, label

    def test_the_answer_is_json_shape_not_file_content(self, tmp_path):
        """A reader that wanted the text would ask for the text, not this."""
        p = tmp_path / "log.json"
        p.write_text('{"a": 1}', encoding="utf-8")
        assert isinstance(read_json_object(p), dict)


class TestParseJsonObject:
    def test_returns_the_mapping_the_text_holds(self):
        assert parse_json_object('{"work": "kept"}') == {"work": "kept"}

    def test_a_jsonl_line_needs_no_stripping(self):
        """The callers hand over whole lines, terminator included."""
        assert parse_json_object('{"work": "kept"}\n') == {"work": "kept"}

    def test_an_empty_object_is_a_mapping(self):
        assert parse_json_object("{}") == {}

    def test_every_state_that_is_not_a_mapping_answers_none(self):
        for label, payload in NOT_A_MAPPING.items():
            text = payload.decode("utf-8", "surrogateescape")
            assert parse_json_object(text) is None, label

    def test_a_line_cut_short_by_a_killed_writer_answers_none(self):
        """The state a JSONL append that was interrupted leaves behind."""
        assert parse_json_object('{"timestamp": "2026-10-02T00:00:0') is None

    def test_the_two_entry_points_agree(self, tmp_path):
        p = tmp_path / "log.json"
        cases = {k: v for k, v in NOT_A_MAPPING.items() if k != "undecodable"}
        # `undecodable` is excluded on purpose: it is the one state the file form
        # answers *before* parsing (the bytes never become text), so comparing it
        # here would be comparing the test's own decode, not the reader's.
        for label, payload in list(cases.items()) + [("object", b'{"a": 1}')]:
            p.write_bytes(payload)
            assert read_json_object(p) == parse_json_object(p.read_text(encoding="utf-8")), label

    def test_a_json_decode_error_is_swallowed_but_a_programming_error_is_not(self):
        """The guard is for the input, not for the caller's mistakes.

        `json.loads` on a non-string raises TypeError, and quietly answering
        None for that would hide a caller passing the wrong thing — the shape
        this module exists to stop being what "unreadable" means.
        """
        try:
            parse_json_object(None)  # type: ignore[arg-type]
        except TypeError:
            pass
        else:
            raise AssertionError("a non-string argument must not be answered as 'no payload'")

    def test_the_module_does_not_import_from_the_rest_of_the_tree(self):
        """A leaf: both the daemon and the scheduler import it, so it imports neither."""
        import emrg.server.json_object as mod

        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "from emrg." not in source, "the home must stay a leaf to avoid an import cycle"
        assert "import emrg." not in source

    def test_a_value_json_round_trips_is_returned_unchanged(self, tmp_path):
        payload = {"nested": {"a": [1, 2, {"b": None}]}, "n": 0, "empty": ""}
        p = tmp_path / "log.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        assert read_json_object(p) == payload
