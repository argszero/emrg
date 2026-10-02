"""A file a guard cannot decode is a question it could not answer — never a verdict.

The rule, and where it came from
--------------------------------
This family's guards answer in three codes (`0` holds, `1` violated, `2` could not
measure), and only two of them are verdicts. **A file that cannot be read is never
one of them.** Four readers in `scripts/check-*.py` were one word short of that:
they caught `OSError` and not `UnicodeDecodeError`, which is not an `OSError`, so a
file that is present and readable but not UTF-8 left `main()` as a traceback at
exit **1** — the tools' code for a *finding*.

Measured 2026-10-03 (`cyc20261003-005224`), each against a synthetic tree whose
load-bearing file was `b"\\xff\\xfe..."`:

| reader | reported as | honest answer |
|---|---|---|
| `check-node-test-count.py` | `1` — "the doc and the runner disagree" | 2 |
| `check-rant-citations.py` | `1` — "FAIL: N problem(s)" | 2 |
| `check-release-tag.py` | `1` — "the tag and the declared version disagree" | 2 |
| `check-issue-links.py` | `1` — a link fault | 2 |

A host whose editor writes UTF-16 or GBK — the encoding lesson this repo already
carries twice (`push-branch-from-api.py:15`, `check-node-test-count.py`'s `_run`
docstring) — was sent to correct numbers, citations or a tag that had never been
read. The same defect as `#1820`'s preflight, in the other direction of the same
mistake: there, a could-not-measure was read as *a refusal*.

What this file asserts, and why two legs
----------------------------------------
* **the rule, over the source** — every `read_text` / `read_bytes` in
  `scripts/check-*.py` either passes `errors=` (which is how a reader says "these
  bytes are not text, and I expect that") or sits inside a handler that catches the
  decode failure. A registry of justified exceptions exists and must not be stale:
  the point of a rule stated in one place is that drift is caught, not noticed;
* **the outcome, over every reader** — the rule's *spelling* is not what matters,
  so each of the four readers is handed a non-UTF-8 file and asserted to answer in
  its own vocabulary (its own error type, `None`, or an `unreadable` list). A guard
  could satisfy the source leg with an `except` that immediately re-raises.

The positive leg is there for the reason §1.1 gives: a check that can only fail
would satisfy the negative one, so every reader is also handed a well-formed file
and must answer with it.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
FAMILY = sorted(SCRIPTS.glob("check-*.py"))

#: Reads that may raise a decode error without a handler, with the reason. Empty but
#: for one entry, and it is checked in both directions: a name here that no longer
#: holds such a read fails too, so the table cannot become a list of lies.
JUSTIFIED: dict[str, str] = {
    "check-memory-index.py": (
        "`read_index()` documents that it raises `UnicodeDecodeError` for a file that is "
        "not UTF-8 — \"the same failure the store's own reader names rather than guesses "
        "through\" — and every caller catches it one frame up, where a lexical scan "
        "cannot see it. Measured 2026-10-03: given an undecodable index the tool prints "
        "`could not measure: ... could not be read` and exits 2, and the behavioural leg "
        "below pins that reading so this entry cannot go stale silently."
    ),
}

#: What a `errors=` argument can say instead: "these bytes are not text on purpose".
_TEXT_TOLERANT = {"replace", "ignore", "surrogateescape", "backslashreplace"}

#: Every exception whose name means "these bytes are not the text I asked for".
_DECODE_ERRORS = {"UnicodeDecodeError", "UnicodeError", "UnicodeTranslateError"}


def _ancestors(tree: ast.AST):
    """`(node, ancestors)` for every node, outermost ancestors first."""
    stack: list[ast.AST] = []

    def walk(node: ast.AST):
        yield node, tuple(stack)
        stack.append(node)
        for child in ast.iter_child_nodes(node):
            yield from walk(child)
        stack.pop()

    yield from walk(tree)


def _catches_decode(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:  # bare `except:` — catches it, and catches everything else
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    for node in types:
        name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")
        if name in _DECODE_ERRORS or name in {"Exception", "BaseException"}:
            return True
    return False


def _read_calls(path: Path) -> list[tuple[ast.Call, tuple[ast.AST, ...]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[ast.Call, tuple[ast.AST, ...]]] = []
    for node, ancestors in _ancestors(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"read_text", "read_bytes"}:
            found.append((node, ancestors))
    return found


def _protected(call: ast.Call, ancestors: tuple[ast.AST, ...]) -> bool:
    if any(
        kw.arg == "errors"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value in _TEXT_TOLERANT
        for kw in call.keywords
    ):
        return True
    return any(
        isinstance(node, ast.Try) and any(_catches_decode(h) for h in node.handlers)
        for node in ancestors
    )


def test_every_read_in_the_guard_family_survives_an_undecodable_file() -> None:
    """The rule, over the source: a decode error is caught, or `errors=` says so.

    This is the leg that stops the four measured sites (and the next one) from
    drifting back apart: the family already stated the rule in `check-doc-count.py`
    — `except (OSError, UnicodeDecodeError)`, with the rationale "a claim cannot
    live in a file this rule cannot read" — while three siblings wrote `OSError`
    alone and a fourth read its ledger the same way.
    """
    assert FAMILY, f"no guards found under {SCRIPTS}"
    unprotected: list[str] = []
    for script in FAMILY:
        for call, ancestors in _read_calls(script):
            name = script.name
            if _protected(call, ancestors):
                assert name not in JUSTIFIED, (
                    f"{name} is listed as a justified unprotected read, but line "
                    f"{call.lineno} is protected — drop the entry rather than leave "
                    f"a table that lies"
                )
                continue
            if name not in JUSTIFIED:
                unprotected.append(f"{name}:{call.lineno}")
    assert not unprotected, (
        "these reads can raise UnicodeDecodeError, which is not an OSError, so it "
        "escapes as a traceback at exit 1 — this family's code for a verdict:\n  "
        + "\n  ".join(unprotected)
        + "\n\nEither catch it (the shape `check-doc-count.py` states), read with "
        "errors=... when the bytes are deliberately not text, or register the site "
        "in JUSTIFIED with the reason."
    )


def _load(script: str):
    spec = importlib.util.spec_from_file_location(script.replace("-", "_"), SCRIPTS / script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def undecodable(tmp_path: Path) -> Path:
    path = tmp_path / "not-utf-8.txt"
    path.write_bytes(b"\xff\xfe not utf-8 \n")
    return path


@pytest.fixture
def readable(tmp_path: Path) -> Path:
    path = tmp_path / "utf-8.txt"
    path.write_text("plain text\n", encoding="utf-8")
    return path


def test_node_test_count_reads_through_its_own_error(undecodable: Path, readable: Path) -> None:
    """`_read_text` is the one home of the read inside that tool (both call sites).

    If it let the decode error through, `main()`'s `except OSError` would not see it
    either — and the tool would report a count mismatch it never measured.
    """
    module = _load("check-node-test-count.py")
    with pytest.raises(module.NodeCountError, match="cannot read"):
        module._read_text(undecodable)
    assert module._read_text(readable) == "plain text\n"


def test_rant_citations_names_the_file_it_could_not_read(
    undecodable: Path, readable: Path, tmp_path: Path
) -> None:
    """`scan_tree` answers `([], [names])` — absent and undecodable are one answer."""
    module = _load("check-rant-citations.py")
    undecodable.replace(tmp_path / "not-utf-8.md")
    readable.replace(tmp_path / "utf-8.md")
    sites, unreadable = module.scan_tree(tmp_path, ("not-utf-8.md", "utf-8.md"))
    assert unreadable == ["not-utf-8.md"], unreadable
    assert sites == [], "a file that could not be read has no sites to report"


def test_release_tag_reads_a_decodable_declaration(undecodable: Path, tmp_path: Path) -> None:
    """`declared_version` answers `None`, which every caller already reports as 2."""
    module = _load("check-release-tag.py")
    (tmp_path / "emrg").mkdir()
    (tmp_path / "emrg" / "__init__.py").write_bytes(undecodable.read_bytes())
    assert module.declared_version(tmp_path) is None

    (tmp_path / "emrg" / "__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    assert module.declared_version(tmp_path) == "1.2.3"


def test_issue_links_reports_the_ledger_it_could_not_read(undecodable: Path) -> None:
    """The ledger reader raises its own `RuntimeError`, which `main()` answers with 2.

    Its docstring already said "a store that is missing **or unparseable** is not a
    store with no rants in it"; a store that is present and not UTF-8 is the third
    shape of the same thing.
    """
    module = _load("check-issue-links.py")
    with pytest.raises(RuntimeError, match="rant ledger could not be read"):
        module.load_rant_rows(undecodable)


def test_memory_index_reports_an_undecodable_index_as_unmeasured(undecodable: Path) -> None:
    """The one exemption in `JUSTIFIED`, pinned by its behaviour rather than its prose.

    `read_index()` raises on purpose and the caller catches it; the reading a host
    sees is what has to be right, so the exemption is only as good as this leg.
    """
    module = _load("check-memory-index.py")
    assert module.main([str(undecodable)]) == 2


def test_no_guard_answers_a_verdict_about_a_file_it_could_not_read(tmp_path: Path) -> None:
    """The measured sites, end to end: exit 1 is a finding, and may not be invented.

    Each guard is run the way a cycle runs it, against a tree whose load-bearing file
    is not UTF-8. `1` is the failure mode this file exists for — it is the code that
    says "the doc and the runner disagree", "FAIL: N problem(s)", "the tag names
    another version" — so no row may answer it.

    The allowed sets are explicit rather than flattened to `2`, because two of these
    guards have a legitimate reading that is not a measurement failure:
    `check-doc-count.py` in its reporting mode may answer `0` ("no tracked file states
    the Python test count") since it deliberately skips files it cannot decode
    (`offenders()`: "a claim cannot live in a file this rule cannot read"), while its
    `--resolve-conflict` mode must read `Agent.md` to do its work at all and so has
    only `2` available.
    """
    import subprocess

    tree = tmp_path / "tree"
    (tree / "emrg" / "server").mkdir(parents=True)
    (tree / "scripts").mkdir()
    (tree / "Agent.md").write_bytes(b"\xff\xfe# not utf-8\n")
    (tree / "emrg" / "__init__.py").write_bytes(b'\xff\xfe__version__ = "1.2.3"\n')

    runs = [
        ("check-doc-count.py", [], tree, {0, 2}),
        ("check-doc-count.py --resolve-conflict", ["--resolve-conflict"], tree, {2}),
        ("check-node-test-count.py", [], tree, {2}),
        ("check-release-tag.py", ["v1.2.3", "--root", str(tree)], REPO_ROOT, {2}),
    ]
    for label, args, cwd, allowed in runs:
        script = label.split(" ", 1)[0]
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / script), *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.returncode in allowed, (
            f"{label} answered {proc.returncode} about a tree whose Agent.md is not "
            f"UTF-8; the readings it may report are {sorted(allowed)}, and 1 means a "
            f"fault it did not measure.\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )
        assert "Traceback" not in proc.stderr, f"{label}: {proc.stderr}"
