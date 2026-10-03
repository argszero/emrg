"""Every `.j2` prompt render site provides the names its template needs.

Why this file exists
--------------------
The daemon renders every prompt through jinja2 with ``undefined=jinja2.Undefined``, so a
name a render site does not provide is not an error: it renders as an **empty string**.
A typo therefore breaks nothing a test can see — the prompt tells the agent to write into a
blank path, or to answer a question whose evidence section is blank, and the whole suite
stays green.

That hole is already closed for the six `.md` task templates, from both ends, by
``tests/test_prompt_templates.py``. It was open for the four `.j2` prompts — including
``system.j2``, the one prompt **every** session is rendered under, and the two prompts a
task's completion is judged by. Measured 2026-10-04 (cyc20261004-063900) before writing
this file: all four sites are exactly wired today (the names each template needs are
exactly the names its site provides), so this is the guard that keeps them so, not the
repair of a live blank. What it would have caught is the next edit — a `{{ new_field }}`
added to a template whose site does not pass it, or a kwarg left behind after its last
template line was deleted.

Two readers, because the two shapes cannot be read alike
-------------------------------------------------------
* A site that spells its context out — ``.render(cap=…, lines=…)`` — is read **statically**,
  from the call's own keywords.
* ``system.j2``'s site passes ``**ctx``, a dict assembled conditionally from a session. Its
  provided set is read **by capturing the real context** off a real ``_build_system_prompt``
  call, over several sessions, and taking the union. The union is the point: `skills` and
  the memory keys are set only when there is something to set them from, so a single bare
  session would report a name missing that the template merely guards.

Named limits
------------
* A name a template assembles at runtime (``{{ namespace[field] }}``) is outside what a
  parse can show; the reader is jinja2's own ``find_undeclared_variables``, so it sees
  exactly what the renderer must resolve.
* The template must be named at the site by a literal or by a module-level string constant.
  A site whose template name is computed is reported as **unreadable** rather than skipped,
  so it cannot pass by not being seen.
* This reads prompts as templates, not as prose. Whether the prompt's text is any good is
  not a question a render can answer.
"""

from __future__ import annotations

import ast
import contextlib
import tempfile
from dataclasses import dataclass
from pathlib import Path

import jinja2
from jinja2 import meta

from emrg.config import LlmConfig
from emrg.server import daemon as daemon_mod
from emrg.server.daemon import EmrgServer
from emrg.server.scheduler import TASK_TEMPLATES
from emrg.session import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
EMRG_DIR = REPO_ROOT / "emrg"
PROMPTS_DIR = EMRG_DIR / "server" / "prompts"
SERVER_DIR = EMRG_DIR / "server"


# ── The scanner ────────────────────────────────────────────────────────────────────
#
# A render site is a `get_template(<name>).render(...)` call: chained, or split over two
# statements with the template bound to a local name first. Only those are collected —
# `term.render()` in the client and `row.render(ctx)` in the widgets are not jinja
# renders, and a scanner that swept every `.render(` in the tree would have to carry a
# list of exceptions instead of a rule.


@dataclass(frozen=True)
class RenderSite:
    """One `.render(...)` call reached from a `get_template(...)`."""

    path: str
    line: int
    function: str
    template: str | None  # None: the name is not a literal or a module constant
    provided: frozenset[str]  # the call's keyword names; empty when it passes `**expr`

    @property
    def dynamic(self) -> bool:
        """Does the call pass a splatted mapping rather than named keywords?"""
        return not self.provided


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` bindings, so a template constant resolves."""
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                out[target.id] = node.value.value
    return out


def _template_name(expr: ast.expr, constants: dict[str, str]) -> str | None:
    """The template a `get_template(<expr>)` names, when it is readable."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.Name):
        return constants.get(expr.id)
    return None


def _chained_get_template(expr: ast.expr, constants: dict[str, str]) -> tuple[bool, str | None]:
    """`(is_a_get_template_call, template name)` for a call expression."""
    if not isinstance(expr, ast.Call):
        return False, None
    func = expr.func
    if not (isinstance(func, ast.Attribute) and func.attr == "get_template"):
        return False, None
    if not expr.args:
        return True, None
    return True, _template_name(expr.args[0], constants)


def render_sites_in(source: str, path: str) -> list[RenderSite]:
    """Every `get_template(...)`-rooted `.render(...)` call in one module's source."""
    tree = ast.parse(source)
    constants = _module_string_constants(tree)
    sites: list[RenderSite] = []
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Local names bound from a `get_template(...)` call, so the split shape is read.
        bound: dict[str, str | None] = {}
        for stmt in ast.walk(scope):
            if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            if isinstance(target, ast.Name) and isinstance(stmt.value, ast.Call):
                is_get, name = _chained_get_template(stmt.value, constants)
                if is_get:
                    bound[target.id] = name
        for call in ast.walk(scope):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                continue
            if call.func.attr != "render":
                continue
            receiver = call.func.value
            if isinstance(receiver, ast.Name):
                if receiver.id not in bound:
                    continue
                template = bound[receiver.id]
            else:
                is_get, template = _chained_get_template(receiver, constants)
                if not is_get:
                    continue
            if any(keyword.arg is None for keyword in call.keywords):
                provided: frozenset[str] = frozenset()
            else:
                provided = frozenset(
                    keyword.arg for keyword in call.keywords if keyword.arg is not None
                )
            sites.append(
                RenderSite(
                    path=path,
                    line=call.lineno,
                    function=scope.name,
                    template=template,
                    provided=provided,
                )
            )
    return sites


def all_render_sites() -> list[RenderSite]:
    """Every render site in the package, in path order."""
    sites: list[RenderSite] = []
    for path in sorted(EMRG_DIR.rglob("*.py")):
        sites.extend(render_sites_in(path.read_text(encoding="utf-8"), str(path.relative_to(REPO_ROOT))))
    return sites


def needed_names(template_text: str) -> set[str]:
    """The names a template needs, by jinja2's own parser.

    ``find_undeclared_variables`` is the reader the renderer itself uses to decide what to
    look up in the context, so this cannot disagree with the render about which names must
    be provided — which a hand-written regex over `{{ }}` can (it reads a loop's own
    variable and a `{% set %}` target as context names; jinja2 does not).
    """
    env = jinja2.Environment()
    return set(meta.find_undeclared_variables(env.parse(template_text)))


def _prompt_files() -> dict[str, Path]:
    """Every shipped jinja prompt, by the name a render site would ask for."""
    return {path.name: path for path in sorted(PROMPTS_DIR.glob("*.j2"))}


# ── The instrument's controls, before its verdict is believed ──────────────────────


def test_the_scanner_reads_the_shapes_a_render_site_is_written_in() -> None:
    """The scans below pass on a scanner that finds nothing, so it is controlled first.

    Both spellings the package uses are pinned (a chained call, and a template bound to a
    local name first), the module constant the compaction site names its template with,
    and the negative control: a `.render()` on a receiver no `get_template` produced is
    not a prompt render, and must not be collected as one.
    """
    chained = render_sites_in(
        "def f():\n"
        "    return env.get_template('a.j2').render(alpha=1, beta=2)\n",
        "m.py",
    )
    assert len(chained) == 1, chained
    assert chained[0].template == "a.j2"
    assert chained[0].provided == frozenset({"alpha", "beta"})

    bound = render_sites_in(
        "TEMPLATE = 'b.j2'\n"
        "def g():\n"
        "    template = env.get_template(TEMPLATE)\n"
        "    return template.render(**ctx)\n",
        "m.py",
    )
    assert len(bound) == 1, bound
    assert bound[0].template == "b.j2", "a module-level constant must resolve"
    assert bound[0].dynamic, "`**ctx` is the splatted shape, not 'no keywords'"

    not_a_prompt = render_sites_in(
        "def h():\n"
        "    term.render()\n"
        "    row.render(ctx)\n",
        "m.py",
    )
    assert not not_a_prompt, (
        f"a `.render()` no `get_template` produced was collected as a prompt render: "
        f"{not_a_prompt} — the scanner must read the shape, not the method name"
    )


def test_the_template_scanner_answers_both_ways() -> None:
    """`needed_names` must see a real name and not invent one.

    The second half is why jinja2's parser is used rather than a regex: a loop variable and
    a `{% set %}` target are the template's own bindings, and a scan that reported them
    would demand context keys no site can provide — a false red on every cycle.
    """
    assert needed_names("{{ alpha }} {{ beta.gamma }}") == {"alpha", "beta"}
    assert needed_names("{% if delta %}x{% endif %}") == {"delta"}
    assert needed_names("{% for row in rows %}{{ row.name }}{% endfor %}") == {"rows"}
    assert needed_names("{% set local = task.name %}{{ local }}") == {"task"}
    assert needed_names("no tags at all") == set()


# ── The population: which prompts this file is responsible for ─────────────────────


def test_every_jinja_prompt_is_rendered_by_a_site_this_file_reads() -> None:
    """Every `prompts/*.j2` is named by a discovered site, and no site is unreadable.

    The population rule, and the reason it is asserted rather than assumed: a fifth `.j2`
    prompt added without a render site — or with one whose template name is computed — is a
    prompt nothing measures. A site whose name does not resolve is reported here rather
    than skipped, so this cannot pass by not looking.
    """
    sites = all_render_sites()
    unreadable = [site for site in sites if site.template is None]
    assert not unreadable, (
        "these render sites name their template in a way this scanner cannot read "
        f"({[(s.path, s.line, s.function) for s in unreadable]}) — name it as a literal or "
        "a module-level constant, or extend the scanner; a site this file cannot read is "
        "one it cannot check"
    )

    jinja_prompts = _prompt_files()
    assert len(jinja_prompts) >= 4, (
        f"only {len(jinja_prompts)} `.j2` prompt(s) found under {PROMPTS_DIR} — the scan is "
        "not looking where it thinks it is"
    )

    rendered = {site.template for site in sites if site.template in jinja_prompts}
    never_rendered = sorted(set(jinja_prompts) - rendered)
    assert not never_rendered, (
        f"these prompts are shipped but no render site names them: {never_rendered} — a "
        "prompt nothing renders is text nothing measures"
    )
    assert len(rendered) == len(jinja_prompts)


def test_the_two_guards_together_cover_every_prompt_the_daemon_ships() -> None:
    """This file's prompts and the task templates' are the whole population, and disjoint.

    `tests/test_prompt_templates.py` covers the six `.md` task templates from both
    directions; this file covers the `.j2` prompts. The split is deliberate, not an
    accident of two authors — so it is asserted: every prompt file under `emrg/server/` is
    in exactly one of the two sets. A new prompt file that lands in neither fails here
    rather than being silently unmeasured.
    """
    jinja = set(_prompt_files())
    task_templates = set(TASK_TEMPLATES.values())

    shipped = {
        path.name for path in SERVER_DIR.glob("*.md") if path.name.endswith("_prompt.md")
    } | jinja
    covered = jinja | task_templates

    assert shipped, f"no prompt files found under {SERVER_DIR} — this check would be vacuous"
    assert jinja.isdisjoint(task_templates), (
        f"a template is claimed by both guards: {sorted(jinja & task_templates)} — one rule, "
        "one home"
    )
    assert not shipped - covered, (
        f"these prompt files are covered by neither guard: {sorted(shipped - covered)}"
    )
    assert not covered - shipped, (
        f"a guard names a prompt file this repository does not ship: {sorted(covered - shipped)}"
    )
    assert len(shipped) >= 9, (
        f"only {len(shipped)} prompt file(s) found — the globs above are not reading the tree"
    )


# ── Direction 1: the names a template needs ─────────────────────────────────────────


def test_every_jinja_render_site_provides_the_names_its_template_needs() -> None:
    """A name a template needs must be a name its render site hands it.

    For a site that spells its context out this is read straight from the call's own
    keywords, so the reading cannot drift from the call. `system.j2`'s site is covered by
    the builder capture below — named here so a new splatted site cannot slip past both.
    """
    prompts = _prompt_files()
    sites = [s for s in all_render_sites() if s.template in prompts]
    assert sites, "no render site for any shipped `.j2` prompt — nothing would be checked"

    dynamic = sorted({s.template for s in sites if s.dynamic})
    assert dynamic == ["system.j2"], (
        f"these sites pass a splatted context: {dynamic} — their provided names cannot be "
        "read from the call, so each needs its own capture (see "
        "test_the_system_prompt_context_is_wired_by_the_builder) or this check skips it"
    )

    offenders: list[str] = []
    for site in sites:
        if site.dynamic:
            continue
        needed = needed_names(prompts[site.template].read_text(encoding="utf-8"))
        missing = sorted(needed - set(site.provided))
        if missing:
            offenders.append(
                f"{site.path}:{site.line} ({site.function}) → {site.template}: {missing}"
            )
    assert not offenders, (
        "these render sites do not provide a name their template needs — with the daemon's "
        "`Undefined` it renders as an empty string and nothing fails:\n  "
        + "\n  ".join(offenders)
    )


def test_no_jinja_render_site_passes_a_name_its_template_never_reads() -> None:
    """The reverse: a value a site computes that no template line consumes.

    A value nothing consumes is free to become a lie in the reader's hands, because nothing
    measures it any more — the reason `tests/test_prompt_templates.py` asks this question of
    the task templates' context. Same question, same answer for the `.j2` sites.
    """
    prompts = _prompt_files()
    sites = [s for s in all_render_sites() if s.template in prompts]

    offenders: list[str] = []
    for site in sites:
        if site.dynamic:
            continue
        needed = needed_names(prompts[site.template].read_text(encoding="utf-8"))
        unread = sorted(set(site.provided) - needed)
        if unread:
            offenders.append(
                f"{site.path}:{site.line} ({site.function}) → {site.template}: {unread}"
            )
    assert not offenders, (
        "these render sites pass a name their template never reads (either the template "
        "line that consumed it was deleted, or the value is new and inert):\n  "
        + "\n  ".join(offenders)
    )


# ── Direction 1 and 2 for the one site whose context is assembled, not spelled ─────


def _make_server() -> EmrgServer:
    """A real server, with the projects log pointed away from the host's own.

    The shape `tests/test_daemon.py::_make_server` and `tests/test_memory_row_bound_reach.py`
    use, for the same reason: building a system prompt must never write the real
    `~/.emrg/projects.yml`.
    """
    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tempfile.mkdtemp()) / "projects.yml"
    return server


@contextlib.contextmanager
def _captured_context():
    """Run the real builder with the context it hands to `render` observed.

    A wrapper around the environment's `get_template`, rather than around
    `jinja2.Environment`: `_get_jinja_env` caches one environment for the process, so a
    subclass installed now would not be the one an already-warm cache returns. Restored by
    hand rather than through `monkeypatch.undo()`, which would also undo the caller's own
    patches.
    """
    real = daemon_mod._get_jinja_env
    seen: dict = {}

    class _RecordingTemplate:
        def __init__(self, inner) -> None:
            self._inner = inner

        def render(self, *args, **kwargs):
            seen.update(kwargs)
            return self._inner.render(*args, **kwargs)

    class _RecordingEnv:
        def __init__(self, inner) -> None:
            self._inner = inner

        def get_template(self, name):
            return _RecordingTemplate(self._inner.get_template(name))

    daemon_mod._get_jinja_env = lambda: _RecordingEnv(real())
    try:
        yield seen
    finally:
        daemon_mod._get_jinja_env = real


class _Skill:
    """The minimum a skill entry needs to reach the context."""

    def __init__(self, path: Path) -> None:
        self.name = "demo"
        self.source = "user"
        self.path = path
        self.description = "a skill that exists so the skills section renders"


def _session(tmp_path: Path, name: str, *, project_index: bool, session_index: bool) -> Session:
    """A session, optionally carrying the memory indexes the optional sections need."""
    if project_index:
        memory_dir = tmp_path / ".emrg" / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "MEMORY.md").write_text(
            "# Memory Index\n\n| id | note |\n| --- | --- |\n| a1 | one row |\n",
            encoding="utf-8",
        )
    session = Session.create_with_id(name, tmp_path)
    if session_index:
        session.memory_dir.mkdir(parents=True, exist_ok=True)
        (session.memory_dir / "MEMORY.md").write_text(
            "# Session Index\n\n| id | note |\n| --- | --- |\n| s1 | one row |\n",
            encoding="utf-8",
        )
    return session


def _provided_by_the_builder(tmp_path: Path) -> set[str]:
    """The union of the keys `_build_system_prompt` provides, over several sessions.

    The union is the whole point. `skills` is provided only when there are skills, and the
    memory keys only when there is an index, so any single session reports names the
    template merely *guards* as missing — a false red that would make this guard useless.
    Taken together the scenarios reach every line of the template, and what remains missing
    is a name no session can supply. That the union is really built from several scenarios
    is itself controlled, in `test_the_builder_capture_reaches_the_guarded_sections`.
    """
    server = _make_server()
    provided: set[str] = set()
    for index, (with_skills, project_index, session_index) in enumerate(
        (
            (False, False, False),
            (True, False, False),
            (False, True, False),
            (False, True, True),
        )
    ):
        server.skills = [_Skill(tmp_path / "demo.md")] if with_skills else []
        session = _session(tmp_path, f"wired-{index}", project_index=project_index,
                           session_index=session_index)
        with _captured_context() as seen:
            rendered = server._build_system_prompt(session)
        assert rendered, "the builder produced an empty prompt — the capture checked nothing"
        provided |= set(seen)
    return provided


def test_the_system_prompt_context_is_wired_by_the_builder(tmp_path: Path) -> None:
    """`system.j2`'s context is assembled, so it is measured from the real builder.

    Both directions in one place, because both are read off the same capture: every name
    the template needs is provided (a blank section otherwise), and every provided name is
    read by the template (a value nothing measures otherwise). Measured 2026-10-04 before
    this test existed: 17 names needed, 17 provided, the same 17.
    """
    template = PROMPTS_DIR / "system.j2"
    assert template.is_file(), f"{template} is not shipped — this test would check nothing"
    needed = needed_names(template.read_text(encoding="utf-8"))
    assert len(needed) >= 17, (
        f"system.j2 names only {len(needed)} context key(s): {sorted(needed)} — too few to "
        "show the parser is reading the template"
    )

    provided = _provided_by_the_builder(tmp_path)

    missing = sorted(needed - provided)
    assert not missing, (
        f"the builder never provides {missing}, which `system.j2` needs — with the daemon's "
        "`Undefined` the section renders blank in every session and nothing fails"
    )
    unread = sorted(provided - needed)
    assert not unread, (
        f"the builder provides {unread}, which `system.j2` never reads — a value nothing "
        "consumes is a value nothing measures"
    )


def test_the_builder_capture_reaches_the_guarded_sections(tmp_path: Path) -> None:
    """The capture's own control: the conditional keys really do come from the scenarios.

    Without this, `test_the_system_prompt_context_is_wired_by_the_builder` would pass for
    the wrong reason if the capture silently stopped working and returned one scenario's
    keys — the names under `{% if %}` would then read as "provided" only by accident. The
    assertion is that the optional keys are present, and that at least one of them is
    *absent* in the bare scenario: a key the union gains is the evidence the union is doing
    the work.
    """
    server = _make_server()
    session = _session(tmp_path, "capture-control", project_index=False, session_index=False)
    server.skills = []
    with _captured_context() as seen:
        server._build_system_prompt(session)
    assert "working_dir" in seen and "shell_tool" in seen, (
        f"the capture missed the unconditional keys ({sorted(seen)}) — it is not in the "
        "render path"
    )
    assert "has_memories" not in seen and "skills" not in seen, (
        "a bare session must not already provide the guarded keys, or the union above "
        "proves nothing about which scenario supplied them"
    )
