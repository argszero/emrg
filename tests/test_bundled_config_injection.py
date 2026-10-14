"""The packaging-time bundled default config, and the lines it must leave alone.

The subject is ``packaging/bundle_default_config.py``: the step that puts a working
``[llm]`` block into a **staged** runtime, from build secrets, so a fresh install works
before its host has edited anything (host, 2026-10-09T21:32:35).

Two readings this file exists to keep apart, because a whole-file search cannot:

* the **carrier** — the first-run template inside ``emrg/config.py`` (what ``ensure_config``
  writes) and inside ``emrg/gui/main.js`` (what the packaged GUI writes when no config
  exists). Both are patched.
* the **sentinels and readers beside them** — ``PLACEHOLDER_API_KEY = "sk-..."``, the value
  the request path refuses and the wizard recognises; ``key !== "sk-..."``, the GUI's
  reader; and the ``[[llm.models]]`` entries' own ``model = "…"``. Rewriting any of those
  would ship a product that refuses the key it bundles, so each is asserted in place.

Every test works on **copies under ``tmp_path``**: the script is pointed at a staged root
this file creates, and the repository's own carriers are read only. Two of them drive the
module in-process, the rest run it as a subprocess so the environment handling is the real
one.

A carrier's template is read **the way the product reads it** — CPython for ``config.py``,
node for ``main.js`` — because the span lives inside a string literal and the bytes on disk
are not the string that literal yields (issue #2272). A guard that parsed the file's text
would share the blind spot it exists to cover.
"""

from __future__ import annotations

import importlib.util
import json
import os
import runpy
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "packaging" / "bundle_default_config.py"

#: The two carriers on master, and the value each holds that must survive the patch.
CARRIERS = ("emrg/config.py", "emrg/gui/main.js")
SENTINEL_CONSTANT = 'PLACEHOLDER_API_KEY = "sk-..."'
GUI_READER = 'key !== "sk-..."'


def _load_module():
    spec = importlib.util.spec_from_file_location("bundle_default_config", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _staged(tmp_path: Path) -> Path:
    """A staged source tree holding copies of the two carriers, as packaging stages them."""
    root = tmp_path / "source"
    for rel in CARRIERS:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, target)
    return root


def _run(root: Path, env: dict[str, str], *extra: str) -> subprocess.CompletedProcess[str]:
    """The script as a subprocess, with an environment holding only ``env``'s EMRG_BUNDLED_*.

    Everything else is inherited — a missing ``PATH`` would make the exit status a fact
    about the machine — but any ``EMRG_BUNDLED_*`` the caller did not name is removed, so
    a variable exported in the developer's shell cannot decide a test's outcome.
    """
    clean = {k: v for k, v in os.environ.items() if not k.startswith("EMRG_BUNDLED_")}
    clean.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--source-root", str(root), *extra],
        env=clean, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


#: The values a configured release build would carry — the host's own, from 2026-10-09.
VALUES = {
    "EMRG_BUNDLED_API_KEY": "atk_live_test_value_not_a_real_key",
    "EMRG_BUNDLED_BASE_URL": "https://atp-dev.example.invalid/v1",
    "EMRG_BUNDLED_MODEL": "deepseek-flash",
    "EMRG_BUNDLED_CONTEXT_WINDOW": "1024000",
    "EMRG_BUNDLED_VISION": "true",
}


#: Node evaluates the carrier's own declaration, so no Python scanner of the file stands in
#: for JS's escaping. The statement is located the way the carrier writes it — the literal
#: closes on a line that is a backtick and a semicolon — and run in an empty context, where
#: the only name it reads is the one it defines.
_JS_READ = """
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
const start = src.indexOf("const CONFIG_TEMPLATE");
const end = src.indexOf("\\n`;", start);
if (start < 0 || end < 0) {
  console.error("main.js no longer declares CONFIG_TEMPLATE the way this reading expects");
  process.exit(2);
}
process.stdout.write(JSON.stringify(vm.runInNewContext(src.slice(start, end + 3) + "\\nCONFIG_TEMPLATE", {})));
"""


def _render_python_carrier(path: Path) -> str:
    """The template a ``.py`` carrier holds, as **CPython** yields it.

    ``runpy.run_path`` executes the module body, so the f-string is evaluated exactly as the
    import the product performs would evaluate it — and a value the f-string reinterprets is
    the ``NameError`` issue #2272 measured, raised here rather than read past. The file is a
    staged copy: its body defines classes, functions and four constants and calls nothing, and
    the names it imports resolve against this checkout.
    """
    namespace = runpy.run_path(str(path), run_name="staged_config_template")
    template = namespace["CONFIG_TEMPLATE"]
    assert isinstance(template, str), f"{path}: CONFIG_TEMPLATE is no longer a string"
    return template


def _node() -> str:
    """The ``node`` that actually runs here, or a skip naming why the reading is unavailable.

    ``shutil.which`` alone is not enough. On this host ``node`` is an ``asdf`` shim, which
    resolves the toolchain through ``$HOME`` and exits 126 when ``HOME`` points elsewhere —
    which is the environment a mutation arm runs its child in, so the probe decides between
    "this reading is not available here" and a red that would be about the host rather than
    about the carrier. The probe runs one trivial program; nothing about the carrier is read
    before it answers.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip(
            "no `node` on PATH: this reading evaluates the carrier's template literal in the "
            "language the GUI ships in, and cannot do that without that language's runtime"
        )
    probe = subprocess.run(
        [node, "-e", "0"],
        capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
    )
    if probe.returncode != 0:
        pytest.skip(
            f"`{node}` is on PATH but cannot run here (exit {probe.returncode}): this reading "
            "evaluates the carrier's template literal in the language the GUI ships in, and an "
            "unavailable runtime is not a finding about the carrier"
        )
    return node


def _render_js_carrier(path: Path) -> str:
    """The template a ``.js`` carrier holds, as **node** yields it.

    A different engine from any this repository writes in, which is the point: the escaping
    the injection has to get right is JS's own, and no Python reading of the file can be the
    judge of it.
    """
    node = _node()
    harness = path.parent / "_render_template.js"
    harness.write_text(_JS_READ, encoding="utf-8")
    result = subprocess.run(
        [node, str(harness), str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, f"{path}: node could not evaluate it: {result.stderr}"
    return json.loads(result.stdout)


def _carrier_template(path: Path) -> str:
    """The first-run template ``path`` holds, as the language that ships it yields it."""
    if path.suffix == ".js":
        return _render_js_carrier(path)
    return _render_python_carrier(path)


def _template_of(path: Path, module) -> dict:
    """The ``[llm]`` section of the template inside ``path``, parsed as TOML.

    The span is taken from the **rendered** template, not from the file's bytes: both carriers
    write it inside a string literal, so a value read out of the file is a value the carrier
    may yet rewrite. The span *begins* with the heading, so the parsed document has exactly
    one top-level table, ``llm``; it is unwrapped here so callers name keys rather than the
    section that holds them, and so a span that stopped opening that table is a failure and
    not a silently different reading.
    """
    rendered = _carrier_template(path)
    span = module.section_span(rendered)
    assert span is not None, f"{path} no longer renders an [llm] template span"
    start, end = span
    parsed = tomllib.loads(rendered[start:end])
    assert set(parsed) == {"llm"}, f"{path}: the rendered span no longer opens the [llm] table"
    return parsed["llm"]


# ── the environment contract ───────────────────────────────────────────────────────


def test_one_variable_per_key_and_nothing_else_is_read():
    """The names are the keys, so a new key is one line and a typo is visible."""
    module = _load_module()
    for key in module.BUNDLED_KEYS:
        env = {f"EMRG_BUNDLED_{key.upper()}": "x"}
        assert module.env_overrides(env) == {key: "x"}


def test_an_empty_value_is_unset_not_an_empty_string():
    """An unconfigured GitHub secret evaluates to ``''``, and must mean "leave it alone".

    This is the direction that decides whether the mechanism is inert: if ``''`` were a
    value, adding the *names* to the workflow without configuring the secrets would ship
    ``api_key = ""`` — a config worse than the sentinel it replaced.
    """
    module = _load_module()
    assert module.env_overrides({"EMRG_BUNDLED_API_KEY": ""}) == {}
    assert module.env_overrides({}) == {}


def test_a_variable_named_after_no_key_is_not_a_configuration():
    module = _load_module()
    assert module.env_overrides({"EMRG_BUNDLED_SOMETHING_ELSE": "x"}) == {}


#: The spellings of a secret both carriers' literals reinterpret, from issue #2272's table and
#: the two neighbours of the same mechanism it does not list: a **newline** (what
#: ``gh secret set KEY < file`` appends, and which would end the assignment line) and a
#: **backtick** (which closes a JS template literal). Read on the *rendered* template, so the
#: assertion is about the string the product will write, not about the escaping.
REINTERPRETED_VALUES = {
    "brace": "sk-A{b}C9d8e7f6",
    "double-quote": 'sk-A"B',
    "backslash": "sk-A\\B",
    "template-substitution": "sk-A${process.env.HOME}B",
    "newline": "sk-A\nB",
    "backtick": "sk-A`B",
    "triple-single-quote": "sk-A'''B",
}


# ── the injected direction ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("shape,value", sorted(REINTERPRETED_VALUES.items()))
def test_a_value_each_literal_reinterprets_reaches_both_carriers(tmp_path, shape, value):
    """The value the carrier's own language would rewrite must come back out of it.

    This is issue #2272's acceptance, one spelling at a time and on **both** carriers, because
    the two literals reinterpret different characters — a brace is an f-string field and a
    dollar brace a JS substitution — and a value escaped for one is not a value escaped for the
    other. ``base_url`` rides along so the assertion is not about one key's line shape: every
    override in the run has to come back equal.
    """
    module = _load_module()
    root = _staged(tmp_path)
    result = _run(root, {"EMRG_BUNDLED_API_KEY": value, "EMRG_BUNDLED_BASE_URL": value})
    assert result.returncode == 0, result.stderr
    for rel in CARRIERS:
        parsed = _template_of(root / rel, module)
        assert parsed["api_key"] == value, f"{rel} did not render the {shape} api_key"
        assert parsed["base_url"] == value, f"{rel} did not render the {shape} base_url"


def test_the_repository_carriers_render_them_too(tmp_path):
    """The two real carriers, not a fixture shaped like them: the escape is written for these.

    A staged tree holds copies, but the *literal each copy is inside* is the file's own — an
    f-string here and a template literal there — so this drives the same two languages on the
    same two files a release build patches.
    """
    module = _load_module()
    root = _staged(tmp_path)
    value = REINTERPRETED_VALUES["brace"]
    assert _run(root, {"EMRG_BUNDLED_API_KEY": value}).returncode == 0
    assert _template_of(root / CARRIERS[0], module)["api_key"] == value
    assert _template_of(root / CARRIERS[1], module)["api_key"] == value


def test_the_configured_values_reach_both_carriers(tmp_path):
    """Both writers of a fresh install, not only the Python one.

    A GUI-first host never runs ``ensure_config`` — ``main.js`` writes the template itself
    in packaged mode — so patching one loader would leave exactly half of a fresh install
    without a key, and the failure would look like "the wizard sometimes appears".
    """
    module = _load_module()
    root = _staged(tmp_path)
    result = _run(root, VALUES)
    assert result.returncode == 0, result.stderr

    expected = {
        "api_key": VALUES["EMRG_BUNDLED_API_KEY"],
        "base_url": VALUES["EMRG_BUNDLED_BASE_URL"],
        "model": VALUES["EMRG_BUNDLED_MODEL"],
        "context_window": 1024000,
        "vision": True,
    }
    for rel in CARRIERS:
        parsed = _template_of(root / rel, module)
        for key, value in expected.items():
            assert parsed[key] == value, f"{rel} did not carry {key}"
        # The rest of the template is untouched: a key nothing named keeps its default.
        assert parsed["temperature"] == 0.7
        assert parsed["api_key"] != "sk-..."


def test_the_dry_run_reports_without_writing(tmp_path):
    root = _staged(tmp_path)
    before = {rel: (root / rel).read_bytes() for rel in CARRIERS}
    result = _run(root, VALUES, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "would patch" in result.stdout
    for rel in CARRIERS:
        assert (root / rel).read_bytes() == before[rel]


def test_a_third_carrier_is_discovered_not_listed(tmp_path):
    """A fourth writer copied from one of these must be caught without editing the script.

    The population assertion is on the *set*, and the baseline is asserted non-empty first —
    a discovery that found nothing would otherwise satisfy "every discovered carrier was
    patched" while patching nothing at all.
    """
    module = _load_module()
    root = _staged(tmp_path)
    (root / "emrg" / "another_writer.py").write_bytes((root / CARRIERS[0]).read_bytes())
    result = _run(root, VALUES)
    assert result.returncode == 0, result.stderr
    assert "another_writer.py" in result.stdout
    discovered = {p.relative_to(root).as_posix() for p in module.discover(root)}
    assert discovered == set(CARRIERS) | {"emrg/another_writer.py"}


# ── the neighbours that must not move ──────────────────────────────────────────────


def test_the_sentinel_and_the_readers_are_outside_the_span(tmp_path):
    """The neighbours the patch must not touch, each asserted in place.

    ``sk-...`` occurs **six** times across the two carriers — three per file — and only the
    two occurrences inside the ``[llm]`` span's ``api_key`` line are the template's own
    assignment. The other four are neighbours: the sentinel constant and a prose mention in
    ``config.py``, the GUI's reader and a prose mention in ``main.js``. Rewriting the
    sentinel would make the request path refuse the bundled key and open the wizard for a
    configured install — the two halves of one rule disagreeing — so this is the assertion
    the whole span boundary exists for, and the per-file count is what says all four stayed.
    """
    module = _load_module()
    root = _staged(tmp_path)
    result = _run(root, VALUES)
    assert result.returncode == 0, result.stderr

    config_text = (root / "emrg" / "config.py").read_text(encoding="utf-8")
    gui_text = (root / "emrg" / "gui" / "main.js").read_text(encoding="utf-8")

    assert SENTINEL_CONSTANT in config_text, "the sentinel constant was rewritten"
    assert GUI_READER in gui_text, "the GUI's reader was rewritten"
    # After the patch exactly one quoted sentinel survives in each carrier, and it is the
    # neighbour — the constant in config.py, the reader in main.js. The template's own
    # assignment is the occurrence the patch replaces, so a whole-file replacement would
    # have taken the survivor with it: the count over the whole file tells them apart.
    assert config_text.count('"sk-..."') == 1, "config.py lost the neighbour beside the span"
    assert gui_text.count('"sk-..."') == 1, "main.js lost the neighbour beside the span"
    for rel in CARRIERS:
        text = (root / rel).read_text(encoding="utf-8")
        start, end = module.section_span(text)
        # The span's *bytes* are not the string the carrier yields, so the two assertions
        # about what the template now says are read off the rendered span — the file's own
        # language is what decides them (issue #2272). The assertions about the neighbours
        # stay on the bytes, because those lines are the ones the patch must not touch.
        rendered = module.render_span(text, module.literal_kind(text, start))
        # Every mention (prose included) survives, and the span's own assignment is gone.
        assert text.count("sk-...") == 2, f"{rel}: one of the two neighbours beside the span moved"
        assert 'api_key = "sk-..."' not in text, f"{rel}: the template still holds the sentinel"
        assert 'model = "deepseek-flash"' in rendered, f"{rel}: the span kept its old model"
        # The [[llm.models]] entries keep their own `model` lines — the same key name, one
        # section down, which a whole-file replacement would have overwritten.
        assert 'name = "deepseek-v3"' in text
        assert text.count('model = "deepseek-chat"') == 1, (
            f"{rel}: a [[llm.models]] entry's own model line moved"
        )


def test_a_key_the_template_no_longer_has_is_an_error_not_a_silent_miss(tmp_path):
    """A renamed template key must stop the build, not ship an install without a key.

    Driven by mutating the *fixture* — the staged copy loses ``base_url`` from its span,
    as a rename would — because no variable name can produce this on master. The failure
    is what makes the script's exit status load-bearing under ``set -e``.
    """
    root = _staged(tmp_path)
    target = root / CARRIERS[0]
    text = target.read_text(encoding="utf-8")
    target.write_text(
        text.replace('base_url = "https://api.deepseek.com"\n', "", 1), encoding="utf-8"
    )
    result = _run(root, VALUES)
    assert result.returncode == 1
    assert "base_url" in result.stderr


def test_a_staged_tree_with_no_carrier_is_an_error(tmp_path):
    """ "Nothing to patch" and "the template moved" are different readings, and only one
    of them is safe to continue past. An empty staged root is the second."""
    module = _load_module()
    empty = tmp_path / "source"
    empty.mkdir()
    (empty / "unrelated.py").write_text("x = 1\n", encoding="utf-8")
    assert module.discover(empty) == []
    result = _run(empty, VALUES)
    assert result.returncode == 1
    assert "no carrier" in result.stderr


def test_the_skipped_directories_are_read_relative_to_the_root(tmp_path):
    """A root *under* a directory named like an ignored one is still searched.

    The staged tree is ``$ROOT/dist/runtime/source`` on a runner, so the skip has to be a
    fact about the path inside the root, not about the absolute path: matched absolutely, a
    root whose ancestors contain one of those names answers "no carrier" and the build stops
    for a reason that has nothing to do with the carriers. Measured against the absolute
    form, this layout fails.
    """
    module = _load_module()
    buried = tmp_path / "node_modules" / "source"
    for rel in CARRIERS:
        target = buried / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, target)
    assert {p.relative_to(buried).as_posix() for p in module.discover(buried)} == set(CARRIERS)
    result = _run(buried, VALUES)
    assert result.returncode == 0, result.stderr
    assert _template_of(buried / CARRIERS[0], module)["api_key"] == VALUES["EMRG_BUNDLED_API_KEY"]


# ── the write is checked by reading it back ────────────────────────────────────────


def _patched(tmp_path: Path, env: dict[str, str], rel: str = CARRIERS[0]) -> tuple[Path, str, str]:
    """A staged tree patched with ``env``; returns the carrier, its text and its literal kind."""
    module = _load_module()
    root = _staged(tmp_path)
    assert _run(root, env).returncode == 0
    path = root / rel
    text = path.read_text(encoding="utf-8")
    span = module.section_span(text)
    assert span is not None
    return path, text, module.literal_kind(text, span[0])


def test_the_read_back_finds_a_value_that_is_not_there(tmp_path):
    """Both directions, because a read-back that cannot fail proves nothing.

    ``unlanded_keys`` is the only thing standing between "the patch ran" and "the artifact has
    a key", so it is exercised on a rendered span that holds the value and on one asked for a
    different value — the shape a half-applied write leaves — and not only on the tree a write
    produced.
    """
    module = _load_module()
    keyed = {name.removeprefix("EMRG_BUNDLED_").lower(): value for name, value in VALUES.items()}
    _path, text, kind = _patched(tmp_path, VALUES)
    rendered = module.render_span(text, kind)
    assert module.unlanded_keys(rendered, keyed) == []
    assert module.unlanded_keys(rendered, {**keyed, "temperature": "0.25"}) == ["temperature"]


def test_a_value_written_without_the_literal_layer_is_never_reported_as_landed(tmp_path):
    """The direction the old check could not see, and the reason the check moved.

    The bytes-on-disk reading asked whether the file *held* a line, and a value written as the
    TOML text (the pre-#2272 shape) held exactly that line while the carrier's language
    rewrote it. Here the staged fixture is given that write by hand: the f-string's braces stay
    single, so no rendering of the file yields a template, and the check has no reading under
    which this passed.
    """
    module = _load_module()
    root = _staged(tmp_path)
    path = root / CARRIERS[0]
    text = path.read_text(encoding="utf-8")
    naive = text.replace('api_key = "{PLACEHOLDER_API_KEY}"', 'api_key = "sk-A{b}C"')
    assert naive != text, "the fixture no longer holds the sentinel assignment this test rewrites"
    path.write_text(naive, encoding="utf-8")

    span = module.section_span(naive)
    assert span is not None
    kind = module.literal_kind(naive, span[0])
    with pytest.raises(module.LiteralError):
        module.render_span(naive, kind)

    # ... and the same tree through the script's own reading: the build stops rather than
    # reporting the value it never wrote.
    result = _run(root, {"EMRG_BUNDLED_API_KEY": "sk-A{b}C9d8e7f6"})
    assert result.returncode == 0, result.stderr  # the patch itself re-encodes it correctly
    assert _template_of(path, module)["api_key"] == "sk-A{b}C9d8e7f6"


def test_a_write_that_does_not_land_stops_the_build(tmp_path, monkeypatch, capsys):
    """The read-back is **wired** to the exit status, not merely defined.

    ``unlanded_keys`` can be perfectly correct and reach nothing, and the failure it names is
    silent: a release with no key looks exactly like a release from before the feature. So the
    write is made to vanish — ``write_text`` is the single call between the patch and the
    reading — and the check then runs on a file that still holds the sentinel, which is the
    half-applied release this step exists to refuse. The status is the assertion, because
    under ``set -e`` in a release build it is the only thing that stops the artifact.
    """
    module = _load_module()
    root = _staged(tmp_path)
    monkeypatch.setenv("EMRG_BUNDLED_API_KEY", "sk-a-key-that-never-landed")
    monkeypatch.setattr(Path, "write_text", lambda self, *args, **kwargs: len(str(args[0])))
    assert module.main(["--source-root", str(root)]) == 1
    assert "did not land" in capsys.readouterr().err


def test_a_carrier_whose_literal_cannot_be_named_stops_the_build(tmp_path):
    """A literal the script cannot name is a stopped build, never a guessed escape.

    Driven by mutating the *fixture* — the f-string prefix becomes ``r``, so a backslash in the
    value would no longer be an escape — because no variable name can produce this on master.
    The prefix is the whole reading: escaping a raw literal's backslashes writes a value the
    carrier does not yield, which is the class of defect the two-layer encoding exists for.
    """
    root = _staged(tmp_path)
    target = root / CARRIERS[0]
    text = target.read_text(encoding="utf-8")
    assert 'CONFIG_TEMPLATE = f"""' in text, "the carrier's literal prefix moved; re-point this test"
    target.write_text(
        text.replace('CONFIG_TEMPLATE = f"""', 'CONFIG_TEMPLATE = r"""', 1), encoding="utf-8"
    )
    result = _run(root, VALUES)
    assert result.returncode == 1
    assert "config.py" in result.stderr
    assert "prefixed" in result.stderr


# ── the inert direction, which is what ships today ─────────────────────────────────


def test_with_no_variable_set_the_staged_tree_is_byte_identical(tmp_path):
    """The property that makes this landable: unconfigured ⇒ the artifact does not change.

    Asserted on bytes rather than on a summary line, because the claim is about the
    artifact: a script that rewrote a carrier and rewrote it back would still print
    "nothing to do", and a local build must not differ from a released one in any byte
    while no secret is configured.
    """
    root = _staged(tmp_path)
    before = {rel: (root / rel).read_bytes() for rel in CARRIERS}
    result = _run(root, {})
    assert result.returncode == 0, result.stderr
    assert "nothing to do" in result.stdout
    for rel in CARRIERS:
        assert (root / rel).read_bytes() == before[rel]


def test_the_repository_carriers_are_never_the_subject(tmp_path):
    """The staged root is the only write target — the checkout keeps the sentinel.

    This is the red line the mechanism rests on: a key that reaches the repository reaches
    its history. Driven after a configured run, on the real files.
    """
    root = _staged(tmp_path)
    assert _run(root, VALUES).returncode == 0
    assert SENTINEL_CONSTANT in (REPO_ROOT / "emrg" / "config.py").read_text(encoding="utf-8")
    assert GUI_READER in (REPO_ROOT / "emrg" / "gui" / "main.js").read_text(encoding="utf-8")


# ── the pipeline has to reach every carrier ────────────────────────────────────────


#: Every carrier of the first-run template — the census in
#: ``tests/test_config_template_carriers.py`` discovers them — is in exactly one of the two
#: tables below. A carrier that is in neither is a silent half-feature: the entry points the
#: pipeline does patch get the key, and the one it does not keeps showing a new host the
#: setup wizard, which is the whole thing the injection exists to remove.
_PIPELINE_PATCHES: dict[str, tuple[str, str]] = {
    # carrier -> (the --source-root the pipeline points the script at, who runs it)
    "emrg/config.py": ('--source-root "$DIST/source"', "packaging/build-runtime.sh"),
    "emrg/gui/main.js": ("--source-root emrg/gui", ".github/workflows/build-release.yml"),
}

_EXCLUDED_CARRIERS: dict[str, str] = {
    "install.sh": (
        "served from the repository at run time (`curl … | bash`), so no build secret can "
        "reach it, and a key written into it would be a key in git history"
    ),
}


def test_every_carrier_of_the_first_run_template_is_patched_or_excluded():
    """The wiring is asserted, not the intention: who runs the script, and at which root.

    A carrier the pipeline stops patching breaks nothing loudly — the release still builds,
    the tests still pass, and the only symptom is a host meeting the setup wizard. So the
    call itself is what is read here, in the file that makes it.
    """
    from tests.test_config_template_carriers import _discovered_carriers

    discovered = _discovered_carriers()
    known = set(_PIPELINE_PATCHES) | set(_EXCLUDED_CARRIERS)
    assert discovered == known, (
        "the carriers of the first-run template and this table disagree about "
        f"{sorted(discovered ^ known)}: each carrier is either patched by the release "
        "pipeline or excluded here with its reason"
    )
    for carrier, (root_arg, caller) in _PIPELINE_PATCHES.items():
        text = (REPO_ROOT / caller).read_text(encoding="utf-8")
        assert "bundle_default_config.py" in text, f"{caller} no longer runs the injection"
        assert root_arg in text, (
            f"{caller} no longer points the injection at {root_arg}, so {carrier} would ship "
            "the sentinel while the carriers beside it ship the bundled key"
        )
    assert all(reason.strip() for reason in _EXCLUDED_CARRIERS.values()), (
        "an exclusion without its reason is indistinguishable from an oversight"
    )

