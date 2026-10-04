"""A test does not assert the name it just substituted.

Why this file exists
--------------------
`tests/test_abort_runs.py::test_the_default_path_is_under_the_daemons_log_directory` is
the row that says where the abort-run state lives. It installed a lambda for
`default_path` and then asserted that lambda's arithmetic back out:

    monkeypatch.setattr(mod, "default_path",
                        lambda: mod.config_dir() / "logs" / mod.STATE_FILENAME)
    assert mod.default_path() == tmp_path / "home" / "logs" / "abort-runs.json"

Two independent reasons it could not fail: the assertion restates the line above it, and
`tests/conftest.py`'s autouse redirect had already replaced the module attribute, so
`mod.default_path` was the redirect in every test of that suite anyway. Measured
2026-10-04 (issue #1844, PR #1845): moving the state off the daemon's log directory
(`config_dir() / "state" / STATE_FILENAME`) left the row **SURVIVED** — the path the test
exists to pin was unguarded and nothing went red.

The issue that reported it declined to mechanise the class, on the grounds that "a
mechanised rule for this class would have to tell 'a test whose subject *is* the
redirect' from 'a test that mistook the redirect for the resolution', and both have the
same shape". **They do not have the same shape, and this file is the measurement.** The
legitimate test (`test_the_test_suite_is_not_pointed_at_the_hosts_state`) installs no
lambda for the name it asserts — the conftest fixture does that, globally — so the
narrow rule below reports one of the two and not the other, which is the discrimination
the issue judged impossible. The rule needs no allow-list and no exemption: scanned over
every test module in this tree it has **exactly one** instance, the defect above, and
that instance is on its way out with PR #1845.

What is asserted, and the legs that keep it from being decoration
----------------------------------------------------------------
* the **tree** substitutes no name that one of its own assertions then calls (the
  enforcement);
* a **synthetic source built to carry the exact shape is reported**, so a checker that
  matched nothing cannot pass this file;
* the **legitimate shape is not reported** — the same assertion read through a module the
  fixture patched, with no substitution of its own — which is the half that makes this a
  rule rather than a blanket ban on `monkeypatch.setattr`;
* the scan is asserted to have **read test files at all**, so a glob that stopped
  matching cannot report a clean suite.

A note on the name that comes back
----------------------------------
The finding names the substituted name and the line, not the test: a reader who has to
open the file anyway is better served by "which name" than by a restatement of the
function they are standing in. The failure message spells the fix as well — read the
resolution through a name the fixture cannot reach (this suite's by-value import, the
pattern `tests/test_config.py::test_config_path` records) — because "do not assert your
own substitute" is only half an instruction without the alternative.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def _asserted_names(node: ast.AST) -> set[str]:
    """Every name an assertion *calls*: `f()` and `mod.f()` both yield `f`."""
    found: set[str] = set()
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        func = inner.func
        if isinstance(func, ast.Name):
            found.add(func.id)
        elif isinstance(func, ast.Attribute):
            found.add(func.attr)
    return found


def _substituted(node: ast.AST) -> dict[str, int]:
    """`name -> line` for every `setattr(..., "name", <callable>)` in this body.

    Only a **callable** counts. `setattr(mod, "STATE_FILENAME", "x.json")` replaces a
    value, and an assertion about that value is a test of the substitute working, not a
    hollow one — the defect is specifically that the *code under test* was replaced by
    the test's own anonymous function.
    """
    replaced: dict[str, int] = {}
    for inner in ast.walk(node):
        if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)):
            continue
        if inner.func.attr != "setattr" or len(inner.args) < 3:
            continue
        name, value = inner.args[1], inner.args[2]
        if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
            continue
        if isinstance(value, (ast.Lambda, ast.Name)):
            replaced[name.value] = inner.lineno
    return replaced


def offenders(source: str, filename: str) -> list[str]:
    """One finding per assertion that calls a name its own test substituted."""
    tree = ast.parse(source, filename=filename)
    findings: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        replaced = _substituted(fn)
        if not replaced:
            continue
        for assertion in (n for n in ast.walk(fn) if isinstance(n, ast.Assert)):
            for name in sorted(_asserted_names(assertion.test) & set(replaced)):
                findings.append(
                    f"{filename}:{assertion.lineno} in {fn.name}(): asserts `{name}()`, "
                    f"which line {replaced[name]} replaced with a lambda the test itself "
                    f"installed - the assertion restates its own fixture"
                )
    return findings


def _test_modules() -> list[Path]:
    return sorted(TESTS_DIR.rglob("test_*.py"))


# --- the enforcement --------------------------------------------------------


def test_no_test_asserts_a_name_it_substituted() -> None:
    """The rule, over every test module in this tree."""
    modules = _test_modules()
    findings: list[str] = []
    for path in modules:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover - the tree is readable in CI
            findings.append(f"{path}: could not be read ({exc})")
            continue
        findings.extend(offenders(source, str(path.relative_to(TESTS_DIR.parent))))
    assert not findings, (
        "a test asserts the very name it substituted, so it measures its own fixture "
        "and cannot fail:\n  " + "\n  ".join(findings) + "\n\nRead the code under test "
        "through a name the fixture cannot reach (this suite's by-value import - the "
        "pattern tests/test_config.py::test_config_path records), and keep the "
        "substitution for the *inputs* the real function resolves."
    )


def test_the_scan_reads_test_modules_at_all() -> None:
    """A glob that stopped matching would report a perfectly clean suite.

    The same leg `tests/test_skip_semantics.py` and its siblings carry: an enforcement
    whose subject it never found is not an enforcement.
    """
    modules = _test_modules()
    assert len(modules) > 100, f"only {len(modules)} test module(s) found - the scan reads {TESTS_DIR}"
    assert any(p.name == "test_abort_runs.py" for p in modules), sorted(
        p.name for p in modules
    )[:10]


# --- the leg that keeps the rule from being decoration ----------------------


def test_a_source_that_asserts_its_own_substitute_is_reported() -> None:
    """The measured defect, verbatim — a checker that matches nothing cannot pass."""
    source = (
        "def test_the_default_path_is_under_the_daemons_log_directory(tmp_path, monkeypatch):\n"
        '    monkeypatch.setattr(mod, "config_dir", lambda: tmp_path / "home")\n'
        '    monkeypatch.setattr(mod, "default_path",\n'
        '                        lambda: mod.config_dir() / "logs" / mod.STATE_FILENAME)\n'
        "\n"
        '    assert mod.default_path() == tmp_path / "home" / "logs" / "abort-runs.json"\n'
    )
    found = offenders(source, "synthetic.py")
    assert len(found) == 1, found
    assert "default_path" in found[0]
    assert "config_dir" not in found[0], (
        "`config_dir` is an *input* the real resolution resolves, so patching it and "
        f"asserting the resolution is the correct shape: {found[0]}"
    )


def test_the_legitimate_redirect_shape_is_not_reported() -> None:
    """The other half, and the reason the two shapes are distinguishable.

    This is the sibling test as it stands in the tree: it reads the resolution through
    the module attribute (which the conftest fixture replaced) and asserts it, having
    substituted **nothing**. If this were reported, the rule would be a blanket ban on
    reading a module attribute at all, and the sibling that exists precisely to assert
    the redirect is in force could not be written.
    """
    source = (
        "def test_the_test_suite_is_not_pointed_at_the_hosts_state(tmp_path):\n"
        "    assert mod.default_path() == tmp_path / \"abort-runs.json\"\n"
    )
    assert offenders(source, "synthetic.py") == []


def test_the_fixed_shape_is_not_reported() -> None:
    """The shape PR #1845 lands: the input patched, the resolution read by value.

    Pinned here rather than only in `test_abort_runs.py` so this file records what the
    rule *asks for*, not only what it forbids — a remediation the suite never exercises
    is a remediation nobody has run.
    """
    source = (
        "from emrg.server.abort_runs import default_path\n"
        "\n"
        "def test_the_default_path_is_under_the_daemons_log_directory(tmp_path, monkeypatch):\n"
        '    monkeypatch.setattr(mod, "config_dir", lambda: tmp_path / "home")\n'
        "\n"
        '    assert default_path() == tmp_path / "home" / "logs" / "abort-runs.json"\n'
    )
    assert offenders(source, "synthetic.py") == []


def test_a_recorder_substituted_for_a_collaborator_is_not_reported() -> None:
    """Patching a collaborator with a recorder and asserting on the recorder is fine.

    The widespread, correct idiom: the substitute is installed so the test can *observe*
    it, and the assertion reads the recorder rather than the name. Flagging this would
    have made the rule unusable, which is why it is measured rather than assumed.
    """
    source = (
        "def test_the_writer_is_called_once(tmp_path, monkeypatch):\n"
        "    seen = []\n"
        '    monkeypatch.setattr(mod, "write_bytes", lambda data, path: seen.append(path))\n'
        "    mod.save(tmp_path)\n"
        "\n"
        "    assert len(seen) == 1\n"
    )
    assert offenders(source, "synthetic.py") == []
