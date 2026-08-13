"""Shared pytest fixtures — hermeticity guards.

2026-08-13 incident: the full test suite intermittently wrote pytest
temp paths into the real ~/.emrg/projects.yml. The mechanism is a
feedback loop — once a stale entry lands in the file (seeded by the
workspace self-heal family), subsequent tests re-read it and re-write
it, advancing the pytest-N counter on every full run. The daemon's
per-cycle repair (#734) eventually cleans it, but until then the host's
GUI project picker shows a dead path.

This autouse fixture makes any write to the REAL ~/.emrg/projects.yml a
hard test failure: the offending test is named immediately instead of
the pollution being discovered later (precedent: #583 assertPortFileInTmp
sandbox guard for the emrgd.port file).
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _guard_real_projects_yml(monkeypatch):
    """Fail any test that writes the real ~/.emrg/projects.yml."""
    real = (Path.home() / ".emrg" / "projects.yml").resolve()

    import emrg.server.daemon as daemon_mod
    import emrg.server.scheduler as sched_mod

    orig = sched_mod.atomic_write_yaml
    assert orig is daemon_mod.atomic_write_yaml, "both modules must share atomic_write_yaml"

    def guarded(data, path, **kwargs):
        if Path(path).resolve() == real:
            raise AssertionError(
                "test attempted to write the real ~/.emrg/projects.yml; "
                f"keep tests hermetic (target={path!r})"
            )
        return orig(data, path, **kwargs)

    monkeypatch.setattr(sched_mod, "atomic_write_yaml", guarded)
    monkeypatch.setattr(daemon_mod, "atomic_write_yaml", guarded)
