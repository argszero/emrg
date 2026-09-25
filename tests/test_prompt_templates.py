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

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import jinja2
import yaml

from emrg.protocol import InstanceIdentity
from emrg.sandbox.policy import SandboxPolicy
from emrg.sandbox.roots import canonical_path, writable_roots
from emrg.server import scheduler as mod
from emrg.server.daemon import EmrgServer
from emrg.server.scheduler import TaskHandler

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


# The memory roots a template may name, in the spelling a template writes them: the
# session's *project* memory (`<session.cwd>/.emrg/memory/`) and its *session* memory
# (`<session.cwd>/.emrg/sessions/<id>/memory/`). After D9 (design §6 D9, host decision
# 2026-09-21 16:25) every memory path in every template is written against one root —
# `{{ source_dir }}`, the session workspace — because it is both the directory the sandbox
# confines writes to and the directory whose `.emrg/memory/MEMORY.md` the daemon embeds.
# Spaces are stripped from a ref's suffix before comparison — the session root is spelled
# `{{ source_dir }}/.emrg/sessions/{{ session_id }}/memory/`, so the two spellings are the
# same path only once the placeholder's interior spaces are gone.
LEGAL_MEMORY_ROOTS = ("/.emrg/memory", "/.emrg/sessions/{{session_id}}/memory")

# `{{ <name> }}` plus the path it is followed by, up to a `memory/` segment. Deliberately
# *any* placeholder-rooted path rather than `{{ source_dir }}` one: a scan for the root it
# wants cannot see the root it forbids, so a path re-based onto another root yields an
# empty list — and "nothing found" then reads exactly like "nothing wrong". Multi-part
# because the session root goes through two placeholders, so a single-placeholder pattern
# would be blind to `promote_prompt.md`, the template that names *both* roots (#1557).
PLACEHOLDER_MEMORY_REF = re.compile(
    r"\{\{\s*(?P<var>[\w.]+)\s*\}\}(?P<rest>(?:/[\w.{}\s-]+?)*/memory(?![-\w]))"
)


def _placeholder_memory_refs(text: str) -> list[tuple[str, str]]:
    """Every placeholder-rooted memory path, as ``(root variable, path suffix)``."""
    return [
        (match.group("var"), match.group("rest").replace(" ", ""))
        for match in PLACEHOLDER_MEMORY_REF.finditer(text)
    ]


def _is_legal_memory_root(var: str, suffix: str) -> bool:
    """Is this memory path rooted at the workspace root the daemon loads?

    The single reading of that rule. Two guards apply it — the memory-root scan (a write
    target must be one of `LEGAL_MEMORY_ROOTS`) and the embed-claim scan (a root may only
    be *called* embedded if it is) — and a second copy of it would be free to drift.
    """
    return var == "source_dir" and any(
        suffix == root or suffix.startswith(root + "/") for root in LEGAL_MEMORY_ROOTS
    )


def _sandbox_defines(name_fragment: str) -> list[str]:
    """Functions under ``emrg/sandbox/`` whose name contains this text.

    Read with ``ast`` rather than by scanning the source text: the deleted mechanism is
    still *named* in a docstring (`emrg/sandbox/roots.py` explains what it replaced), and
    a text scan would report that explanation as the mechanism itself.
    """
    found: list[str] = []
    for path in sorted((REPO_ROOT / "emrg" / "sandbox").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if name_fragment in node.name:
                    found.append(f"{path.name}:{node.name}")
    return found


def test_prompt_memory_writes_land_where_the_daemon_loads_them(tmp_path, monkeypatch) -> None:
    """A prompt's memory path must be the directory whose index the daemon embeds.

    Rewritten for D9 (design §6 D9, host decision 2026-09-21 16:25). What it replaced,
    and why the replacement measures instead of trusting a copy of the rule:

    The templates used to send the agent's identity file and its cycle records to
    `{{ evolution_cwd }}/.emrg/memory/` — `~/.emrg/evolution/.emrg/memory/`. The daemon
    loads `<session.cwd>/.emrg/memory/`, a *different* directory, so the prompt told the
    agent to write somewhere whose index it never read. Two things kept that invisible:
    no session embeds the old root, and the old command-line sandbox carried a trusted
    extra root for it, so the write went through anyway. D5 deleted that extra root
    (P1), which turns the path into a fail-closed refusal as soon as v2 is the boundary.

    The expected root is therefore not copied here. It is **measured**, by handing the
    daemon's own loader a session whose `cwd` is the `{{ source_dir }}` the template
    renders: if the write root and the loaded root ever split apart again, this fails.

    Named limit: this measures the *placeholder*, not the prose. A memory path a template
    assembles at runtime, or writes into a shell heredoc, is outside what a render can
    show; and a path that is wrong in a way the render still resolves (a legal root in the
    wrong session) is not a shape this test can tell apart.
    """
    handler = _make_handler(
        tmp_path, monkeypatch, "evolution_prompt.md", {"project": "demoproj"}
    )
    source_dir = Path(handler._source_dir)
    source_dir_str = str(handler._source_dir)
    memory_root = source_dir / ".emrg" / "memory"

    # (1) "Every memory path is rooted at the workspace the daemon loads" used to be
    # asserted here too, by a scan that looked for the `{{ source_dir }}` member of the
    # category — the wrong shape twice over: a scan for the root it wants cannot see the
    # root it forbids, and this copy of the rule could drift from
    # `test_no_template_names_a_memory_root_other_than_the_two`, which reads the whole
    # category (and absolute paths) through the same `_is_legal_memory_root`. Removed
    # 2026-09-24 (#1557): one rule, one home. What this test is *for* is the other half —
    # that the root the templates write to is the root the daemon actually loads — which
    # the static scan cannot answer and (2)–(4) below measure.

    # (2) The placeholder resolves to that workspace, through the real builder.
    rendered = handler._build_evolution_prompt()
    assert f"{source_dir_str}/.emrg/memory" in rendered, (
        f"the rendered prompt does not name {source_dir_str}/.emrg/memory — "
        f"`{{{{ source_dir }}}}` is not resolving to the session workspace"
    )

    # (3) That workspace's `.emrg/memory` is what the daemon embeds as project memory.
    memory_root.mkdir(parents=True, exist_ok=True)
    (memory_root / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
    # `_collect_memory_data` touches only `session.cwd` and `session.memory_dir`, and
    # reaches the module-level `INDEX_SIZE_WARN` through a method — so an uninitialised
    # instance is enough, and no daemon, socket or event loop is started to measure this.
    daemon = EmrgServer.__new__(EmrgServer)
    data = daemon._collect_memory_data(
        SimpleNamespace(cwd=source_dir, memory_dir=tmp_path / "session-memory")
    )
    assert data, (
        "the daemon embedded no project index for a session whose cwd carries one — "
        "the comparison below would be against nothing"
    )
    assert data["project_memory_dir"] == str(memory_root), (
        f"the templates tell the agent to write under {memory_root}, but the daemon "
        f"embeds {data['project_memory_dir']} as project memory — the write root and "
        f"the loaded root have split apart again"
    )

    # (4) The extra-root mechanism the old root lived in is gone from the boundary that
    # survives — D5 deleted it from v2 (P1). The frozen old tool keeps its own copy until
    # P7 deletes that file, which is why this is asserted against `emrg/sandbox/`: that
    # is the boundary every session is on, since P6 made it the default.
    assert not _sandbox_defines("trusted_write_zone"), (
        "an extra trusted write root is defined again under `emrg/sandbox/` — the "
        "mechanism D5 removed is back, and with it the reason this file exists"
    )


# The claim guard's reader of the rule is `_is_legal_memory_root` (defined with the
# memory-root scan above): the same legal roots, so "where memory may be written" and
# "which index may be called loaded" cannot drift apart. What is specific to this guard is
# only the *claim* pattern below.
#
# Every spelling the templates use, both directions of the sentence: "the memory index is
# part of this prompt", "the memory index embedded in this prompt", and "memory entries
# under …, whose index this prompt embeds". The first version of this pattern knew only the
# passive forms, which made it blind to four live paragraphs — measured 2026-09-23 by a
# mutation arm: a claim re-based onto the old root ("…whose index this prompt embeds; write
# them under `{{ evolution_cwd }}/.emrg/memory/`") went unflagged by this guard, and only
# the memory-root scan caught it. A detector blind to the majority spelling of the thing it
# looks for reports success by not looking.
_INDEX_IN_PROMPT = re.compile(
    r"(?:part of|embedded in)\s+this prompt\b|this prompt\s+embeds?\b", re.IGNORECASE
)


def _embedded_index_claims(text: str) -> list[str]:
    """Paragraphs that claim an embedded index while naming a root that is not one.

    Only paragraphs carrying the claim are examined: naming a memory root without the
    claim is what most of every template does, and is not this guard's business.
    """
    offenders: list[str] = []
    for paragraph in text.split("\n\n"):
        if not _INDEX_IN_PROMPT.search(paragraph):
            continue
        for match in PLACEHOLDER_MEMORY_REF.finditer(paragraph):
            suffix = match.group("rest").replace(" ", "")
            if not _is_legal_memory_root(match.group("var"), suffix):
                offenders.append(paragraph.strip()[:140])
                break
    return offenders


def _embedded_index_claim_counts(text: str) -> tuple[int, int]:
    """Paragraphs that claim an embedded index, and how many also name a memory root.

    The second number is what keeps this guard from passing on a *different* file: if the
    claim and the path stopped co-occurring, every paragraph would be trivially legal and
    the check would answer only about paragraphs that name nothing.
    """
    claims = paired = 0
    for paragraph in text.split("\n\n"):
        if not _INDEX_IN_PROMPT.search(paragraph):
            continue
        claims += 1
        if PLACEHOLDER_MEMORY_REF.search(paragraph):
            paired += 1
    return claims, paired


def test_a_prompt_may_only_call_an_embedded_index_its_own() -> None:
    """A root must not be described as *loaded* unless it is the loaded one.

    Measured 2026-09-14 (cyc20260914-175549), before the fix: three of the paragraphs
    re-based onto memory entries under `{{ evolution_cwd }}/.emrg/memory/` also called
    that directory's index "part of this prompt". It never was. `_collect_memory_data`
    embeds `session.cwd/.emrg/memory/MEMORY.md` — the *task project's* index — and the
    session index, and never `{{ evolution_cwd }}/.emrg/memory/MEMORY.md`; measured by
    pointing `EVOLUTION_CWD` at a directory whose memory index carries a marker and
    rendering the system prompt through the real builder: the marker stayed out while
    the project's appeared.

    The pairing was what was false, not the path: naming that root is fine, and claiming
    "the memory index is embedded in this prompt" without naming a path is fine. Naming a
    root *and* claiming its index is in the prompt points the agent at a place whose
    contents it will not find. D9 removed the false pairing at its root — the two legal
    roots are now the two embedded ones — so this guard keeps the pairing true rather
    than repairing it.

    Named limit: the claim is matched only in the paragraph that carries the path. A
    template that names a root in one paragraph and claims embedding three paragraphs
    later is not caught; that shape is not in the templates, and detecting it needs a
    notion of "about" this file does not have.
    """
    # The control: the exact shape the guard was written for. It must be flagged, and
    # `{{ evolution_cwd }}/.emrg/memory/` is still a root a template could name.
    planted_bad = (
        "Record findings under `{{ evolution_cwd }}/.emrg/memory/`, "
        "whose index is part of this prompt."
    )
    assert _embedded_index_claims(planted_bad), (
        "the detector no longer detects the shape it was written for"
    )
    # …and the two forms that are true stay legal, or the guard forbids saying what the
    # daemon actually does.
    assert not _embedded_index_claims(
        "Record findings under `{{ source_dir }}/.emrg/memory/`, whose index is part of "
        "this prompt."
    ), "the project-memory root IS the embedded one; flagging it is a false positive"
    assert not _embedded_index_claims(
        "Durable facts belong in **memory entries under `{{ source_dir }}/.emrg/sessions/"
        "{{ session_id }}/memory/`**, whose index this prompt embeds."
    ), "the session-memory root is embedded too (promote_prompt.md writes there)"
    assert not _embedded_index_claims(
        "The memory index is embedded in this prompt, under the session's own `.emrg/`."
    ), "a claim that names no root is not this guard's business"

    claims = paired = 0
    suspects: list[str] = []
    for _task_type, filename in _builtin_templates():
        text = (PROMPTS_DIR / filename).read_text(encoding="utf-8")
        found, with_root = _embedded_index_claim_counts(text)
        claims += found
        paired += with_root
        suspects += [f"{filename}: {p}" for p in _embedded_index_claims(text)]

    # Floors, measured when D9 landed: 10 paragraphs claim an embedded index and 5 of
    # them pair the claim with a memory root. Both may grow; shrinking means the guard
    # stopped looking (and the second, that its live half went vacuous).
    assert claims >= 8, (
        f"only {claims} paragraph(s) in the templates claim an embedded index — the "
        f"scan is not looking where it thinks it is, so 'no offenders' says nothing"
    )
    assert paired >= 3, (
        f"only {paired} of them also name a memory root — the pairing this guard exists "
        f"to check is no longer exercised by any real paragraph"
    )
    assert not suspects, (
        "these paragraphs claim an index is in the prompt while naming a root the "
        "daemon does not embed:\n  " + "\n  ".join(suspects)
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


# The boundary every session is on since P6 made v2 the default is
# `emrg/sandbox/roots.writable_roots`: the policy's workspace root plus the temp areas,
# and **nothing else**, because D5 deleted EMRG's extra deployer root
# (`bash_tool._trusted_write_zones()`, `~/.emrg/evolution/.emrg/`) rather than carrying it
# over under another name. `{{ evolution_cwd }}` is `~/.emrg/evolution/` — a sibling of
# the session's workspace — so every path under it is refused with `workspace-write
# sandbox: blocked write outside workspace`. That is what made D9 a prerequisite of P7
# rather than a tidy-up: the memory root the templates named was outside the boundary that
# survives. The old wording of this block cited the trusted-zone list as the reason the
# path was legal; the list is gone, so the claim moved with the zone.
#
# Acceptance item 3 of rant 2026-09-14T14:35:47 ("提示词内不存在指向工作区之外的写路径") is
# still the claim, and this is still its guard — the retired-mechanism pattern above
# cannot see it, because a *different* out-of-zone path is not the retired mechanism.
# The category scan below reads `PLACEHOLDER_MEMORY_REF` and `_is_legal_memory_root`,
# defined with the memory-root scan near the top of this file: one pattern and one reading
# of the rule, so the write-root rule and the claim rule cannot drift apart. What is
# specific to this scan is the second half — a memory directory spelled as an absolute (or
# `~`-rooted) path, below.
#
# `_ABSOLUTE_MEMORY_DIR`: whatever a template hardcodes is outside the placeholder
# mechanism, so it cannot be re-based by a change to the render context, and it is right
# only on the machine it was written on.
_ABSOLUTE_MEMORY_DIR = re.compile(r"(?:^|[\s`'\"(])(?P<path>[/~][\w./~-]*/memory(?![-\w]))")


def _inside(path: Path, root: str) -> bool:
    """Is ``path`` at or under ``root``, in the identity the sandbox itself compares?

    Both sides go through ``canonical_path`` because the Seatbelt provider matches
    *resolved* paths, and a root compared as spelled matches nothing.
    """
    resolved = Path(canonical_path(str(path)))
    canonical_root = Path(canonical_path(root))
    return resolved == canonical_root or canonical_root in resolved.parents


def test_every_memory_root_a_template_names_is_writable() -> None:
    """The templates' memory roots must lie inside the boundary the sandbox derives.

    Two claims, and the second is the control that makes the first mean something:

    1. the root the templates name is inside a writable root, so the writes the prompt
       prescribes are not the ones the tool layer refuses;
    2. the root D9 moved *off* is outside it — the measured reason the migration was a
       prerequisite rather than a preference. Without (2) this would pass even if the
       boundary had grown to cover every path, in which case the migration was pointless
       and the guard's premise is unmeasured.

    Neither side is a copy of the boundary: both call ``writable_roots`` with a policy
    built here, so a boundary change moves the answer.
    """
    policy = SandboxPolicy(mode="workspace-write", workspace_root=str(REPO_ROOT))
    roots = writable_roots(policy)
    assert roots, "the policy derived no writable root — this check would pass vacuously"

    project_root = REPO_ROOT / ".emrg" / "memory"
    assert any(_inside(project_root, root) for root in roots), (
        f"{project_root} is outside every writable root the sandbox derives "
        f"({roots}) — the memory root the templates name is one the tool layer refuses"
    )

    old_root = Path(mod.EVOLUTION_CWD) / ".emrg" / "memory"
    if _inside(old_root, str(REPO_ROOT)):
        raise AssertionError(
            f"this checkout is placed such that {old_root} IS inside the workspace "
            f"{REPO_ROOT} — the two roots are indistinguishable here, so this test "
            f"cannot answer the question it asks (it reports that, rather than passing)"
        )
    assert not any(_inside(old_root, root) for root in roots), (
        f"{old_root} is inside a writable root ({roots}) on this machine, so the D9 "
        f"premise ('the old root is refused under v2') does not hold here — re-measure "
        f"before believing the guards above"
    )


def _memory_root_offenders(text: str) -> tuple[int, list[str]]:
    """Memory references in ``text``, and those of them rooted at the wrong place."""
    checked = 0
    offenders: list[str] = []
    for var, suffix in _placeholder_memory_refs(text):
        checked += 1
        if not _is_legal_memory_root(var, suffix):
            offenders.append(f"{{{{ {var} }}}}{suffix}")
    for match in _ABSOLUTE_MEMORY_DIR.finditer(text):
        offenders.append(match.group("path"))
    return checked, offenders


def test_no_template_names_a_memory_root_other_than_the_two() -> None:
    """Every memory path in every template is rooted at the session workspace.

    After D9 exactly two roots are legal, and both are the session workspace's own
    (`LEGAL_MEMORY_ROOTS` — the project and session memory the daemon embeds, which are
    the same two directories the sandbox allows because ``{{ source_dir }}`` IS
    ``session.cwd``). Everything else is a defect of one of two kinds: rooted at another
    variable (the D9 shape), or hardcoded as an absolute path (which no render-context
    change can re-base, and which is correct only on the machine that wrote it).

    Two halves, and the second was added because the first alone has slack (#1557): the
    scan refuses a mis-rooted path, and the per-file floor refuses a *silent* one. Measured
    2026-09-24 while reviewing that issue — its headline ("a one-line revert of
    `paper_prompt.md` passes every arm") is true of the two arms *inside*
    `test_prompt_memory_writes_land_where_the_daemon_loads_them`, and false of this file:
    reverting that line turns **this test** red, and this test alone is the failure set
    (an earlier reading that showed a second failure had `HOME` pinned inside the
    repository, which makes `test_every_memory_root_a_template_names_is_writable` red on
    its own — a false red, measured and discarded).

    What was genuinely missing was the floor's *shape*. A floor summed over the set is
    masked by any gain elsewhere: at 13 references against a floor of 13 a deletion is
    still caught, but the first time one template gains a reference, another's loss is
    offset and the aggregate reads healthy while a whole file's memory paths are gone — and
    in neither case can it name the file that is short, which is the edit a reader needs.
    """
    offenders: list[str] = []
    per_file: dict[str, int] = {}
    for _task_type, filename in _builtin_templates():
        text = (PROMPTS_DIR / filename).read_text(encoding="utf-8")
        found, bad = _memory_root_offenders(text)
        per_file[filename] = found
        offenders += [f"{filename}: {ref}" for ref in bad]

    # A floor **per file**, not over the set (#1557): a floor summed over the six templates
    # is masked by any gain elsewhere, and cannot name the file that is short. These are
    # minimums, measured when the floor was made per-file (1, 3, 2, 3, 1, 3 — 13 across the
    # six templates: 10 project-memory + 3 session-memory); a removal is allowed, but it has
    # to be deliberate and it has to be this line that is edited. A template absent from the
    # map is still covered by the scan; a *renamed* one lands here as 0 and cannot shed its
    # floor silently.
    floor = {
        "competition_prompt.md": 1,
        "evolution_prompt.md": 3,
        "journal_prompt.md": 2,
        "open_source_prompt.md": 3,
        "paper_prompt.md": 1,
        "promote_prompt.md": 3,
    }
    thin = [
        f"{name}: {per_file.get(name, 0)} < {needed}"
        for name, needed in floor.items()
        if per_file.get(name, 0) < needed
    ]
    assert not thin, (
        "these templates name fewer placeholder-rooted memory paths than they did when "
        "this floor was made per-file: " + ", ".join(thin)
    )
    assert not offenders, (
        f"these templates name a memory root that is neither the session workspace's "
        f"project memory nor its session memory: {offenders}"
    )


def test_the_memory_root_scan_answers_both_ways() -> None:
    """The instrument's controls: what it flags, and what it must leave alone.

    Without the refusing half the scan would pass on a regex that matches nothing;
    without the accepting half it would pass on one that refuses the legal forms the
    templates actually use. Both halves are taken from real template text.
    """
    # Refuses: the root D9 re-based off, the same root reached through another variable,
    # and a hardcoded absolute path.
    assert _memory_root_offenders(
        "memory entries under `{{ evolution_cwd }}/.emrg/memory/`"
    )[1], "the D9 shape is exactly what the scan exists for"
    assert _memory_root_offenders(
        "write `{{ session_dir }}/memory/cycle-1.md`"
    )[1], "a memory root behind any other variable is the same defect"
    assert _memory_root_offenders(
        "append to /Users/someone/.emrg/evolution/.emrg/memory/MEMORY.md"
    )[1], "an absolute memory path cannot be re-based by the render context"
    # Accepts: the two roots a template is allowed to name, in the spellings the
    # templates actually use (both halves of the session root's two placeholders included).
    assert not _memory_root_offenders(
        "memory entries under `{{ source_dir }}/.emrg/memory/`"
    )[1], "the project-memory root is where D9 sends every hand-off"
    assert not _memory_root_offenders(
        "`{{ source_dir }}/.emrg/sessions/{{ session_id }}/memory/`"
    )[1], "the session-memory root is the second legal one (promote_prompt.md)"
    assert not _memory_root_offenders(
        "Write `{{ source_dir }}/.emrg/memory/cycle-{{ timestamp }}.md`"
    )[1], "a *file* under the legal root is not a third root"


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
    # down, which is what the rant measured (both the configured and the failed-convergence
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


# A default branch named literally. `main` and `master` are two spellings of one thing and a
# repository's own default is whichever it happens to use — this one is `master`, so the
# `origin/main` the first version of §0.3 told the reader to run fails with
# `fatal: Needed a single revision` (measured 2026-09-21, PR #1524 review).
_LITERAL_DEFAULT_BRANCH = re.compile(r"\b(?:origin|upstream)/(?:main|master)\b")


def test_the_default_branch_is_resolved_rather_than_spelled() -> None:
    """The template resolves the default branch and starts the branch at it.

    Two claims, both from the external review of PR #1524 (measured, not argued):

    1. A literal `<remote>/main` does not resolve in a repository whose default is `master`, and
       enumerating spellings is the #461 class this repo keeps refusing to open — so the template
       must not name one, and must keep the mechanism that resolves it (`gh repo view --json
       defaultBranchRef`, already used for the PR base).
    2. B.3's `git checkout -b` had no start point, so it branched off the clone's own HEAD — and a
       fork is only as fresh as its last sync. The reviewer's fork stood **396 commits** behind
       upstream, so B.5's suite would have measured a tree twelve days old while the PR's diff
       stays clean (the merge base is still an ancestor). The start point is the fix.
    """
    text = (PROMPTS_DIR / "open_source_prompt.md").read_text(encoding="utf-8")

    literals = _LITERAL_DEFAULT_BRANCH.findall(text)
    assert not literals, (
        f"the template names a default branch literally: {literals} — resolve it instead "
        "(`gh repo view {{ owner }}/{{ repo }} --json defaultBranchRef`); `main` and `master` "
        "are two spellings of one class and this repository's default is the second one"
    )
    assert "defaultBranchRef" in text, (
        "the template must keep the resolution mechanism it replaced the literal with"
    )

    clone_block = text.split('DEV="{{ source_dir }}', 1)[1].split("#### B.4", 1)[0]
    checkout = [
        line
        for block in _fenced_blocks("```bash\n" + clone_block)
        for line in block
        if "git checkout -b" in line
    ]
    assert checkout, "B.3 must still create the branch"
    for line in checkout:
        assert "upstream/$DEFAULT" in line or "$DEFAULT" in line, (
            f"`{line.strip()}` branches off the clone's own HEAD — a fork can be hundreds of "
            "commits behind, so the branch (and B.5's suite) must start at the upstream default"
        )


def test_the_default_branch_scan_answers_both_ways() -> None:
    """The scanner's controls, on the two spellings and the resolved form."""
    assert _LITERAL_DEFAULT_BRANCH.findall("git diff HEAD origin/main\n"), (
        "the spelling the first version carried must be flagged — in this repository it does "
        "not resolve"
    )
    assert _LITERAL_DEFAULT_BRANCH.findall("git show upstream/master:<path>\n"), (
        "the other spelling of the same class must be flagged too"
    )
    assert not _LITERAL_DEFAULT_BRANCH.findall('git diff HEAD "origin/$DEFAULT"\n'), (
        "the resolved form is what the rule asks for and must not be flagged"
    )


# The clone's blocks are copied one at a time — they are separate fenced snippets and the reader
# reaches B.6 hours after B.3. A block that says `cd "$DEV"` without defining it depends on a
# variable from another block, and an unset one is not an error: measured on this host
# (2026-09-21) in bash, sh, dash and zsh, `cd ""` leaves the shell where it was and returns 0.
# B.5 would then run the suite in the reader's own directory, which for this task is the host
# tree the whole phase exists to keep out of the way — silently, which is the worse direction.
_CLONE_CD = re.compile(r'\bcd\s+"\$DEV"|\bcd\s+\$DEV\b')
_CLONE_DEFINE = re.compile(r"^\s*DEV=")


def _clone_blocks_without_a_definition(text: str) -> list[str]:
    """Blocks that enter the clone but never name it, one entry per offending `cd` line."""
    offenders: list[str] = []
    for block in _fenced_blocks(text):
        enters = [line for line in block if _CLONE_CD.search(line)]
        if enters and not any(_CLONE_DEFINE.search(line) for line in block):
            offenders.extend(line.strip() for line in enters)
    return offenders


def test_every_clone_block_defines_the_directory_it_enters() -> None:
    """Every block that `cd "$DEV"` defines `DEV` in that same block.

    Rant 2026-09-21T16:12:19, second round — the first version of this flow defined `DEV` in B.3
    alone, so the three later blocks were only correct when read together. The measurement that
    makes this worth a guard rather than a note: `cd ""` is not an error in any shell tried, so
    the failure is a suite run in the wrong tree, reported as a pass.
    """
    text = (PROMPTS_DIR / "open_source_prompt.md").read_text(encoding="utf-8")

    offenders = _clone_blocks_without_a_definition(text)
    assert not offenders, (
        f"these commands enter the clone without defining it in their own block: {offenders} — "
        "a copied block must not depend on a variable set in another block"
    )

    # Non-empty surface: the flow still enters the clone, so the scan is not passing by default.
    assert sum(
        len([line for line in block if _CLONE_CD.search(line)])
        for block in _fenced_blocks(text)
    ) >= 3, "the flow no longer enters the clone anywhere — the scan above would pass vacuously"


def test_the_clone_definition_scan_answers_both_ways() -> None:
    """The instrument's controls: the dependent shape is flagged, the self-contained one is not."""
    dependent = '```bash\ncd "$DEV"\ngit push origin <branch> 2>&1\n```'
    assert _clone_blocks_without_a_definition(dependent) == ['cd "$DEV"'], (
        "a block that enters a variable it never sets is exactly what this guard is for"
    )

    self_contained = (
        '```bash\nDEV="{{ source_dir }}/.emrg/sessions/{{ session_id }}/tmp/{{ repo }}-dev"\n'
        'cd "$DEV"\ngit push origin <branch> 2>&1\n```'
    )
    assert not _clone_blocks_without_a_definition(self_contained), (
        "the shipped shape names the clone in its own block and must not be flagged"
    )


# B.3 leaves the branch tracking `upstream` and keeps that remote beside the fork, which is exactly the
# layout in which `gh pr create` cannot infer the head: its `@{push}` lookup errors (`push.default` is
# unset and the local and upstream branch names differ) and its ref-probe fallback stops at the first
# missing ref. Measured 2026-09-21 on a clone of a real fork: `--dry-run` printed `head: master` — the
# base repository's own branch — where the reviewer's clone aborted outright, and gh's abort message
# asks for the flag by name. `--head` skips the inference, so the create call must carry it.
_CREATE_HEAD = re.compile(r"--head\s")
# The head must be spelled by resolving the login: this template serves every contributor, so a
# literal name is wrong even when it happens to be right for the cycle that wrote it.
_CREATE_RESOLVED_HEAD = re.compile(r"--head\s+\"\$\(gh api user -q \.login\):")


def _create_calls(text: str) -> list[str]:
    """Every `gh pr create` command in a fenced block, its continuations joined.

    Only fenced blocks are read: the template also *talks* about `gh pr create` in prose and in
    capability tables, and a rule about the command must not be satisfiable by the sentence.
    """
    calls: list[str] = []
    for block in _fenced_blocks(text):
        for i, line in enumerate(block):
            if "gh pr create" not in line:
                continue
            call = [line]
            j = i
            while block[j].rstrip().endswith("\\") and j + 1 < len(block):
                j += 1
                call.append(block[j])
            calls.append(" ".join(part.strip() for part in call))
    return calls


def _create_calls_without_a_head(text: str) -> list[str]:
    """Create calls that do not name the head at all."""
    return [call for call in _create_calls(text) if not _CREATE_HEAD.search(call)]


def _create_calls_naming_a_head_literally(text: str) -> list[str]:
    """Create calls that name the head without resolving whose fork it is."""
    return [
        call
        for call in _create_calls(text)
        if _CREATE_HEAD.search(call) and not _CREATE_RESOLVED_HEAD.search(call)
    ]


def test_every_create_call_names_the_head() -> None:
    """`gh pr create` is always told which repository the head lives in.

    Rant 2026-09-21T16:12:19, fourth round (PR #1524 review). B.3 makes the new branch track `upstream`
    and keeps `upstream` beside the fork, so gh's head inference lands on the base repository — and what
    it does next depends on which refs happen to exist locally: measured, a wrong head (`master`) in one
    clone where another aborted. Naming the head removes the guess in both directions, and the silent
    variant is the one a guard has to catch because a TTY merely prompts.

    The second clause is why this reads the *call* rather than the file: the first version asserted that
    `gh api user -q .login` appeared somewhere in the template, and a mutation arm replacing the head
    with a literal login stayed green on the strength of the sentence explaining the rule. A guard whose
    power rests on prose is the defect class it was written against.
    """
    text = (PROMPTS_DIR / "open_source_prompt.md").read_text(encoding="utf-8")

    missing = _create_calls_without_a_head(text)
    assert not missing, (
        f"these `gh pr create` calls do not name their head: {missing} — without `--head "
        '"$(gh api user -q .login):<branch>"` gh infers it, and B.3\'s `upstream` remote makes that '
        "inference land on the base repository rather than the fork"
    )

    literal = _create_calls_naming_a_head_literally(text)
    assert not literal, (
        f"these create calls name a head without resolving the login: {literal} — the contributor is "
        "whoever runs this, so the login comes from `gh api user -q .login`, never a literal"
    )

    # Non-empty surface: the scans are not passing because no create call exists to check.
    assert _create_calls(text), (
        "the template no longer opens a PR anywhere — the checks above would pass vacuously"
    )


def test_the_create_head_scan_answers_both_ways() -> None:
    """The instrument's controls, on the shipped shape and on the flagged ones."""
    shipped = (
        '```bash\ncd "$DEV" && gh pr create -R o/r \\\n'
        '  --head "$(gh api user -q .login):<branch name>" \\\n'
        '  --title "x"\n```'
    )
    assert not _create_calls_without_a_head(shipped), "the shipped shape names the head"
    assert not _create_calls_naming_a_head_literally(shipped), "and resolves the login"

    assert _create_calls_without_a_head(
        '```bash\ncd "$DEV" && gh pr create -R o/r \\\n  --title "x" \\\n  --body "y"\n```'
    ), "a create call with no `--head` is exactly what the first scan is for"

    assert _create_calls_without_a_head(
        '```bash\ngh pr create -R {{ owner }}/{{ repo }} \\\n  --title "<scope>: <description>"\n```'
    ) == ['gh pr create -R {{ owner }}/{{ repo }} \\ --title "<scope>: <description>"'], (
        "a continuation line carries no head, so the call must be read as a whole"
    )

    assert _create_calls_naming_a_head_literally(
        '```bash\ngh pr create -R o/r \\\n  --head "someuser:<branch name>" \\\n  --title "x"\n```'
    ), "a literal login is wrong for every contributor but the one who wrote it"

    assert not _create_calls_without_a_head(
        "| `gh pr create` | ✅ | ✅ |\n\nTry `gh pr create` when the branch is ready.\n"
    ) and not _create_calls("| `gh pr create` | ✅ | ✅ |\n"), (
        "a capability table and a sentence are not the command — the scan reads fenced blocks only"
    )


#: The tier rule a dirty tree used to carry, in each template's own words. #1563
#: (`69745b8c`, 2026-09-24) made the daemon converge the tree itself and leave the
#: configured tier intact — and its file list contains **no** `prompt.md` (measured:
#: `git show --name-only 69745b8c | grep -c prompt.md` → 0), so both task templates kept
#: stating the retired rule while the code, its test and that test's own old name
#: (`test_dirty_tree_forces_read_only_structural_guard`) all moved.
RETIRED_DIRTY_TIER_CLAIMS = (
    "it means this cycle runs **read-only**",
    "read-only cycle (no git writes, no PR submission)",
    "or forced by the dirty-tree guard above",
    "**Dirty tree read-only**",
)

#: The mechanism the corrected clause names instead. It can only be present if the clause
#: states what the daemon does now, so it is the positive anchor that keeps the absences
#: above from passing on a clause someone deleted.
CONVERGENCE_ANCHOR = "refs/emrg/rescue/"


def test_a_dirty_tree_no_longer_costs_a_cycle_its_tier_in_either_prompt() -> None:
    """Both task prompts state the tier rule the guard now implements, not the retired one.

    A prompt is not documentation — its reader acts on it. `_effective_sandbox` converges a
    dirty tree itself and leaves the configured tier intact, so a cycle told "a dirty tree
    means you run read-only" either skips work it could do or reports a tier nothing set.
    The retired strings are what a later edit would paste back if it re-derived the clause
    from the pre-#1563 behaviour.
    """
    for name in ("journal_prompt.md", "open_source_prompt.md"):
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        survivors = [claim for claim in RETIRED_DIRTY_TIER_CLAIMS if claim in text]
        assert not survivors, (
            f"{name} still states the retired dirty-tree tier rule: {survivors}. Since "
            "#1563 the daemon converges a dirty tree itself and the cycle keeps its "
            "configured tier; `read-only` follows a convergence that **failed**, never "
            "dirt alone"
        )
        assert CONVERGENCE_ANCHOR in text, (
            f"{name} no longer names the mechanism that replaced the rule "
            f"({CONVERGENCE_ANCHOR!r}) — the absence asserted above would then pass on a "
            "clause that was deleted rather than corrected, which is a failure to "
            "measure, not a pass"
        )
