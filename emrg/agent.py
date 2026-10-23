"""The Agent: the one directory a session runs in.

An `Agent` is a directory and nothing else. It is not a repository, a workspace or a
registry entry — those are facts *about* a directory that other layers keep, and
letting one word answer all of them is what made the older vocabulary ambiguous: a
name could mean the directory, the alias it is registered under, or a field of a
record.

What the class owns is the layout derived from that directory:

    <base_dir>/.emrg                emrg_dir
    <base_dir>/.emrg/sessions       sessions_dir
    <base_dir>/.emrg/memory         memory_dir

Nothing else lives here on purpose. An Agent runs no model, keeps no history and knows
nothing about sandbox tiers; those belong to the session that runs in it and to the
daemon that schedules it, and a field added here would be a second place to keep a fact
that already has an owner. The class exists for one reason: a path spelled out at every
site that needs it is one convention per site, so a layout change becomes a search; one
object with one derivation makes it an edit.

`base_dir` is stored **exactly as given** — no `resolve()`, no `expanduser()`, no
trailing-slash normalisation, and a `str` stays a `str`. The value is compared for
equality downstream against strings that are already recorded elsewhere (what a client
sent, what a registry entry kept, what `meta.json` stored). Normalising it here would
make two spellings of one directory disagree with every reader that normalised only its
own side. A symlink, a trailing slash and a `..` all survive unchanged.
"""

from __future__ import annotations

from pathlib import Path


class Agent:
    """One directory a session runs in: holds it, and derives its layout."""

    def __init__(self, base_dir: Path | str | Agent) -> None:
        # Idempotent, so a caller holding an Agent cannot end up with a second wrapper
        # around it. Wrapping twice would be harmless only while every derivation
        # agreed with every other, which is the assumption this class exists to remove.
        if isinstance(base_dir, Agent):
            base_dir = base_dir.base_dir
        self.base_dir = base_dir

    @property
    def emrg_dir(self) -> Path:
        """``<base_dir>/.emrg`` — everything this Agent keeps lives under here."""
        return Path(self.base_dir) / ".emrg"

    @property
    def sessions_dir(self) -> Path:
        """``<base_dir>/.emrg/sessions`` — one directory per session, named by its id."""
        return self.emrg_dir / "sessions"

    @property
    def memory_dir(self) -> Path:
        """``<base_dir>/.emrg/memory`` — memory shared by every session in this Agent."""
        return self.emrg_dir / "memory"

    def __eq__(self, other: object) -> bool:
        # Value equality on the stored value, with no normalisation on either side, so
        # `Agent("x")` and `Agent(Path("x"))` stay different objects — exactly as the
        # two paths they carry are different values.
        if not isinstance(other, Agent):
            return NotImplemented
        return self.base_dir == other.base_dir

    def __hash__(self) -> int:
        return hash(self.base_dir)

    def __repr__(self) -> str:
        return f"Agent({self.base_dir!r})"


def as_agent(value: Agent | Path | str) -> Agent:
    """The Agent for a directory, whether the caller holds one or only its path.

    This is what lets every existing caller keep passing a path: the one place that
    knows "a path here means an Agent" is this function, instead of each of them.
    """
    return value if isinstance(value, Agent) else Agent(value)
