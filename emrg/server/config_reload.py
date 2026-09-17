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

Three properties the design is built around, each of them a requirement in the
rant:

* **In place, for the next request.** Every field is assigned onto the *live*
  `LlmConfig` object the `LlmClient` already holds and reads per request, so a
  change applies to subsequent requests and can never rewrite a stream that is
  already in flight.
* **Atomic, and only when the whole revision is good.** The file is parsed and
  every field type-checked *before* the first assignment: a half-written or
  wrongly-typed file keeps the previous good configuration and leaves no
  half-applied state (a partially applied revision would be worse than the
  bug — it looks like it worked).
* **`model` is not assigned here.** It travels the `/model` path, because a
  real API-model change must invalidate the usage anchors (Dev.to 3dh3g) and
  re-resolve `context_window`. This module only *reports* the model the file
  asks for.

The retry rule: a revision that is rejected is recorded as *seen* (so a broken
file does not spam the log on every tick) and is retried as soon as the file
changes again — the host's next write is a new fingerprint.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from emrg.config import EmrgConfig, LlmConfig, config_path, load_config

logger = logging.getLogger("emrg.server")

#: How often the daemon stats the file. A stat is the whole cost of an idle
#: tick (`os.stat`, no read, no parse) — the file is read only when the
#: fingerprint moves.
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
    "vision": (bool,),
    "stream_options": (dict, type(None)),
    "context_refresh_interval_ms": (int,),
}


def reloadable_fields() -> tuple[str, ...]:
    """Every `LlmConfig` field this module applies, in declaration order."""
    names = tuple(f.name for f in dataclasses.fields(LlmConfig))
    return tuple(n for n in names if n not in EXCLUDED_FIELDS)


def fingerprint(path: Path) -> Optional[tuple[int, int]]:
    """`(mtime_ns, size)` for `path`, or None when it cannot be stat'ed.

    Chosen over a content hash because it is read-free: the tick that finds no
    change must not read or parse the file at all.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


@dataclass
class ReloadOutcome:
    """What one poll decided. `applied`/`model` and `error` are exclusive."""

    applied: list[str] = field(default_factory=list)
    model: Optional[str] = None
    error: Optional[str] = None

    @property
    def changed(self) -> bool:
        return bool(self.applied) or self.model is not None


def validate(cfg: LlmConfig) -> Optional[str]:
    """Why `cfg` may not be applied, or None when every field is well typed.

    Run over the **whole** revision before any assignment: the alternative
    (assign until something breaks) leaves a configuration that is half the old
    file and half the new one, which reads exactly like a successful reload.
    """
    for name in reloadable_fields():
        expected = _TYPES.get(name)
        if expected is None:
            continue  # untyped field (e.g. a future addition) — nothing to check
        value = getattr(cfg, name)
        strict_bool = expected == (bool,)
        if strict_bool:
            ok = isinstance(value, bool)
        else:
            ok = isinstance(value, expected) and not isinstance(value, bool)
        if not ok:
            return (
                f"{name} is {type(value).__name__}, expected "
                f"{'/'.join(t.__name__ for t in expected)}"
            )
    return None


class ConfigReloader:
    """Decides whether the config file has moved to a revision worth applying.

    Not a daemon: it never sleeps, never logs a policy and never touches the
    network. `poll()` answers one question per call — "has the file changed
    since I last looked, and if so what should happen?" — so the daemon's loop
    body and this decision can be tested apart.
    """

    def __init__(self, live: LlmConfig, path: Optional[Path] = None) -> None:
        self.live = live
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
        reason = validate(cfg.llm)
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
    if outcome.model is not None:
        parts.append(f"model→{outcome.model} (via the /model path)")
    return "; ".join(parts) if parts else "no effective field differs"
