"""Apply a `~/.emrg/config.toml` edit to a **running** daemon (rant 2026-09-17T16:52:57).

The host's complaint, measured: `emrg/server/__main__.py` reads the file once at
startup, so the only way to make an edit take effect was to SIGKILL the whole
daemon — which drops every connected client and rebuilds every scheduled
handler. Editing `api_key`, `base_url`, `model`, `vision`, `max_tokens` or
`temperature` silently did nothing until that happened (and from the GUI, where
no freshness check exists at all, not even then).

This module owns the decision half of the fix: when does a file change count as
a new revision, is that revision safe to apply, and which fields does it move.
The daemon owns the act half — it calls :meth:`ConfigReloader.poll` on a timer,
logs the changed keys, and routes a `model` change through the same path
`/model` uses (usage-anchor invalidation, `context_window` resolution,
`model_set` broadcast).

**Both sections of the file are covered** — `[llm]` and `[update]` (issue #1356).
Each is applied onto the live object its reader already holds: the `LlmConfig`
the `LlmClient` reads per request, and the `UpdateConfig` the `UpgradeManager`
reads once per tick. `update.enabled` / `update.delay_minutes` therefore take
effect on a running daemon, which they did not while the section was read once at
startup and the client's mtime-restart was the only thing that ever re-read it.

Four properties the design is built around, each of them a requirement in the
rant:

* **In place, for the next request.** Every field is assigned onto the *live*
  `LlmConfig` object the `LlmClient` already holds and reads per request, so a
  change applies to subsequent requests and can never rewrite a stream that is
  already in flight.
* **Atomic, and only when the whole revision is good.** The file is parsed and
  every field of every covered section type-checked *before* the first
  assignment: a half-written or wrongly-typed file keeps the previous good
  configuration and leaves no half-applied state (a partially applied revision
  would be worse than the bug — it looks like it worked).
* **`model` is not assigned here.** It travels the `/model` path, because a
  real API-model change must invalidate the usage anchors (Dev.to 3dh3g) and
  re-resolve `context_window`. This module only *reports* the model the file
  asks for.
* **A section is owned by the object it is given.** The `[update]` half applies
  only when an `UpdateConfig` was passed; the upgrade *interval* is not
  configurable and is not a field, so it stays out of reach by construction.

The retry rule: a revision that is rejected is recorded as *seen* (so a broken
file does not spam the log on every tick) and is retried as soon as the file
changes again — the host's next write is a new fingerprint.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from emrg.config import EmrgConfig, LlmConfig, UpdateConfig, config_path, load_config

logger = logging.getLogger("emrg.server")

#: How often the daemon looks at the file. The look is a read of a small TOML
#: file and a sha256 of it (`fingerprint`) — see that function for why a stat is
#: not enough to answer "did this change?".
POLL_INTERVAL_SECONDS = 2.0

#: Fields applied onto the live config, taken from the dataclass rather than
#: listed by hand so a new field cannot be silently unreloadable. `model` is
#: excluded by construction: it is the one field whose change runs a path (see
#: the module docstring), and it is reported in `ReloadOutcome.model` instead.
EXCLUDED_FIELDS = ("model",)

#: The type each reloadable field must have in the file. `bool` is checked
#: explicitly because it is a subclass of `int` — `max_tokens = true` would
#: otherwise pass an `isinstance(v, int)` test and become `1`.
_TYPES: dict[str, tuple[type, ...]] = {
    "base_url": (str,),
    "api_key": (str,),
    "max_tokens": (int,),
    "temperature": (int, float),
    "max_tool_rounds": (int,),
    "context_window": (int,),
    "auto_compact_threshold": (int, float),
    "models": (list,),
    # `vision` is derived at load time from `vision_default` + the matching
    # `[[llm.models]]` entry (`resolve_model_vision`, rant 2026-09-17T16:53:02),
    # so it is always a real bool and the file's `[llm] vision` key lands on
    # `vision_default` — the field that must be type-checked to catch a
    # `vision = "yes"`.
    "vision": (bool,),
    "vision_default": (bool,),
    "stream_options": (dict, type(None)),
    "context_refresh_interval_ms": (int,),
}

#: The `[update]` section (issue #1356), type-checked the same way. Derived from
#: the dataclass like `reloadable_fields()` is, so a new field cannot be silently
#: unreloadable — `DEVELOPMENT.md` promises the section is live, and a field
#: missing from this table would be applied with no check at all.
_UPDATE_TYPES: dict[str, tuple[type, ...]] = {
    "enabled": (bool,),
    "delay_minutes": (int,),
}


def reloadable_fields() -> tuple[str, ...]:
    """Every `LlmConfig` field this module applies, in declaration order."""
    names = tuple(f.name for f in dataclasses.fields(LlmConfig))
    return tuple(n for n in names if n not in EXCLUDED_FIELDS)


def update_reloadable_fields() -> tuple[str, ...]:
    """Every `UpdateConfig` field this module applies, in declaration order.

    Not the upgrade *interval*: `upgrade.TICK_INTERVAL` is hard-coded at 5
    minutes by host decision (rant 2026-08-20T12:33:59) and is not a field of
    this dataclass, so it cannot be mistaken for one here.
    """
    return tuple(f.name for f in dataclasses.fields(UpdateConfig))


def fingerprint(path: Path) -> Optional[str]:
    """A content hash of `path`, or None when it cannot be read.

    Not `(st_mtime_ns, st_size)`. That pair was the first design - "read-free, a stat
    is the whole cost of an idle tick" - and the `windows-2025` leg falsified it
    (measured 2026-09-17): two writes milliseconds apart share a timestamp there, so
    an edit that also keeps the file's size leaves the stat **identical** and the
    revision is silently never applied, which is the exact complaint this module
    exists to remove ("I edited it and nothing happened"). Equality is the ambiguous
    case - the tick that finds no change is the tick that cannot rule one out - so
    there is no sound stat fast path; the bytes have to be read. The cost is one
    small TOML read plus a sha256 per `POLL_INTERVAL_SECONDS` tick, and it is paid
    on every platform rather than behind a Windows branch, because a filesystem with
    coarse timestamps is a property of the mount, not of the OS.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


@dataclass
class ReloadOutcome:
    """What one poll decided. `applied`/`model` and `error` are exclusive."""

    applied: list[str] = field(default_factory=list)
    update_applied: list[str] = field(default_factory=list)
    model: Optional[str] = None
    error: Optional[str] = None

    @property
    def changed(self) -> bool:
        return bool(self.applied) or bool(self.update_applied) or self.model is not None


def _first_type_error(
    obj: object, names: tuple[str, ...], table: dict[str, tuple[type, ...]], prefix: str = ""
) -> Optional[str]:
    """The first field in `names` whose value is not the type the file must give.

    `bool` is checked strictly because it is a subclass of `int` — a
    `max_tokens = true` would otherwise pass an `isinstance(v, int)` test and
    become `1`. `prefix` names the section for a field that is not under
    `[llm]`, so a rejected revision says where to look.
    """
    for name in names:
        expected = table.get(name)
        if expected is None:
            continue  # untyped field (e.g. a future addition) — nothing to check
        value = getattr(obj, name)
        strict_bool = expected == (bool,)
        if strict_bool:
            ok = isinstance(value, bool)
        else:
            ok = isinstance(value, expected) and not isinstance(value, bool)
        if not ok:
            return (
                f"{prefix}{name} is {type(value).__name__}, expected "
                f"{'/'.join(t.__name__ for t in expected)}"
            )
    return None


def validate(cfg: LlmConfig, update: Optional[UpdateConfig] = None) -> Optional[str]:
    """Why this revision may not be applied, or None when every field is well typed.

    Run over the **whole** revision — every section this reloader owns — before
    any assignment: the alternative (assign until something breaks) leaves a
    configuration that is half the old file and half the new one, which reads
    exactly like a successful reload. The section boundary is the object
    boundary: `update` is None for a reloader that was not given an
    `UpdateConfig`, and that section is then neither checked nor applied.
    """
    reason = _first_type_error(cfg, reloadable_fields(), _TYPES)
    if reason is not None or update is None:
        return reason
    return _first_type_error(update, update_reloadable_fields(), _UPDATE_TYPES, "[update] ")


class ConfigReloader:
    """Decides whether the config file has moved to a revision worth applying.

    Not a daemon: it never sleeps, never logs a policy and never touches the
    network. `poll()` answers one question per call — "has the file changed
    since I last looked, and if so what should happen?" — so the daemon's loop
    body and this decision can be tested apart.
    """

    def __init__(
        self,
        live: LlmConfig,
        path: Optional[Path] = None,
        live_update: Optional[UpdateConfig] = None,
    ) -> None:
        self.live = live
        #: The `[update]` object the daemon's `UpgradeManager` holds (issue
        #: #1356). Mutated **in place** for the same reason `live` is: the
        #: manager captured this object at construction and reads it once per
        #: 5-minute tick, so an assignment here is what the next tick sees.
        #: None means this reloader was not given the section and does not own it.
        self.live_update = live_update
        self.path = Path(path) if path is not None else config_path()
        # The daemon loaded this file at startup, so its current bytes are
        # already in force: the baseline is "seen", not "pending".
        self._seen = fingerprint(self.path)
        # Kept for the log line: a revision that changes nothing effective is
        # still worth one line, because "I saved the file and nothing
        # happened" is exactly what the host could not distinguish before.
        self.rejected_revisions = 0

    def poll(self) -> Optional[ReloadOutcome]:
        """None when nothing changed; otherwise what to do about the new revision."""
        fp = fingerprint(self.path)
        if fp is None:
            return None  # file gone (or unreadable) — keep the live config
        if fp == self._seen:
            return None
        self._seen = fp
        try:
            cfg = load_config(self.path)
        except Exception as exc:  # TOMLDecodeError, OSError, UnicodeDecodeError…
            self.rejected_revisions += 1
            return ReloadOutcome(error=f"{exc.__class__.__name__}: {exc}")
        reason = validate(cfg.llm, cfg.update if self.live_update is not None else None)
        if reason is not None:
            self.rejected_revisions += 1
            return ReloadOutcome(error=reason)
        return self._apply(cfg)

    def _apply(self, cfg: EmrgConfig) -> ReloadOutcome:
        """Assign the new revision onto the live config and report what moved."""
        outcome = ReloadOutcome()
        for name in reloadable_fields():
            new = getattr(cfg.llm, name)
            if getattr(self.live, name) != new:
                setattr(self.live, name, new)
                outcome.applied.append(name)
        if self.live_update is not None:
            # In place, so the `UpgradeManager` that holds this object reads the
            # new values on its next tick without being reconstructed — the same
            # property the `[llm]` half has, and the reason a reload needs no
            # restart of anything.
            for name in update_reloadable_fields():
                new = getattr(cfg.update, name)
                if getattr(self.live_update, name) != new:
                    setattr(self.live_update, name, new)
                    outcome.update_applied.append(name)
        if cfg.llm.model != self.live.model:
            # Reported, never assigned: the daemon runs the /model path (anchor
            # invalidation + context_window resolution + broadcast) and only
            # then does `live.model` move.
            outcome.model = cfg.llm.model
        return outcome


def describe(outcome: ReloadOutcome) -> str:
    """One log line a host can read to self-verify the reload.

    Names the keys that moved (in declaration order) so "did my edit take
    effect?" is answerable from `emrgd.log` alone, which is the point of the
    change — before it, the file could be edited with no observable effect.
    """
    if outcome.error is not None:
        return f"rejected (previous config kept): {outcome.error}"
    parts = []
    if outcome.applied:
        parts.append("changed=" + ",".join(outcome.applied))
    if outcome.update_applied:
        parts.append("[update] changed=" + ",".join(outcome.update_applied))
    if outcome.model is not None:
        parts.append(f"model→{outcome.model} (via the /model path)")
    return "; ".join(parts) if parts else "no effective field differs"
