"""The config-file write guard — measured firing, not merely declared.

`tests/conftest.py::_guard_real_config_files` is the mechanism behind a host red
line: `~/.emrg/config.toml` is the file whose corruption is on record, where a
stray appended line made `tomllib` raise at line 48 and the daemon exited during
startup with the traceback landing in `emrgd-crash.log` rather than in any channel
a startup-failure report reads. A guard that nothing demonstrates failing is a
claim rather than a guard, so every seam it patches is exercised here.

**No test in this file can reach the host's files.** The three real paths are only
ever arguments to `_is_real_config_target`, a pure predicate that resolves and
opens nothing — the shape that lets a test name the host's own path without the
test's safety depending on the guard working. Every refusal below is driven by
pointing `_REAL_CONFIG_FILES` at a `tmp_path`, so the write that must fail is a
write into a directory this test created.

**What each test pins is the refusal, not the wording of the seam that produced
it** — except where the seam is the point. The guard may name `open(mode='a')`
or `io.open(mode='w')`; either way the write must not land. Pinning the label
instead would let a seam that carries no write look load-bearing, and would fail
a change that merely moved one write from one wrapper to another.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests import conftest

# The invariant every refusal shares, whatever wrapper produced it.
REFUSAL = "keep tests hermetic"


@pytest.fixture
def guarded_file(tmp_path, monkeypatch):
    """Point the guard at one file inside this test's own directory.

    The directory is named `.emrg` so the cheap pre-filter in
    `_is_real_config_target` does not answer before the membership test does —
    a guard proven only on paths the pre-filter skips would be proven on
    nothing.
    """
    victim = tmp_path / ".emrg" / "config.toml"
    victim.parent.mkdir(parents=True)
    monkeypatch.setattr(conftest, "_REAL_CONFIG_FILES", (victim.resolve(),))
    return victim


def test_the_three_real_files_are_named():
    """All three, not two: `config.toml` is the one the incident was about."""
    home = Path.home()
    assert conftest._is_real_config_target(home / ".emrg" / "config.toml")
    assert conftest._is_real_config_target(home / ".emrg" / "projects.yml")
    assert conftest._is_real_config_target(home / ".emrg" / "tasks.yml")


def test_a_scratch_path_is_never_a_real_file(tmp_path):
    """The counter-direction: a tmp file of the same name must pass through."""
    assert not conftest._is_real_config_target(tmp_path / "config.toml")
    assert not conftest._is_real_config_target(tmp_path / ".emrg" / "config.toml")
    assert not conftest._is_real_config_target(None)
    assert not conftest._is_real_config_target(3)


def test_yaml_writer_is_refused(guarded_file):
    """`projects.yml` / `tasks.yml`'s own seam, which the guard already had."""
    from emrg.server import scheduler as sched_mod

    with pytest.raises(AssertionError, match=REFUSAL):
        sched_mod.atomic_write_yaml({"a": 1}, guarded_file)
    assert not guarded_file.exists()


def test_path_write_text_is_refused(guarded_file):
    """A `Path` write reaches the guard through `Path.open` → `io.open`."""
    with pytest.raises(AssertionError, match=REFUSAL):
        guarded_file.write_text("probe\n", encoding="utf-8")
    assert not guarded_file.exists()


def test_path_write_bytes_is_refused(guarded_file):
    with pytest.raises(AssertionError, match=REFUSAL):
        guarded_file.write_bytes(b"probe\n")
    assert not guarded_file.exists()


def test_open_in_append_mode_is_refused(guarded_file):
    """The recorded incident's shape: an append, which neither method above is."""
    with pytest.raises(AssertionError, match=REFUSAL):
        open(guarded_file, "a", encoding="utf-8")
    assert not guarded_file.exists()


def test_open_for_reading_is_allowed(guarded_file):
    """A read is not a write: the guard must not make the file unreadable."""
    guarded_file.touch()
    with open(guarded_file, "r", encoding="utf-8") as fh:
        assert fh.read() == ""


def test_ensure_config_cannot_create_the_real_file(guarded_file, monkeypatch):
    """The seam that actually writes `config.toml`, driven end to end.

    `ensure_config` is the product's own creator of this file, and it writes
    through `cfg_path.write_text` — not through the YAML seam. This is the call
    the guard exists to stop and could not see before.
    """
    import emrg.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "config_path", lambda: guarded_file)
    with pytest.raises(AssertionError, match=REFUSAL):
        cfg_mod.ensure_config()
    assert not guarded_file.exists()
