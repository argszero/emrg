"""Every ``config.toml`` field is named by the GUI's field list — and the list tells the truth.

The defect this guards (issue #3221): ``emrg/gui/main.js`` kept a whitelist of five string keys,
and the writer set only the keys that survived it, so a field the form sent but the whitelist did
not name was **dropped in silence** — the save reported success and the file did not move. Twelve
of the seventeen fields here had no control at all. Nothing failed, which is why nobody noticed the
list was growing.

So the list is not prose any more: it is ``emrg/gui/config-fields.json``, read by the main process
to validate and write, by the renderer to draw a control per ``control: "auto"`` entry, and here to
compare against the dataclasses that actually own the defaults. A field added to ``emrg/config.py``
without an entry reddens this test; an entry whose declared default has drifted from the dataclass
reddens it too, because a hint that quotes a stale number is the same defect one step later.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from emrg import config as emrg_config

MANIFEST = Path(__file__).resolve().parents[1] / "emrg" / "gui" / "config-fields.json"

#: The four sections ``[llm] [update] [sandbox] [client]``, and the dataclass that owns each.
SECTIONS = {
    "llm": emrg_config.LlmConfig,
    "update": emrg_config.UpdateConfig,
    "sandbox": emrg_config.SandboxConfig,
    "client": emrg_config.ClientConfig,
}

#: The kinds ``emrg/gui/config_fields.js`` can coerce. A typo'd kind would sail through the JSON
#: and only bite when a value was typed, so it is named here as well.
KINDS = {"int", "float", "bool", "text", "usage"}


def _manifest() -> list[dict]:
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(raw.get("fields"), list), "the manifest has no `fields` list"
    return raw["fields"]


def _built_default(field: dataclasses.Field):
    """A dataclass field's default, calling its factory when it has one instead."""
    if field.default is not dataclasses.MISSING:
        return field.default
    return field.default_factory()  # type: ignore[misc]


def test_the_manifest_reads_and_names_every_section() -> None:
    fields = _manifest()
    assert fields, "the field list is empty"
    seen = {entry["section"] for entry in fields}
    assert seen == set(SECTIONS), f"the manifest covers {sorted(seen)}, the dataclasses are {sorted(SECTIONS)}"


@pytest.mark.parametrize("section", sorted(SECTIONS))
def test_every_dataclass_field_has_an_entry(section: str) -> None:
    """A field with no entry is a field with no control — the exact silence issue #3221 is."""
    owner = SECTIONS[section]
    named = {entry["field"] for entry in _manifest() if entry["section"] == section}
    missing = sorted(f.name for f in dataclasses.fields(owner) if f.name not in named)
    assert not missing, (
        f"{owner.__name__} declares {missing} and the GUI's field list does not name them: add an "
        f"entry to {MANIFEST.name} and a control for it, or mark it `control: covered` naming the "
        f"entry whose control already reaches the same key"
    )


def test_every_entry_names_a_real_field() -> None:
    """An entry for a field that no longer exists is a control writing nothing."""
    for entry in _manifest():
        owner = SECTIONS[entry["section"]]
        names = {f.name for f in dataclasses.fields(owner)}
        assert entry["field"] in names, f"{entry['section']}.{entry['field']} is not a field of {owner.__name__}"


def test_declared_defaults_match_the_dataclass() -> None:
    """The number a hint quotes is the number the daemon runs on, or this reddens."""
    for entry in _manifest():
        owner = SECTIONS[entry["section"]]
        field = next(f for f in dataclasses.fields(owner) if f.name == entry["field"])
        expected = _built_default(field)
        assert "default" in entry, f"{entry['field']} declares no default, so a control states none"
        assert entry["default"] == expected, (
            f"{entry['section']}.{entry['field']} declares the default {entry['default']!r} while "
            f"{owner.__name__} uses {expected!r}: one of the two moved and the other did not"
        )


def test_auto_entries_declare_a_kind_the_writer_can_coerce() -> None:
    for entry in _manifest():
        if entry.get("control") != "auto":
            continue
        assert entry.get("kind") in KINDS, f"{entry['field']} declares kind {entry.get('kind')!r}, not one of {sorted(KINDS)}"
        assert entry.get("ipc"), f"{entry['field']} is drawn by the form but names no ipc field to send"
        assert entry.get("tab"), f"{entry['field']} names no settings page to appear on"


def test_a_covered_entry_names_an_entry_that_is_drawn() -> None:
    """`vision_default` rides on `vision`'s checkbox: one file key, so one control."""
    drawn = {entry["field"] for entry in _manifest() if entry.get("control") == "auto"}
    for entry in _manifest():
        if entry.get("control") != "covered":
            continue
        assert entry.get("coveredBy") in drawn, (
            f"{entry['field']} is covered by {entry.get('coveredBy')!r}, which no control draws"
        )


def test_ipc_field_names_are_unique() -> None:
    """Two entries sharing an ipc name would write each other's value."""
    names = [entry["ipc"] for entry in _manifest() if entry.get("ipc")]
    assert len(names) == len(set(names)), f"duplicate ipc names: {sorted({n for n in names if names.count(n) > 1})}"
