"""Every reader that names an error guard answers the third shape too.

Measured 2026-10-03 (`cyc20261003-222355`). The rule `emrg/read_errors.py` states and
`scripts/check_read_parse_guards.py` enforces has two halves: a `try` that reads a
file's text must name a decode error, whether or not it parses what it read. The
guard's first version asked only about readers that parse, so eleven **read-only**
readers went on letting `UnicodeDecodeError` — a `ValueError`, not an `OSError` —
past a handler spelled `except OSError`.

A guard is a claim; these are the readings that make it true one reader at a time.
Each leg feeds its reader real bytes that are not UTF-8 and asserts the answer its
own docstring promises, because "the exception no longer escapes" and "the documented
fallback happens" are different claims and only the second one helps a caller. The
control leg beside each — the same reader over a readable file — is what keeps a
reader that answers the fallback to *everything* from passing.

Nothing here touches `~/.emrg/config.toml`, a daemon, or the upgrade chain's
endpoints: every path is one this file created, and the two version files the upgrade
module names are patched to `tmp_path` by `conftest.py`'s autouse hermeticity fixture
before the first test runs.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
NOT_UTF8 = b"caf\xe9 \xff\xfe not utf-8\n"  # latin-1 bytes: valid text, invalid UTF-8


def _load(relative: str):
    """Import a `scripts/*.py` by path, the way the other script tests do."""
    path = REPO_ROOT / relative
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestTheProductReaders:
    def test_the_usage_anchor_count_survives_a_stats_file_it_cannot_decode(self, tmp_path) -> None:
        """`_append_usage_anchor_event`: the count restarts instead of raising.

        The docstring promises "callers swallow OSError — a stats write must never take
        the daemon down". A line count decodes, so a stats file whose bytes are not
        UTF-8 raised out of a function whose contract is that it does not.
        """
        from emrg.server.daemon import _append_usage_anchor_event

        stats = tmp_path / "usage-anchor.jsonl"
        stats.write_bytes(NOT_UTF8)
        total = _append_usage_anchor_event({"kind": "probe"}, path=stats)
        assert total == 1  # the unreadable history counted as none, and this event is one

    def test_the_usage_anchor_count_control(self, tmp_path) -> None:
        """The readable direction: two existing events, so the new one is three."""
        from emrg.server.daemon import _append_usage_anchor_event

        stats = tmp_path / "usage-anchor.jsonl"
        stats.write_text('{"a": 1}\n{"b": 2}\n', encoding="utf-8")
        assert _append_usage_anchor_event({"kind": "probe"}, path=stats) == 3

    def test_a_custom_task_template_that_cannot_be_decoded_reads_as_missing(
        self, tmp_path, monkeypatch
    ) -> None:
        """`_read_custom_template`: None, which is already the answer for "not there"."""
        from emrg.server import scheduler

        monkeypatch.setattr(scheduler, "_task_templates_dir", lambda: tmp_path)
        (tmp_path / "notes.md").write_bytes(NOT_UTF8)
        assert scheduler._read_custom_template("notes") is None

    def test_a_custom_task_template_that_can_be_decoded_still_reads(self, tmp_path, monkeypatch) -> None:
        from emrg.server import scheduler

        monkeypatch.setattr(scheduler, "_task_templates_dir", lambda: tmp_path)
        (tmp_path / "notes.md").write_text("hello", encoding="utf-8")
        assert scheduler._read_custom_template("notes") == "hello"

    def test_a_builtin_template_that_cannot_be_decoded_falls_back_to_the_placeholder(
        self, tmp_path, monkeypatch
    ) -> None:
        """`list_templates`: the preview is empty, the row is still listed.

        The frame the GUI reads must exist for every task type — an empty `prompt` is
        the documented fallback, and a raised `UnicodeDecodeError` would take the whole
        list with it.
        """
        from emrg.server import scheduler

        (tmp_path / "bad.md").write_bytes(NOT_UTF8)
        monkeypatch.setattr(scheduler, "__file__", str(tmp_path / "scheduler.py"))
        monkeypatch.setattr(scheduler, "TASK_TEMPLATES", {"bad": "bad.md"})
        manager = scheduler.TaskScheduler.__new__(scheduler.TaskScheduler)
        rows = manager.list_templates()
        assert [r["name"] for r in rows] == ["bad"]
        assert rows[0]["prompt"] == ""

    def test_the_installed_versions_reader_skips_a_file_it_cannot_decode(
        self, tmp_path, monkeypatch
    ) -> None:
        """`_installed_versions`: "the policy must not depend on them being there"."""
        from emrg.server import upgrade

        current = tmp_path / "version.txt"
        previous = tmp_path / "previous-version.txt"
        current.write_bytes(NOT_UTF8)
        previous.write_text("v0.3.7\n", encoding="utf-8")
        monkeypatch.setattr(upgrade, "VERSION_FILE", current)
        monkeypatch.setattr(upgrade, "PREVIOUS_VERSION_FILE", previous)
        assert upgrade._installed_versions() == ("0.3.7",)

    def test_the_local_version_reader_answers_empty_for_a_file_it_cannot_decode(
        self, tmp_path, monkeypatch
    ) -> None:
        """`_read_local_version`: "" on failure, the same answer as a missing file."""
        from emrg.server import upgrade

        current = tmp_path / "version.txt"
        current.write_bytes(NOT_UTF8)
        monkeypatch.setattr(upgrade, "VERSION_FILE", current)
        manager = upgrade.UpgradeManager.__new__(upgrade.UpgradeManager)
        assert manager._read_local_version() == ""

    def test_the_local_version_reader_control(self, tmp_path, monkeypatch) -> None:
        from emrg.server import upgrade

        current = tmp_path / "version.txt"
        current.write_text("v0.3.8\n", encoding="utf-8")
        monkeypatch.setattr(upgrade, "VERSION_FILE", current)
        manager = upgrade.UpgradeManager.__new__(upgrade.UpgradeManager)
        assert manager._read_local_version() == "0.3.8"


class TestTheScriptReaders:
    def test_declared_version_is_none_for_a_source_it_cannot_decode(self, tmp_path) -> None:
        """`check-release-tag.py`: None is "no reading", and every caller says so."""
        mod = _load("scripts/check-release-tag.py")
        root = tmp_path
        (root / mod.VERSION_SOURCE).parent.mkdir(parents=True, exist_ok=True)
        (root / mod.VERSION_SOURCE).write_bytes(NOT_UTF8)
        assert mod.declared_version(root) is None

    def test_declared_version_control(self, tmp_path) -> None:
        mod = _load("scripts/check-release-tag.py")
        root = tmp_path
        (root / mod.VERSION_SOURCE).parent.mkdir(parents=True, exist_ok=True)
        (root / mod.VERSION_SOURCE).write_text('__version__ = "0.3.8"\n', encoding="utf-8")
        assert mod.declared_version(root) == "0.3.8"

    def test_a_rant_citation_subject_that_cannot_be_decoded_is_reported_missing(self, tmp_path) -> None:
        """`check-rant-citations.py`: the file joins `missing`, the scan still runs."""
        mod = _load("scripts/check-rant-citations.py")
        (tmp_path / "bad.md").write_bytes(NOT_UTF8)
        (tmp_path / "good.md").write_text("plain prose, no citation\n", encoding="utf-8")
        sites, missing = mod.scan_tree(tmp_path, ("bad.md", "good.md"))
        assert missing == ["bad.md"]
        assert sites == []

    def test_an_unreadable_rant_ledger_raises_rather_than_answering_no_rants(self, tmp_path) -> None:
        """`check-issue-links.py`: a RuntimeError, never an empty ledger."""
        mod = _load("scripts/check-issue-links.py")
        path = tmp_path / "rants.jsonl"
        path.write_bytes(NOT_UTF8)
        with pytest.raises(RuntimeError):
            mod.load_rants(path)

    def test_calibrate_reports_an_input_it_cannot_decode_and_exits_one(self, tmp_path) -> None:
        """`calibrate_silent_drift_threshold.py`: "the script must not guess"."""
        mod = _load("scripts/calibrate_silent_drift_threshold.py")
        path = tmp_path / "usage-anchor.jsonl"
        path.write_bytes(NOT_UTF8)
        with pytest.raises(SystemExit) as excinfo:
            mod.load_events(path)
        assert excinfo.value.code == 1

    def test_resolve_conflict_reports_a_document_it_cannot_decode(self, tmp_path, monkeypatch) -> None:
        """`check-doc-count.py --resolve-conflict`: exit 2, not a rewrite of nothing.

        The one site in this file that also *writes*: if the read fails the count line
        is not there to clear, and the mode's whole purpose is to rewrite that line.
        """
        mod = _load("scripts/check-doc-count.py")
        (tmp_path / "Agent.md").write_bytes(NOT_UTF8)
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        assert mod._resolve_conflict_mode() == 2

    def test_the_node_count_tool_reports_a_gui_test_file_it_cannot_decode(
        self, tmp_path, monkeypatch
    ) -> None:
        """`module_skip_entries`: the read there had **no** `try` at all.

        Found by re-running the older branch's own test file against this tree
        (`cyc20261003-222355`): its census reports every read, guarded or not, and this
        one is a read inside a generator expression. The guard next door only sees reads
        that already have a `try`, which is why this site needed a leg of its own.
        """
        mod = _load("scripts/check-node-test-count.py")
        monkeypatch.setattr(mod, "GUI_ROOT", tmp_path)
        (tmp_path / "test").mkdir()
        (tmp_path / "test" / f"bad{mod.GUI_TEST_SUFFIX}").write_bytes(NOT_UTF8)
        with pytest.raises(mod.NodeCountError, match="cannot read"):
            mod.module_skip_entries()

    def test_the_node_count_tool_reports_a_document_it_cannot_decode_as_unmeasurable(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """`check-node-test-count.py`: exit 2, because exit 1 is its "they disagree"."""
        mod = _load("scripts/check-node-test-count.py")
        doc = tmp_path / "Agent.md"
        doc.write_bytes(NOT_UTF8)
        monkeypatch.setattr(mod, "DOC", doc)
        assert mod.main([]) == 2
        assert "cannot read" in capsys.readouterr().err
