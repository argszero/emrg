"""Build a TaskHandler the way the scheduler does — from its record.

A handler holds a name and nothing else (rant 2026-09-28T09:54:10): at every wake it
reads its own record out of tasks.yml and derives type / interval / config / project /
sandbox from that, rather than from a snapshot taken at construction. So a test that
needs a handler with a particular configuration hands the record it wants to
`make_handler`, and the production derivation runs unchanged.

Only the file read is short-circuited. That read has its own tests, which write a real
tasks.yml into a tmp path and drive the real `TaskHandler.run()` loop
(`tests/test_scheduler.py`, the `record is the only input` block) — a test that wants to
prove the file is the source of truth must use those, not this helper.
"""

from __future__ import annotations

from pathlib import Path

from emrg.protocol import InstanceIdentity
from emrg.server.scheduler import TaskHandler


def make_handler(
    name: str = "emrg-task",
    config: dict | None = None,
    interval: int = 60,
    identity: InstanceIdentity | None = None,
    sandbox: str | None = None,
    template_path: Path | None = None,
    type: str = "evolution",
    enabled: bool = True,
) -> TaskHandler:
    """A TaskHandler whose record is `config` / `interval` / `type` / `sandbox`."""
    handler = TaskHandler(name=name, identity=identity or InstanceIdentity())
    record: dict = {
        "name": name,
        "type": type,
        "config": config or {},
        "interval": interval,
        "enabled": enabled,
    }
    if sandbox is not None:
        record["sandbox"] = sandbox
    handler._apply_record(record)
    if template_path is not None:
        # The record's `type` picks the template, so a test that wants a template no
        # built-in type names says so directly. Extra types with a custom template are
        # covered by the loader, which resolves them from disk.
        handler._derived["template_path"] = Path(template_path)
    return handler
