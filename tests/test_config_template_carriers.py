"""The first-run ``config.toml`` template has three carriers and one setting set.

``~/.emrg/config.toml`` is created on first run by whichever entry point the host
took, so the same template text is carried by three separate code paths:

* ``emrg/config.py::ensure_config()`` — the CLI / TUI path;
* ``emrg/gui/main.js::CONFIG_TEMPLATE`` — the **packaged** GUI, which inlines its
  copy because it cannot spawn a python there (source mode spawns
  ``ensure_config()`` instead);
* ``install.sh::generate_config_template()`` — the ``curl … | bash`` installer,
  which runs before any of the above and writes the file itself.

Nothing compared them, and ``install.sh``'s copy had already drifted: it was
missing ``vision`` at the top level and on both ``[[llm.models]]`` entries
(measured 2026-10-12 on master ``acc98a19`` — issue #2144). A host who installed
with ``install.sh`` never saw the setting, and the next key added to one carrier
would silently skip the others.

**The carriers are discovered, not listed.** Any tracked file holding both of
the template's own lines — its ``[llm]`` heading and its
``auto_compact_threshold`` default — is a copy of this template, whatever it is
called and wherever it lives, so a fourth writer copied from one of these is
caught. Neither marker is the sentinel, so a constant that moves does not stop
discovery: the reading that reports it is the placeholder test below, which fails
with the two values side by side. Two exclusions are rules, named here because
both were measured:

* **``*.md``** — a document writes nothing, and ``DEVELOPMENT.md``'s block is
  deliberately a *different* example (its second ``[[llm.models]]`` entry is
  ``gpt-4o`` with ``vision = true``, which is what that segment is there to
  illustrate).
* **``tests/``** — a test writes a config to exercise the product, never to give
  a host one. This module is the reason the rule has to be stated: it names both
  marker lines as data, so without the exclusion the discovery finds *itself* —
  and only once the file is staged, which is the trap
  ``test_the_index_derived_scans_reach_new_files`` exists to catch.

What must agree is the **settings**, not the text: each carrier keeps its own
comments and ordering (``install.sh`` addresses the host per key, the others
document the block once). ``tomllib`` is the reading — the template is TOML, so
the same parse the product performs is what is compared.

**One setting carries a rule rather than a value** (issue #2256, and #2262 after
it): ``api_key`` is :data:`emrg.config.PLACEHOLDER_API_KEY`, the sentinel meaning
"the host has not set one yet". Three carriers write it and two read it — the
GUI's ``isKeyConfigured`` decides whether to open its wizard, and
``LlmClient._headers`` refuses the request rather than spending it. The sentinel
is stated **once** in this repository, in ``emrg/config.py``; every other home is
held to it by the two tests at the end of this file, one for the writers and one
for the reader that lives in another language.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

from emrg.config import PLACEHOLDER_API_KEY

REPO_ROOT = Path(__file__).resolve().parents[1]

# The two lines every copy of this template necessarily carries: the section heading
# and the last top-level key. Together they are what makes "this file writes the
# first-run config" decidable from the file's text.
#
# Neither is the sentinel, and that is deliberate (issue #2262). A marker derived
# from `PLACEHOLDER_API_KEY` would leave this module holding no copy of the value —
# but it would also make a constant that moved *stop discovery*, so the population
# check would fail with "the carriers are the three named here" when the real
# finding is "the templates and the constant disagree". Keeping the marker on the
# template's own shape lets that second reading be the loud one, from the test
# below, while discovery stays stable. The sentinel is stated once, in
# `emrg/config.py`, and nowhere in this file.
_HEADING = "[llm]"
_DEFAULT = "auto_compact_threshold = 0.0"

# carrier path -> the pattern that lifts its template literal out of it. Each is
# anchored on `[llm]`, so a match is the template and not a stray quote. The python
# carrier's entry is `None`: its template is an f-string in `emrg/config.py`, so it
# is read by importing the rendered text — the same string `ensure_config` writes —
# rather than by a pattern over source that would have to re-derive it.
_EXTRACTORS = {
    "emrg/config.py": None,
    "emrg/gui/main.js": r"const CONFIG_TEMPLATE = `(\[llm\].*?)`;",
    "install.sh": r"<<'EMRGCONF'\n(\[llm\].*?)\nEMRGCONF",
}

_ABSENT = object()


def _tracked_files() -> list[str]:
    """Repo-relative paths of every tracked file, via `git ls-files`.

    git's own index rather than a directory walk: it skips the ignored trees
    (`.venv/`, `node_modules/`, `dist/`) and the untracked ones for free, and it
    is what "is published" means.
    """
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return out.stdout.split()


def _discovered_carriers() -> set[str]:
    found = set()
    for rel in _tracked_files():
        # A document writes nothing, and a test writes a config to exercise the
        # product rather than to give a host one — see the module docstring.
        if rel.endswith(".md") or rel.startswith("tests/"):
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _HEADING in text and _DEFAULT in text:
            found.add(rel)
    return found


def _template(rel: str) -> str:
    pattern = _EXTRACTORS[rel]
    if pattern is None:
        # The python carrier, read as the product renders it. Its *source* spelling is
        # still pinned by `test_the_carriers_write_the_placeholder_the_request_path_
        # refuses`, which compares this rendered text's `api_key` to the constant.
        from emrg.config import CONFIG_TEMPLATE

        return CONFIG_TEMPLATE
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    match = re.search(pattern, text, re.S)
    assert match is not None, (
        f"no first-run config template could be lifted out of {rel} — the pattern "
        f"{pattern!r} has gone stale, and a stale pattern is not a pass"
    )
    return match.group(1)


def _differences(left, right, prefix: str = "") -> list[str]:
    """Dotted paths where two parsed templates disagree; a missing key counts."""
    if isinstance(left, dict) and isinstance(right, dict):
        out: list[str] = []
        for key in sorted({*left, *right}):
            out += _differences(
                left.get(key, _ABSENT), right.get(key, _ABSENT),
                f"{prefix}.{key}" if prefix else key,
            )
        return out
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [f"{prefix} (length {len(left)} vs {len(right)})"]
        out = []
        for index, (one, other) in enumerate(zip(left, right)):
            out += _differences(one, other, f"{prefix}[{index}]")
        return out
    return [] if left == right else [prefix]


def test_the_carriers_are_the_three_that_are_named_here():
    """Discovery and the extractor table must agree, in both directions.

    A fourth writer copied from one of these carries both marker lines and
    appears here; a carrier that fell out of the table (renamed, reformatted,
    stopped writing the file) appears here too. Either way the next step is a
    human reading this list, not a silent drop.
    """
    assert _discovered_carriers() == set(_EXTRACTORS)


def test_every_carrier_holds_a_template_that_parses():
    """The non-empty baseline: an extraction that finds nothing is not a pass."""
    for rel in _EXTRACTORS:
        template = _template(rel)
        assert template.strip(), f"the template lifted out of {rel} is empty"
        parsed = tomllib.loads(template)
        assert parsed.get("llm"), f"{rel}'s template declares no [llm] section"


def test_the_carriers_configure_the_same_settings():
    """One template, three carriers: the settings must be identical.

    Comments and ordering are each carrier's own; what a host ends up with is
    what this reads.
    """
    settings = {rel: tomllib.loads(_template(rel)) for rel in _EXTRACTORS}
    reference, *rest = sorted(settings)
    for rel in rest:
        assert settings[rel] == settings[reference], (
            f"{rel} and {reference} are two copies of one first-run template and "
            f"must configure the same settings; they differ in "
            f"{', '.join(_differences(settings[reference], settings[rel]))}"
        )


def test_the_carriers_write_the_placeholder_the_request_path_refuses():
    """The writers of the sentinel and the reader that now honours it, held together.

    ``emrg.config.PLACEHOLDER_API_KEY`` is the value ``emrg.server.llm.LlmClient._headers``
    refuses before building a request (issue #2256). If a carrier's template spelled the
    placeholder even slightly differently, that refusal would be dead code with every test
    green: the constant would name a value no file holds, and the CLI first run would go back
    to spending a request on its own placeholder. The comparison is made from the *parsed*
    template, so it reads what a host's config file would hold rather than the file's text.
    """
    for rel in _EXTRACTORS:
        parsed = tomllib.loads(_template(rel))
        written = parsed.get("llm", {}).get("api_key")
        assert written == PLACEHOLDER_API_KEY, (
            f"{rel}'s template stores api_key={written!r}, but "
            f"emrg.config.PLACEHOLDER_API_KEY — the value `_headers` refuses — is "
            f"{PLACEHOLDER_API_KEY!r}; one of the two has to move with the other"
        )


#: The GUI's reader of the sentinel, lifted out of `main.js` by its own body. Anchored on
#: the definition and closed by a line that is exactly two spaces of indentation, which is
#: where that function ends in the file; a stale pattern fails the match assert rather than
#: passing over nothing.
_GUI_READER = re.compile(r"function isKeyConfigured\(key\)\s*\{(.*?)\n  \}", re.S)


def test_the_gui_reader_keys_on_the_same_sentinel():
    """The other reader of the sentinel, in another language, held to the same string.

    Issue #2256's table names two readers: ``emrg/gui/main.js::isKeyConfigured`` (G117,
    which opens the setup wizard) and ``LlmClient._headers`` (which refuses the request).
    Only the second was pinned when that fix landed. Renaming the GUI's comparison left this
    whole module green — measured on master `f33bbc7e` with
    ``run-mutation-arm.py --file emrg/gui/main.js --old 'key !== "sk-..."' --new
    'key !== "some-other-placeholder"'`` → **SURVIVED, 4 passed**. The drift that hides there
    is silent: move the constant and the writers' test above forces every template to follow,
    the refusal follows with the constant, and the GUI stops recognising the key the product
    writes — two halves of one rule, consistent with each other and wrong together.

    `main.js` cannot import the constant, so the comparison is read out of the source. What
    the assert reads is the **returned expression**, not the function's text: the sentinel also
    appears in that function's comment, and a guard a comment can satisfy is not a guard.
    """
    text = (REPO_ROOT / "emrg" / "gui" / "main.js").read_text(encoding="utf-8")
    match = _GUI_READER.search(text)
    assert match is not None, (
        "the body of `isKeyConfigured` could not be lifted out of emrg/gui/main.js — the "
        "pattern has gone stale, and a stale pattern is not a pass"
    )
    returned = [
        line.strip() for line in match.group(1).splitlines()
        if line.strip().startswith("return")
    ]
    assert returned, (
        "no `return` line inside isKeyConfigured — the wizard's rule cannot be read, and "
        "this is not a pass"
    )
    assert any(f'"{PLACEHOLDER_API_KEY}"' in line for line in returned), (
        "the GUI's isKeyConfigured no longer compares the key against "
        f"PLACEHOLDER_API_KEY ({PLACEHOLDER_API_KEY!r}), so the setup wizard stops opening "
        f"for the placeholder the product writes; it now returns: {returned}"
    )


# ── the release build's injection of the template (issue #2265) ────────────────
#
# A fresh install meets the setup wizard because the first-run template's `api_key`
# is the placeholder. The host asked for EMRG to work as soon as it is installed and
# supplied the values to do it with; they are a secret, so a release build takes them
# from **build secrets** and writes them into the carriers it packages
# (`packaging/inject_llm_config.py`). A build secret can reach two of the three
# carriers above; the third is fetched from the repository at install time and no
# secret is ever in scope for it (issue #2265's stated limitation).

#: carrier (repo-relative) -> the path the **built tree** carries it at, which is what
#: the workflow step patches. `emrg/config.py` is staged by `build-runtime.sh` under
#: `dist/runtime/source/` (so the checkout's own copy is never touched); the GUI's
#: `main.js` is packed by electron-builder from the project directory, so there its
#: working-tree path is the one that ships.
_RELEASE_INJECTION_TARGETS = {
    "emrg/config.py": "dist/runtime/source/emrg/config.py",
    "emrg/gui/main.js": "emrg/gui/main.js",
}

#: The carriers a release build cannot reach, named so that their absence from the map
#: above is a decision rather than an omission. `install.sh` is the `curl … | bash`
#: installer: it carries the template but fetches and runs it from the repository at
#: install time, so no build secret can patch it.
_UNREACHABLE_BY_BUILD = {"install.sh"}

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-release.yml"
INJECTOR = REPO_ROOT / "packaging" / "inject_llm_config.py"

#: the pattern that lifts a carrier's template out of its **source text** (rather than
#: the rendered text `_template` returns for the python carrier). The injected text is
#: what a fresh install would hold, so this is the reading that can compare the two.
_SOURCE_TEMPLATE = {
    "emrg/config.py": r'CONFIG_TEMPLATE = f"""(.*?)"""',
    "emrg/gui/main.js": r"const CONFIG_TEMPLATE = `(.*?)`;",
}


def _injector_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("inject_llm_config", INJECTOR)
    assert spec is not None and spec.loader is not None, f"{INJECTOR} could not be loaded"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_carrier_a_build_can_reach_has_an_injection_target():
    """The reachable carriers and the injection map are held to each other, both ways.

    A fourth carrier copied from one of these introduces a builder that ships the
    placeholder unless the map is widened; a carrier that left the map (renamed, or its
    writer deleted) is caught here too. The exclusion is checked against the census so a
    typo in ``_UNREACHABLE_BY_BUILD`` fails rather than silently shrinking the set.
    """
    discovered = _discovered_carriers()
    assert _UNREACHABLE_BY_BUILD <= discovered, (
        f"_UNREACHABLE_BY_BUILD names {sorted(_UNREACHABLE_BY_BUILD - discovered)}, which "
        f"the carrier census does not find; the exclusion has drifted from the carriers"
    )
    reachable = discovered - _UNREACHABLE_BY_BUILD
    assert reachable == set(_RELEASE_INJECTION_TARGETS), (
        f"the carriers a build secret can reach are {sorted(reachable)}, but the release "
        f"injection targets {sorted(_RELEASE_INJECTION_TARGETS)}; every reachable carrier "
        f"must be injected, or the build ships the placeholder silently"
    )


def test_the_release_workflow_injects_into_every_reachable_carrier():
    """The workflow step is read back out of the file, not assumed from the map above.

    The step must name exactly the built-tree paths of the reachable carriers, and it
    must feed the variables the injector reads (both directions: a renamed secret in the
    workflow, or a renamed variable in the script, fails here).
    """
    import yaml

    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")).get("jobs")
    assert isinstance(jobs, dict), f"{WORKFLOW} carries no `jobs` mapping"
    steps = jobs["build"]["steps"]
    steps = [s for s in steps if "inject_llm_config.py" in (s.get("run") or "")]
    assert len(steps) == 1, (
        f"expected exactly one step in build-release.yml's build job running "
        f"packaging/inject_llm_config.py, found {len(steps)}"
    )
    step = steps[0]

    match = re.search(r"packaging/inject_llm_config\.py\s+(\S.*)$", step["run"], re.M)
    assert match is not None, (
        f"the injection step names no carriers: {step['run']!r}; every reachable carrier "
        f"must be an argument or nothing is injected into it"
    )
    names = match.group(1).split()
    assert set(names) == set(_RELEASE_INJECTION_TARGETS.values()), (
        f"the injection step patches {sorted(names)}, but the reachable carriers live at "
        f"{sorted(_RELEASE_INJECTION_TARGETS.values())}; a carrier left out ships the "
        f"placeholder"
    )

    module = _injector_module()
    variables = {var for _, var in module.FIELDS}
    env = step.get("env") or {}
    assert set(env) == variables, (
        f"the step feeds {sorted(env)} but the injector reads {sorted(variables)}; a "
        f"secret the step does not pass is a value the release cannot carry"
    )
    for var in variables:
        assert f"secrets.{var}" in env[var], (
            f"the step does not take {var} from a build secret (it is {env[var]!r}); the "
            f"release build can only inject a value a secret carries"
        )


def test_the_injector_moves_only_the_top_level_llm_keys():
    """The injector against each reachable carrier's own text, in both directions.

    Injection must move the top-level ``[llm]`` keys (what makes a fresh install usable)
    and must **not** touch the ``[[llm.models]]`` entries below them -- replacing every
    ``model = "..."`` would rewrite the switch list ``/model`` and the GUI read. The
    second arm is the no-op: with no value set the text is returned byte-for-byte, which
    is the "adding the secret names before the values is safe" promise.
    """
    module = _injector_module()
    for rel, pattern in _SOURCE_TEMPLATE.items():
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        before = tomllib.loads(re.search(pattern, source, re.S).group(1))

        injected, changed, missing = module.inject(
            source,
            [("base_url", "https://example.invalid/v1"), ("api_key", "FAKE-KEY"), ("model", "fake-model")],
        )
        assert not missing, f"{rel}: the injector found nothing to patch for {missing}"
        assert set(changed) == {"base_url", "api_key", "model"}, (rel, changed)
        after = tomllib.loads(re.search(pattern, injected, re.S).group(1))
        assert after["llm"]["api_key"] == "FAKE-KEY", rel
        assert after["llm"]["base_url"] == "https://example.invalid/v1", rel
        assert after["llm"]["model"] == "fake-model", rel
        assert after["llm"]["models"] == before["llm"]["models"], (
            f"{rel}: the injection changed the [[llm.models]] entries, which are the switch "
            f"list /model and the GUI read; only the top-level keys may move"
        )

        untouched, changed_none, missing_none = module.inject(
            source, [("base_url", None), ("api_key", None), ("model", None)]
        )
        assert untouched == source and changed_none == [] and missing_none == [], (
            f"{rel}: with no value set the injector changed the text; a build without a "
            f"secret must be byte-identical to one without this step"
        )


def test_the_injector_refuses_a_carrier_that_lost_its_llm_block():
    """A carrier whose ``[llm]`` block is gone is a fault, not a silent skip.

    The whole point of the step is that the placeholder does not ship; an injector that
    reported nothing when it patched nothing would leave a release shipping the setup
    wizard while its own log said it had run.
    """
    module = _injector_module()
    _, changed, missing = module.inject("no template here\n", [("api_key", "FAKE-KEY")])
    assert changed == [] and missing == ["api_key"], (changed, missing)
