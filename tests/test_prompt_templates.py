"""Wiring guards for the task-type prompt templates.

Why this file exists
--------------------
The daemon renders every task prompt through jinja2 with
``undefined=jinja2.Undefined`` (see ``TaskHandler._build_evolution_prompt``). That
choice is deliberate — the old ``str.format()`` crashed on a missing placeholder —
but it has a sharp edge: a name that is not in the builder's context renders as an
**empty string** instead of raising. A typo therefore breaks nothing a test can see.
The prompt simply tells the agent to write into a blank path, or to follow a blank
instruction, and the whole suite stays green.

These tests close that hole from both ends:

1. ``test_every_prompt_template_renders_through_the_real_builder`` — every built-in
   task type's template renders through the real builder (real context, real env)
   with a *minimal* task config, so the ``{% if ... %}``-guarded optional sections
   take their unset branch, and no unrendered ``{{ }}`` / ``{% %}`` tag is left.
2. ``test_no_prompt_names_a_placeholder_the_builder_does_not_provide`` — the same
   templates re-rendered with ``StrictUndefined``, which *raises* on a missing name,
   against the context the builder actually produced. That is the check which turns
   a silent blank into a failure. The strict renderer is self-tested with a planted
   typo, so a device that silently stops detecting anything cannot pass.

Named limit: this pins the wiring, not the prose. It cannot show that an agent
follows the procedure, that optional fields (``{% if task.extra_prompt %}``,
``{% if project.description %}``) are set in the host's config, or that the prompt's
content is any good.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import yaml

from emrg.protocol import InstanceIdentity
from emrg.server import scheduler as mod
from emrg.server.scheduler import TaskHandler
from emrg.tools import bash_tool

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "emrg" / "server"

# A reference to a variable or a block tag that survived rendering.
LEFTOVER_TAG = re.compile(r"\{\{|\{%")

# Optional task/project fields the prompts guard on. Populated only by the strict
# render, so that a "missing" name there is a typo rather than a design choice.
FULL_TASK_CONFIG: dict = {
    "project": "demoproj",
    "role": "committer",
    "author_id": "argszero",
    "keywords": "demo, wiring",
    "extra_prompt": "EXTRA-PROMPT-MARKER",
    "allow_self_merge": True,
}
FULL_PROJECT_ENTRY: dict = {
    "name": "demoproj",
    "description": "PROJECT-DESCRIPTION-MARKER",
}


def _write_projects_yml(tmp_path: Path, name: str, entry: dict) -> Path:
    project_dir = tmp_path / name
    project_dir.mkdir(exist_ok=True)
    row = {"path": str(project_dir)}
    row.update(entry)
    (tmp_path / "projects.yml").write_text(
        yaml.safe_dump([row]), encoding="utf-8"
    )
    return project_dir


def _make_handler(
    tmp_path: Path,
    monkeypatch,
    template_name: str,
    config: dict,
    project_entry: dict | None = None,
) -> TaskHandler:
    """A real handler, from a hermetic config dir, pointed at a real template."""
    monkeypatch.setattr(mod, "config_dir", lambda: tmp_path)
    name = config.get("project", "demoproj")
    _write_projects_yml(tmp_path, name, project_entry or {"name": name})
    return TaskHandler(
        name="demo-task",
        config=config,
        interval=300,
        identity=InstanceIdentity(),
        template_path=PROMPTS_DIR / template_name,
    )


def _builtin_templates() -> list[tuple[str, str]]:
    return sorted(mod.TASK_TEMPLATES.items())


def test_every_prompt_template_renders_through_the_real_builder(tmp_path, monkeypatch) -> None:
    """Each built-in task type renders, with the optional sections unset."""
    minimal = {"project": "demoproj"}
    for task_type, filename in _builtin_templates():
        handler = _make_handler(tmp_path, monkeypatch, filename, minimal)
        rendered = handler._build_evolution_prompt()
        leftover = LEFTOVER_TAG.search(rendered)
        assert leftover is None, (
            f"{task_type}/{filename}: unrendered {leftover.group()!r} left in the "
            f"prompt (Jinja saw it as text, so the agent would too)"
        )
        assert len(rendered) > 500, (
            f"{task_type}/{filename}: only {len(rendered)} chars rendered — "
            f"almost everything came out empty"
        )


def test_no_prompt_names_a_placeholder_the_builder_does_not_provide(
    tmp_path, monkeypatch
) -> None:
    """StrictUndefined: a name the builder does not provide fails loudly.

    This is the guard that the daemon's own ``Undefined`` cannot give: rendering the
    real templates against the real context with an env that raises on a missing
    name. Without it, ``{{ evolution_cwd_typo }}`` renders as ``''`` and every test
    in the suite stays green.
    """
    real_env = jinja2.Environment
    captured: dict = {}

    class _RecordingTemplate:
        def __init__(self, inner: jinja2.Template) -> None:
            self._inner = inner

        def render(self, *args, **kwargs) -> str:
            captured.update(kwargs)
            return self._inner.render(*args, **kwargs)

    class _RecordingEnvironment(real_env):  # type: ignore[misc,valid-type]
        def from_string(self, source, *args, **kwargs):
            inner = real_env.from_string(self, source, *args, **kwargs)
            return _RecordingTemplate(inner)

    monkeypatch.setattr(jinja2, "Environment", _RecordingEnvironment)
    handler = _make_handler(
        tmp_path,
        monkeypatch,
        "evolution_prompt.md",
        dict(FULL_TASK_CONFIG),
        dict(FULL_PROJECT_ENTRY),
    )
    handler._build_evolution_prompt()
    monkeypatch.setattr(jinja2, "Environment", real_env)

    # Self-test of the device: if the capture silently stopped working, the strict
    # render below would pass for the wrong reason (nothing to check).
    assert len(captured) >= 15, (
        f"captured only {len(captured)} context keys from the builder — the "
        f"recording environment is not in the render path, so this test would "
        f"prove nothing"
    )
    assert "task" in captured and "project" in captured

    strict = real_env(undefined=jinja2.StrictUndefined)
    with_typo = strict.from_string("{{ evolution_cwd_typo }}")
    try:
        with_typo.render(**captured)
    except jinja2.UndefinedError:
        pass
    else:
        raise AssertionError(
            "the strict renderer did not raise on a planted typo — the check "
            "below would be vacuous"
        )

    for task_type, filename in _builtin_templates():
        text = (PROMPTS_DIR / filename).read_text(encoding="utf-8")
        try:
            strict.from_string(text).render(**captured)
        except jinja2.UndefinedError as exc:
            raise AssertionError(
                f"{task_type}/{filename} references a name the builder does not "
                f"provide ({exc}); with the daemon's Undefined it would silently "
                f"render as an empty string"
            ) from exc


# `{{ evolution_cwd }}` followed by the rest of the path it names.
EVOLUTION_CWD_REF = re.compile(r"\{\{\s*evolution_cwd\s*\}\}([^\s`)\"'|,;]*)")

# A path suffix that routes through a `memory/` directory.
_MEMORY_SEGMENT = re.compile(r"(?:^|/)memory/")


def _evolution_memory_refs(text: str) -> list[str]:
    """Path suffixes of ``{{ evolution_cwd }}`` references that name a memory dir."""
    return [
        suffix
        for suffix in EVOLUTION_CWD_REF.findall(text)
        if _MEMORY_SEGMENT.search(suffix)
    ]


def test_prompt_memory_writes_land_where_the_sandbox_allows_them() -> None:
    """A prompt's memory path must be one the ``workspace-write`` sandbox trusts.

    Measured 2026-09-14 (rant 2026-09-14T14:35:47): `open_source_prompt.md` sent the
    agent's identity file and its "key findings" to `{{ evolution_cwd }}/memory/`,
    i.e. `~/.emrg/evolution/memory/`. That is out of the sandbox's boundary — every
    such write is refused with "blocked write outside workspace", which the rant
    reports an open-source task hitting 32 times in one day — and it is the wrong
    root anyway: the evolution data root is `{{ evolution_cwd }}/.emrg/`, the only
    part of `{{ evolution_cwd }}` `bash_tool._trusted_write_zones()` trusts and the
    only memory the daemon loads. The directory exists, so a write that gets through
    by a route the command-line scan cannot see (an `open()` inside a heredoc, as the
    rant documents) lands in a folder no memory loader reads.

    The expected location is taken from the sandbox's own trust list rather than
    copied here, so if that list moves, this test reports the prompts may be stale
    instead of agreeing with a second copy of the rule.

    Named limit: only ``{{ evolution_cwd }}``-rooted *memory* paths are checked.
    The state-file / reflection-file mechanism the same rant retires is covered by
    ``test_retired_state_file_mechanism_is_gone_or_being_swept`` below, and the
    paths that replaced it live in the memory root this test measures.
    """
    root = Path(mod.EVOLUTION_CWD)
    zones = bash_tool._trusted_write_zones()
    assert zones, "no trusted write zone — this check would pass vacuously"

    # Self-test of the device: it must reject the exact shape the rant measured,
    # and the machine must actually consider that shape outside the boundary.
    planted = _evolution_memory_refs("write `{{ evolution_cwd }}/memory/identity.md`")
    assert planted == ["/memory/identity.md"], planted
    assert not any(
        bash_tool._is_within(str(root / "memory/identity.md"), zone) for zone in zones
    ), (
        "`~/.emrg/evolution/memory/` is inside a trusted zone on this machine, so "
        "this test cannot discriminate between the two roots here"
    )

    checked = 0
    for task_type, filename in _builtin_templates():
        text = (PROMPTS_DIR / filename).read_text(encoding="utf-8")
        for suffix in _evolution_memory_refs(text):
            target = str(root / suffix.lstrip("/"))
            assert any(
                bash_tool._is_within(target, zone) for zone in zones
            ), (
                f"{task_type}/{filename}: tells the agent to write {target!r}, "
                f"which the workspace-write sandbox blocks (trusted zones: {zones}); "
                f"the evolution memory root is `{{{{ evolution_cwd }}}}/.emrg/memory/`"
            )
            checked += 1
    assert checked >= 2, (
        f"only {checked} memory path(s) found across the templates — the scan is "
        f"not looking at what it thinks it is"
    )


# The memory root the sweep re-based every phase hand-off onto: writable (the
# guard above proves it) and readable via the `read` tool — but its own index is
# NOT the one the daemon embeds.
SWEEP_MEMORY_ROOT = "evolution_cwd }}/.emrg/memory"
_INDEX_IN_PROMPT = re.compile(r"(part of|embedded in) this prompt", re.IGNORECASE)


def test_no_template_calls_the_write_root_index_its_own_prompt_index() -> None:
    """A root that is only *writable* must not be described as *loaded*.

    Measured 2026-09-14 (cyc20260914-175549), on this branch before the fix: three
    of the paragraphs re-based onto memory entries under
    `{{ evolution_cwd }}/.emrg/memory/` also called that directory's index "part
    of this prompt". It is not. `_collect_memory_data` embeds
    `session.cwd/.emrg/memory/MEMORY.md` — the *task project's* index — and the
    session index, and never `{{ evolution_cwd }}/.emrg/memory/MEMORY.md`;
    measured by pointing `EVOLUTION_CWD` at a directory whose memory index carries
    a marker and rendering the system prompt through the real builder: the marker
    stays out while the project's appears. The two roots differ by construction on
    this installation — `{{ evolution_cwd }}` is `~/.emrg/evolution` (988 files,
    the durable record) while the embedded index belongs to the session's cwd,
    `{{ evolution_cwd }}/emrg`.

    The pairing is what is false, not the path: writing memory entries under that
    root is correct (the sandbox trusts it, guarded above), and saying "the memory
    index is embedded in this prompt" without naming a path is correct too. Naming
    that path *and* claiming its index is in the prompt points the agent at a
    place whose contents it will not find — the same defect family the sweep
    exists to remove.
    """
    planted = (
        "Record findings under `{{ evolution_cwd }}/.emrg/memory/`, "
        "whose index is part of this prompt."
    )
    assert SWEEP_MEMORY_ROOT in planted and _INDEX_IN_PROMPT.search(planted), (
        "the detector no longer detects the shape it was written for"
    )

    paragraphs_naming_the_root = 0
    suspects: list[str] = []
    for _task_type, filename in _builtin_templates():
        text = (PROMPTS_DIR / filename).read_text(encoding="utf-8")
        for paragraph in text.split("\n\n"):
            if SWEEP_MEMORY_ROOT in paragraph:
                paragraphs_naming_the_root += 1
                if _INDEX_IN_PROMPT.search(paragraph):
                    suspects.append(f"{filename}: {paragraph.strip()[:140]}")

    assert paragraphs_naming_the_root >= 3, (
        f"only {paragraphs_naming_the_root} paragraph(s) name the memory root — "
        f"the scan is not looking where it thinks it is"
    )
    assert not suspects, (
        "these paragraphs name the memory root and also claim its index is in the "
        "prompt, which the daemon never embeds:\n  " + "\n  ".join(suspects)
    )


# Templates still teaching the retired state-file / reflection-file mechanism.
# Rant 2026-09-14T14:35:47 removes it wholesale ("the session itself is the
# memory"); the sweep landed one template at a time, and `promote_prompt.md` was
# the last of them — so the set is now **empty**, and empty is the finished state
# rather than a decoration: it is asserted below, because a template that starts
# teaching the mechanism again must be red, not silently re-added here.
PENDING_STATE_SWEEP: set[str] = set()

# The retired mechanism's fingerprints: the two file names, and the prose that
# told the agent to read/write a state or reflection file. The prose arm matches
# the bare noun phrase, not only "the state file": the first version demanded the
# article, and paper_prompt.md came through the sweep still telling the agent to
# read "state file" for its arXiv keywords (measured 2026-09-14,
# cyc20260914-170405). A line that *denies* the file — "there is no state file,
# the session is the state" — is the replacement text itself, so it stays legal.
#
# ⚠️ Three more spellings, measured 2026-09-15 (cyc20260915-100310). The pattern
# above matched an underscore-prefixed file name, so it was blind to the name
# *without* its project prefix — and `promote_prompt.md` writes it that way:
# line 38 is `- Reflection log: \u2026/reflections.md`, which the old pattern did not
# see. Same for the section heading `### 5. Reflection Log` and for "diary", the
# third name the rant lists. Measured on master `e6eaaee4`: 5 of that template's
# 30 mentions were invisible, and every swept template stayed at 0 in both
# directions (so this is a widening, not a rewrite).
#
# The five sit in a template that is still *pending*, so the guard was right
# about it for the wrong reason. The defect is what happens next: the same
# spelling in a **swept** template is a silent pass, and a guard whose blind spot
# is a plausible spelling of the thing it forbids reports success by not looking
# — the failure this file's own docstring names.
_RETIRED_MECHANISM = re.compile(
    # The file names: the prefixed form (`promote_state.md`) and the bare one
    # (`reflections.md`, `state.md`). The boundary is `(?<![-\w])`, not `(?<!no )`
    # alone, so a name only counts when it *is* a name — with the loose lookbehind
    # `read realestate.md` was flagged as the retired `state.md` (measured
    # 2026-09-15, cyc20260915-124341).
    r"_state\.md|(?<![-\w])state\.md"
    r"|_reflections\.md|(?<![-\w])reflections?\.md"
    # …the same thing written as prose, dash / space / underscore…
    r"|(?<!no )state[-\s_]files?"
    r"|(?<!no )reflections?[-\s_]files?"
    # …its other section name, with or without a separator…
    r"|(?<!no )reflection[-\s_]?logs?"
    # …and the third name the rant lists, singular or plural.
    r"|(?<!no )diar(?:y|ies)",
    re.IGNORECASE,
)


def test_retired_state_file_mechanism_is_gone_or_being_swept() -> None:
    """A prompt outside `PENDING_STATE_SWEEP` must not teach the retired mechanism.

    Measured 2026-09-14 (rant 2026-09-14T14:35:47): each task prompt carried a
    per-project `*_state.md` file and an append-only `*_reflections.md` diary, and
    every phase hand-off was a line written into one of them. The host retired both
    — the daemon already replays the task's session history into every round, so the
    session is the state and the memory index is the durable layer. Nothing reads
    either file any more, which makes every surviving instruction a write into a
    place no loader looks.

    The check runs in both directions, because a set that only ever shrinks is one
    someone can forget to shrink: a swept template must be clean, and a template
    still listed as pending must actually still mention the mechanism (otherwise it
    was swept without being removed from the set here).

    Named limit: this is a *text* guard over the built-in templates only. It cannot
    see a state file an agent invents at runtime, and it does not read the rendered
    prompt — the templates are checked as written.
    """
    swept = [name for _, name in _builtin_templates() if name not in PENDING_STATE_SWEEP]
    assert "open_source_prompt.md" in swept, (
        "the swept set lost its pilot template — the check is not looking where it "
        "thinks it is"
    )
    assert not PENDING_STATE_SWEEP, (
        f"PENDING_STATE_SWEEP is not empty ({sorted(PENDING_STATE_SWEEP)}) — every "
        f"template was swept, so a name here means one was added back without the "
        f"mechanism it is supposed to still teach"
    )

    for name in swept:
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        hits = sorted(set(_RETIRED_MECHANISM.findall(text)))
        assert not hits, (
            f"{name}: still teaches the retired state-file / reflection-file "
            f"mechanism {hits} — the session is the state now, so this instruction "
            f"sends the agent to a file nothing reads (rant 2026-09-14T14:35:47)"
        )

    for name in sorted(PENDING_STATE_SWEEP):
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        assert _RETIRED_MECHANISM.search(text), (
            f"{name} is still listed in PENDING_STATE_SWEEP but no longer mentions "
            f"the mechanism — drop it from the set in the same change that swept it, "
            f"so the set keeps meaning 'not yet done'"
        )


def test_retired_mechanism_fingerprint_covers_the_bare_noun_phrase() -> None:
    """What counts as a fingerprint: the sweep's vocabulary, not just its file names.

    Measured 2026-09-14 (cyc20260914-170405): `paper_prompt.md` came out of the
    sweep still instructing the agent to read a "state file" to derive its arXiv
    keywords. The fingerprint then matched only "the state file", so the guard
    called the template clean while an instruction pointing at a file nothing
    reads was still in it. A guard whose blind spot is a plausible spelling of the
    thing it forbids is a guard that reports success by not looking, which is
    worse than no guard.

    The four strings pin both halves of the pattern: the instruction forms that
    must be flagged, and the sweep's own denial sentences, which must not be —
    they are the text that replaced the mechanism.
    """
    assert _RETIRED_MECHANISM.search(
        "read Agent.md / abstract / state file to determine direction terms"
    ), "the bare 'state file' instruction is exactly what survived the first sweep"
    assert _RETIRED_MECHANISM.search("the state file holds the current phase")
    assert _RETIRED_MECHANISM.search("append this to the reflections file")
    assert not _RETIRED_MECHANISM.search(
        "This task keeps no state file — the session itself is the state."
    ), "the replacement text denies the file; flagging it would forbid saying what replaced it"
    assert not _RETIRED_MECHANISM.search("there is no reflections file any more")

    # The three spellings the underscore-prefixed pattern could not see. Each is
    # taken verbatim from a template that still uses it (measured 2026-09-15), so
    # these are not invented strings — `promote_prompt.md:38` is the first one.
    assert _RETIRED_MECHANISM.search(
        "- Reflection log: `{{ source_dir }}/reflections.md`"
    ), "the un-prefixed file name plus its other section name was the whole blind spot"
    assert _RETIRED_MECHANISM.search(
        "**Every cycle MUST end with a reflection appended to `reflections.md`**"
    )
    assert _RETIRED_MECHANISM.search("### 5. Reflection Log (mandatory every round)")
    assert _RETIRED_MECHANISM.search(
        "| Replies ignored or negative | record in the reflection log (pitfall) |"
    )
    assert _RETIRED_MECHANISM.search("append this round to the diary")
    assert _RETIRED_MECHANISM.search("read state.md to find the current phase")
    # …and the same three in their denial form stay legal, exactly like the two above.
    assert not _RETIRED_MECHANISM.search("there is no diary file and no state file")
    assert not _RETIRED_MECHANISM.search(
        "**Reflection is strategic-layer cognition, and the closing summary is where it goes**"
    )


def test_widened_fingerprint_is_measured_on_the_real_templates() -> None:
    """The widening catches more than the narrow one did — frozen, not live.

    A pattern change can satisfy a string assertion while catching nothing real,
    so the widening was measured against a live template and pinned. That template
    was `promote_prompt.md` (the last pending one) and it has now been swept, so
    the measurement is frozen here: the five lines the widening gained on it, taken
    verbatim, must still be caught by the widened pattern and still be **missed**
    by the prefix-free one. Measured in the tree that swept the journal template
    (base `97479c19`):

        promote_prompt.md   25 -> 30   (lines 38, 329, 331, 363, 376)
        journal_prompt.md   15 -> 15, now 0 -> 0 (swept)
        every swept template 0 ->  0

    Freezing it this way keeps the discriminating power the live version had: if
    someone narrows the pattern again, these five strings stop matching and this
    test goes red, which is exactly what the `== 5` assertion used to catch. What
    it deliberately no longer does is depend on a template that still teaches the
    mechanism — that would have tied the guard's own control to the residue it
    exists to remove. The other direction is kept live: no template may be flagged
    by the widened pattern, and the swept template's replacement text must not be.
    """
    prefix_free = re.compile(
        r"_state\.md|_reflections\.md|(?<!no )state[-\s]file|(?<!no )reflections?[-\s]file",
        re.IGNORECASE,
    )

    def seen(name: str, pattern: re.Pattern[str]) -> set[int]:
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        return {i for i, line in enumerate(text.splitlines(), 1) if pattern.search(line)}

    # The 5 lines of `promote_prompt.md` that the widening added (38, 329, 331,
    # 363, 376), verbatim. Each is a spelling of the retired mechanism that names
    # neither file with its project prefix — the blind spot the widening closed.
    gained_by_the_widening = (
        "- Reflection log: `{{ source_dir }}/reflections.md`",
        "### 5. Reflection Log (mandatory every round)",
        "**Every cycle MUST end with a reflection appended to `reflections.md`**",
        "record the verdict in the reflection log (question 8).",
        "| Replies ignored or negative | record in the reflection log (pitfall), don't "
        "force explanations, don't resend |",
    )
    for line in gained_by_the_widening:
        assert _RETIRED_MECHANISM.search(line), (
            f"the widened pattern no longer catches {line!r} — it was one of the 5 "
            f"lines the widening gained, so this is a narrowing, not a widening"
        )
        assert not prefix_free.search(line), (
            f"{line!r} is caught by the prefix-free pattern too, so it was never part "
            f"of the widening's gain — the frozen control does not measure what it claims"
        )

    # …and the widening must not start flagging the text that replaced it, which is
    # the live half: the swept template states what carries the state instead.
    for replacement in (
        "This task keeps no state file and no reflections file — the session itself is the state",
        "Every cycle MUST end with a closing summary in your final message.",
    ):
        assert not _RETIRED_MECHANISM.search(replacement), (
            f"the widened pattern flags the replacement text {replacement!r} — a "
            f"widening that forbids saying what replaced the mechanism is a different bug"
        )

    for _, name in _builtin_templates():
        hits = seen(name, _RETIRED_MECHANISM)
        assert not hits, (
            f"{name}: the widened pattern flags lines {sorted(hits)} — that is either a "
            f"real residue or a false positive in the replacement text"
        )


# `{{ evolution_cwd }}` is `~/.emrg/evolution/`, and the sandbox trusts only
# `~/.emrg/evolution/.emrg/` — `bash_tool._trusted_write_zones()` returns exactly
# that one root (measured 2026-09-19 on master `97479c19`). A template that names
# `{{ evolution_cwd }}/journal_..._state.md` therefore sends the agent to a write
# the tool layer refuses: `workspace-write sandbox: blocked write outside
# workspace`. That was the journal template's state and reflection files until
# the sweep (rant 2026-09-14T14:35:47, acceptance item 3: "提示词内不存在指向工作区
# 之外的写路径"). This is the guard for that item — the retired-mechanism pattern
# above cannot see it, because a *different* out-of-zone path is not the retired
# mechanism.
_EVOLUTION_CWD_REF = re.compile(r"\{\{ evolution_cwd \}\}(?P<rest>[^\s`)\]},;]*)")


def _out_of_zone_refs(text: str) -> list[str]:
    """Every `{{ evolution_cwd }}` reference that names something outside `.emrg/`.

    Two forms are legal, and only two: the bare root (used by the prohibition
    sentence in `evolution_prompt.md`, which names the directory rather than a
    path inside it) and `{{ evolution_cwd }}/.emrg/...` — the trusted subtree,
    where the memory entries live.
    """
    out: list[str] = []
    for match in _EVOLUTION_CWD_REF.finditer(text):
        rest = match.group("rest")
        if rest == "" or rest.startswith("/.emrg/"):
            continue
        out.append(match.group(0))
    return out


def test_no_prompt_names_a_path_outside_the_trusted_write_zone() -> None:
    """A template's write paths must stay inside the zone the sandbox trusts."""
    seen_refs = 0
    offenders: list[str] = []
    for _task_type, filename in _builtin_templates():
        text = (PROMPTS_DIR / filename).read_text(encoding="utf-8")
        seen_refs += len(_EVOLUTION_CWD_REF.findall(text))
        for ref in _out_of_zone_refs(text):
            offenders.append(f"{filename}: {ref}")

    # The scan must be looking somewhere: fewer references than the templates
    # actually carry would mean this test passes by matching nothing.
    assert seen_refs >= 8, (
        f"only {seen_refs} `{{{{ evolution_cwd }}}}` reference(s) found across the "
        f"built-in templates — the scan is not looking where it thinks it is"
    )
    assert not offenders, (
        "these templates name a write path outside the sandbox's trusted zone "
        f"(`~/.emrg/evolution/.emrg/`): {offenders} — the agent is sent to a write "
        "the tool layer blocks"
    )


def test_the_write_zone_scan_answers_both_ways() -> None:
    """The instrument's controls: what it flags, and what it must leave alone.

    Without the refusing half this test would pass on a regex that matches
    nothing; without the accepting half it would pass on one that refuses the
    legal forms the swept templates actually use. Both halves are taken from
    real template text.
    """
    assert _out_of_zone_refs(
        "- State file: `{{ evolution_cwd }}/journal_argszero_x_editor_state.md`"
    ), "the retired journal state-file path is exactly what this guard exists for"
    assert _out_of_zone_refs(
        "{{ evolution_cwd }}/open_source_{{ owner }}_{{ repo }}_reflections.md"
    ), "the retired reflection-file path must be flagged too"
    assert not _out_of_zone_refs(
        "memory entries under `{{ evolution_cwd }}/.emrg/memory/`"
    ), "the trusted subtree is where the replacement text sends the agent"
    assert not _out_of_zone_refs(
        "Do not modify files under `{{ evolution_cwd }}` outside `{{ source_dir }}/`"
    ), "the bare root names the directory in a prohibition; it is not a write path"


# `open_source_prompt.md` is the one built-in template whose `{{ source_dir }}` is the HOST's own
# working tree — the directory the dirty-tree rule (§0.3) and the sandbox exist to protect. It
# used to send Phase B.3 there to `git checkout -b`, which is unreachable in the `read-only` tier
# those same rules force (rant 2026-09-21T16:12: measured 12:22:01Z `git add` refused at 12:22:10Z
# in the sibling task, while its own hand-rolled clone pushed fine). The fix is a clone under the
# session directory; this guard keeps the flow from drifting back into the host tree.
#
# The other templates are deliberately out of scope: `evolution_prompt.md` works in its own
# repository by design ("not pushing = not done" — the branch, commit and push there ARE the
# cycle's output), and the journal/paper tasks commit manuscripts into their own task clone.
# A guard over all of them would be a different claim, not a stricter version of this one.
_GIT_MUTATOR = re.compile(
    r"\bgit\s+(clone|checkout|switch|add|commit|push|pull|rebase|merge|reset|clean|stash"
    r"|restore|cherry-pick|revert)\b"
)
_CD_TARGET = re.compile(r"\bcd\s+(?P<rest>[^&;|#]+)")


def _fenced_blocks(text: str) -> list[list[str]]:
    """The text's fenced code blocks, as lists of lines (fence lines dropped)."""
    blocks: list[list[str]] = []
    current: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            if in_fence:
                blocks.append(current)
                current = []
            in_fence = not in_fence
            continue
        if in_fence:
            current.append(line)
    return blocks


def _host_tree_git_writes(text: str) -> list[str]:
    """Git writes a template issues with the HOST tree as the current directory.

    The reader's directory is the last `cd <target>` in the block the command sits in —
    alone on its line or joined with `&&`, which is why the retired B.3 shape
    (`cd {{ source_dir }}` … `git checkout -b`) is caught. The target runs to the first
    `&`/`;`/`|`/`#` but *not* to a space, because `{{ source_dir }}` contains one; a trailing
    comment that merely *names* `{{ source_dir }}` (the new instruction says
    "never {{ source_dir }}") therefore stops at the `#` and is not read as the directory.
    Two exclusions, both measured against real template text: a read verb
    (`log`/`show`/`diff`/`status`/`fetch`) is not a mutation, and `--dry-run` writes nothing
    (the role probe in §0.2 is one).
    """
    offenders: list[str] = []
    for block in _fenced_blocks(text):
        cwd: str | None = None
        for line in block:
            match = _CD_TARGET.search(line)
            if match:
                cwd = match.group("rest").strip()
            if (
                cwd is not None
                and "source_dir" in cwd
                and _GIT_MUTATOR.search(line)
                and "--dry-run" not in line
            ):
                offenders.append(line.strip())
    return offenders


def test_the_open_source_flow_writes_only_in_the_session_clone() -> None:
    """`{{ source_dir }}` is the host's tree: no branch, commit or push inside it.

    Rant 2026-09-21T16:12. Three facts are asserted together because each can go missing on
    its own: the flow's git writes all run in the clone, the clone is *named* and sits under
    the session directory the sandbox allows, and the tier that flow needs is stated with it —
    a location without its tier sends the next reader to the same dead end one layer down.
    """
    text = (PROMPTS_DIR / "open_source_prompt.md").read_text(encoding="utf-8")

    offenders = _host_tree_git_writes(text)
    assert not offenders, (
        "the open-source template issues these git writes in the HOST's tree "
        f"(`{{{{ source_dir }}}}`): {offenders} — Phase B.3's clone exists so that "
        "`{{ source_dir }}` stays a read-only reference"
    )

    # The scan must be looking at a non-empty surface: the flow's writes are still there,
    # in the clone. Otherwise deleting the whole phase would read as a pass.
    clone_writes = [
        line
        for block in _fenced_blocks(text)
        for line in block
        if _GIT_MUTATOR.search(line) and "--dry-run" not in line
    ]
    assert len(clone_writes) >= 3, (
        f"only {len(clone_writes)} mutating git command(s) left in the contribution flow — "
        "the scan above would pass on a template that no longer contributes"
    )

    assert re.search(
        r'DEV="\{\{ source_dir \}\}/\.emrg/sessions/\{\{ session_id \}\}/tmp/', text
    ), "B.3 must name the clone under the session directory, not merely say 'a clone'"

    # The tier half is bound to B.3 rather than asserted anywhere in the file: a location
    # without the tier that unlocks it sends the next reader to the same dead end one layer
    # down, which is what the rant measured (both the configured and the dirty-tree-forced
    # `read-only` refuse every step of this flow).
    parts = text.split('DEV="{{ source_dir }}', 1)
    assert len(parts) == 2, "B.3 must define the clone as a shell variable the flow can reuse"
    section = parts[1].split("#### B.4", 1)[0]
    assert "workspace-write" in section and "read-only" in section, (
        "B.3 must state the tier this flow needs and the tier that refuses it — measured: "
        "`git clone`, `git checkout -b`, `git add` and `git commit` are all BLOCK under "
        "`read-only` and all ALLOW under `workspace-write`"
    )


def test_the_host_tree_write_scan_answers_both_ways() -> None:
    """The instrument's controls, on the shapes this repository has actually carried.

    Without the refusing half it would pass on a scan that finds nothing; without the
    accepting half it would flag the read-only probes that legitimately run in the host tree —
    including the new instruction's own comment, which names `{{ source_dir }}` in order to
    forbid it.
    """
    retired = """```bash
cd {{ source_dir }}
gh repo fork {{ owner }}/{{ repo }} --clone=false 2>&1
git checkout -b <branch name per project convention> 2>&1
```"""
    assert _host_tree_git_writes(retired) == [
        "git checkout -b <branch name per project convention> 2>&1"
    ], "the retired B.3 shape is exactly what this guard exists for"

    shipped = """```bash
DEV="{{ source_dir }}/.emrg/sessions/{{ session_id }}/tmp/{{ repo }}-dev"
cd "$DEV"    # the clone from B.3 — never {{ source_dir }}
git add -A
git push origin <branch> 2>&1
```"""
    assert not _host_tree_git_writes(shipped), (
        "the clone's own instructions name `{{ source_dir }}` in a comment; that is not a cwd"
    )

    assert not _host_tree_git_writes(
        "```bash\ncd {{ source_dir }} && git status --short --branch 2>&1\n```"
    ), "reading the host tree is what it is a reference for"
    assert not _host_tree_git_writes(
        "```bash\ncd {{ source_dir }} && git push origin HEAD --dry-run 2>&1\n```"
    ), "a dry run writes nothing, and §0.2's role probe is one"
