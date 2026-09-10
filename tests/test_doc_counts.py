"""Guard against the recurring README/Agent.md test-count drift.

Pattern history: #426 -> #430 -> #510 -> #511. Every time tests are added or
removed, the documented counts drift and require a follow-up doc PR. This
module asserts the documented Python count matches the real collection, and
that the documented GUI breakdown sums to its headline number.

#584: README.cn.md was the only test-count doc NOT guarded — it drifted to
91 (22 renderer smoke) while README.md/Agent.md said 96 (27 renderer smoke)
after #580 added 3 GUI tests. Both checks now cover all three docs
(README.md, README.cn.md, Agent.md); CJK full-width parens and the
"项：" separator are normalized before matching.
#692: rant 2026-08-11T19:50:37 — README.md/README.cn.md switched to the
Tests badge (no hardcoded counts); the python-count check now guards
Agent.md only, while the GUI-breakdown check still picks up any
"(N: ...)" line it finds in any doc (Agent.md keeps the breakdown).
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _collected_pytest_count() -> int:
    """Run pytest in collect-only mode and parse the total."""
    out = subprocess.check_output(
        [__import__("sys").executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=str(REPO_ROOT),
        text=True,
        stderr=subprocess.STDOUT,
    )
    m = re.search(r"(\d+) tests? collected", out)
    assert m, f"could not parse collected count from pytest output:\n{out[-2000:]}"
    return int(m.group(1))


def _gui_breakdowns() -> list[tuple[str, int, list[int]]]:
    """Extract (label, headline, parts) for every documented GUI count."""
    found = []
    for doc in ("README.md", "README.cn.md", "Agent.md"):
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "npm test" not in line:
                continue
            # CJK docs use full-width parens and "（N 项：..." instead of "(N: ..."
            line = line.replace("（", "(").replace("）", ")")
            line = re.sub(r"(\d+) 项：", r"\1: ", line)
            m = re.search(r"\((\d+): ([^)]+)\)", line)
            if not m:
                continue
            headline = int(m.group(1))
            # each breakdown part starts with its count ("22 daemon_client + ...");
            # take the first number per part (avoids false digits inside names like i18n)
            parts = [
                int(re.match(r"\s*(\d+)", part).group(1))
                for part in m.group(2).split("+")
                if re.match(r"\s*\d+", part)
            ]
            found.append((f"{doc}: {line.strip()[:70]}", headline, parts))
    return found


PYTHON_COUNT_LINE = re.compile(r"uv run pytest tests/ -v` \((\d+)\)")


def _documented_python_counts(text: str) -> list[int]:
    r"""Every documented Python count in a doc, in order.

    Returns a list rather than a single value so a caller can tell "the doc
    states this once" from "the doc states it twice". Only the ordering form
    ``uv run pytest tests/ -v` (N)`` in a shell fence counts; the prose
    no-fence form (``python -m emrg``, or a CI note mentioning a count) does
    not — measured on the real ``Agent.md``: one line matches, and a naive
    "any line with a number" rule would count three.
    """
    return [int(value) for value in PYTHON_COUNT_LINE.findall(text)]


def _single_documented_python_count(doc: str, text: str) -> int:
    """The guard proper: the one count `doc` states, refusing an ambiguous doc.

    Extracted from the test below so the ambiguous case can be *driven* (a doc
    is just text) rather than described. This line is the repo's
    most-conflicted one — #1119/#1120/#1121/#1122, then #1124/#1125/#1126 —
    and a hand-resolved conflict can leave the stale line *above* the correct
    one. A first-match guard then reads the stale number and reports the
    *opposite* of the real problem ("documents 1315 but 1338 are collected"),
    so any doc with two lines is ambiguous and must be repaired, never
    rank-ordered.
    """
    counts = _documented_python_counts(text)
    assert counts, f"no documented Python count found in {doc}"
    assert len(counts) == 1, (
        f"{doc} states a Python test count {len(counts)} times: {counts}. This "
        "line is the repo's most-conflicted line and a hand-resolved merge can "
        "leave a stale copy. Delete the stale line, then re-measure."
    )
    return counts[0]


def test_python_count_matches_docs() -> None:
    collected = _collected_pytest_count()
    # rant 2026-08-11T19:50:37: README.md/README.cn.md dropped hardcoded counts in
    # favor of the Tests badge (dynamic — no more doc drift). Agent.md keeps the
    # number (project-context file, checked by the same guard).
    doc = "Agent.md"
    text = (REPO_ROOT / doc).read_text(encoding="utf-8")
    # Agent.md: "pytest tests/ -v` (N)"
    documented = _single_documented_python_count(doc, text)
    assert documented == collected, (
        f"{doc} documents {documented} Python tests but {collected} are collected "
        f"(--collect-only). Sync the doc (and this guard) when adding/removing tests.\n"
        "Fix with: uv run --no-sync python3 scripts/check-doc-count.py --write"
    )


DUPLICATED_DOC = (
    "Python: `uv run pytest tests/ -v` (1338) — import check: `uv run python -c …`\n"
    "Python: `uv run pytest tests/ -v` (1315) — import check: `uv run python -c …`\n"
)


def test_python_count_guard_refuses_a_duplicate_line() -> None:
    """Drive the real guard against a doc that states the count twice.

    The guard itself is invoked (with a doc's text), and the assertion message
    is the guard's own — not a copy of it. A test that re-stated the message
    inline would keep passing after the guard's message changed, which is the
    exact shape #1124 was rejected for: a string a host never sees satisfying a
    test that claims to pin the host's experience.
    """
    with pytest.raises(AssertionError, match=r"states a Python test count 2 times"):
        _single_documented_python_count("Agent.md", DUPLICATED_DOC)


def test_python_count_guard_still_sees_a_single_line() -> None:
    """The negative half: one line must keep working (no fail-loud on the norm).

    Measured 2026-09-10 on `Agent.md`: exactly one line matches, and it is the
    fence form — the prose `python -m emrg` line and the CI note mentioning a
    count do not, so a naive "any line with a number" rule would count 3.
    """
    assert _single_documented_python_count("Agent.md", DUPLICATED_DOC.splitlines()[0] + "\n") == 1338
    assert _documented_python_counts("no count line here\n") == []


def test_gui_breakdown_sums_to_headline() -> None:
    breakdowns = _gui_breakdowns()
    assert breakdowns, "no GUI test breakdowns found in README.md/Agent.md"
    for label, headline, parts in breakdowns:
        assert sum(parts) == headline, (
            f"{label}: breakdown {parts} sums to {sum(parts)} but headline says {headline}"
        )


# The canonical test-command lines in Agent.md. Each is a *kind* that may appear
# at most once: this line is the repo's most-conflicted (four merges in one day,
# #1119/#1120/#1121/#1122), and a hand-resolved conflict can leave a second copy.
COUNT_LINE_KINDS = ("Python: ", "GUI: ", "Renderer: ")


def _duplicated_count_line_kinds(text: str) -> dict[str, list[str]]:
    """Count-line kinds stated more than once in a doc, with the offending lines.

    Takes the doc's text (not a path) so the rule below can be *driven* against
    a two-line document. Keyed by kind rather than by whole line, because the
    realistic conflict artifact is **not** an exact copy: the two sides carry
    different numbers (e.g. `(100: ` and `(97: `), so an exact-line comparison
    sees nothing. That is why the pre-existing
    `test_no_duplicate_npm_test_command_lines` (#617, a *copy-paste* guard) does
    not cover this state.
    """
    found: dict[str, list[str]] = {}
    for kind in COUNT_LINE_KINDS:
        lines = [ln.rstrip() for ln in text.splitlines() if ln.startswith(kind)]
        if len(lines) > 1:
            found[kind] = lines
    return found


def test_count_line_kinds_appear_once_per_doc() -> None:
    """Each doc states each test-count line at most once, even with different numbers.

    Measured 2026-09-10: this is the state every guard missed. A duplicated
    `GUI: \\`cd emrg/gui && npm test\\` (97: ...)` line placed beside the correct
    `(100: ...)` line left **all 9 guards green** once the stale copy was made
    internally consistent - `test_no_duplicate_npm_test_command_lines` compares
    whole lines (so different numbers are invisible), and
    `test_gui_breakdown_sums_to_headline` validates each line against *itself*
    (so a stale line that sums correctly is accepted). The result is a doc that
    claims two different GUI test counts, with nothing flagging the ambiguity.

    Note the distinction from the exact-duplicate guard: that one catches
    copy-paste, this one catches *ambiguity*. Both are needed - an exact
    duplicate is caught by either, a numbered stale copy only by this one.
    """
    for doc in ("README.md", "README.cn.md", "Agent.md"):
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        found = _duplicated_count_line_kinds(text)
        assert not found, (
            f"{doc} states the same test-count line kind more than once, so the "
            "doc claims two different values with no way to tell which is real: "
            + "; ".join(
                f"{kind.strip()} stated {len(lines)}x -> {lines}"
                for kind, lines in sorted(found.items())
            )
            + ". A hand-resolved merge left a stale copy - delete it, then "
            "re-measure with `uv run --no-sync python3 scripts/check-doc-count.py --write`."
        )


def test_count_line_kind_guard_catches_a_numbered_duplicate() -> None:
    """Drive the kind guard against the shape the other two guards miss.

    Two `GUI:` lines whose numbers differ: not an exact duplicate (the #617
    guard's criterion), and each self-consistent (the sum guard's criterion),
    yet the doc now contradicts itself. The rule is invoked with the two-line
    text, so what is exercised is the guard's own logic rather than a
    restatement of its assertion message.
    """
    real = [
        ln for ln in (REPO_ROOT / "Agent.md").read_text(encoding="utf-8").splitlines()
        if ln.startswith("GUI: ")
    ]
    assert len(real) == 1, f"Agent.md must state one GUI count line, has {len(real)}"
    stale = (
        real[0].replace("(100: ", "(97: ", 1).replace("44 daemon_client", "41 daemon_client", 1)
    )
    assert stale != real[0], "the stale copy must differ from the real line"

    # negative half: the real doc is clean, and one line is not a duplicate
    assert _duplicated_count_line_kinds(
        (REPO_ROOT / "Agent.md").read_text(encoding="utf-8")
    ) == {}
    assert _duplicated_count_line_kinds(real[0] + "\n") == {}

    # positive half: two GUI lines, different numbers, both self-consistent
    found = _duplicated_count_line_kinds("\n".join([real[0], stale]) + "\n")
    assert set(found) == {"GUI: "}, found
    assert found["GUI: "] == [real[0], stale]


def test_no_duplicate_npm_test_command_lines() -> None:
    """Each doc must not contain an identical `npm test` command line twice.

    #617: README.cn.md carried the `npm test` line twice (exact copy-paste
    duplicate). The breakdown-sum guard above passed because it validates
    each line independently — both copies sum correctly to the same
    headline. Exact-line duplicates within one doc are always a copy-paste
    bug: README.md has one line, README.cn.md must have one, Agent.md has
    two *different* lines (features + test-commands sections), never an
    identical repeat.
    """
    for doc in ("README.md", "README.cn.md", "Agent.md"):
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        lines = [ln.rstrip() for ln in text.splitlines() if "npm test" in ln]
        dupes = {ln for ln in lines if lines.count(ln) > 1}
        assert not dupes, (
            f"{doc} contains duplicated npm-test command line(s) — remove the "
            f"copy-paste duplicate: {dupes}"
        )
    # rant 2026-08-11T17:58:11 (README emoji 泛滥 → 克制化)：节标题必须纯文本，
    # 无 emoji（保留的 emoji 仅限对比表 ✅/❌、底部 ❤️、语言切换 🇬🇧/🇨🇳）。
    emoji = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")
    for doc in ("README.md", "README.cn.md"):
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        bad = [
            ln for ln in text.splitlines()
            if re.match(r"^#{1,3} ", ln) and emoji.search(ln)
        ]
        assert not bad, f"{doc}: heading(s) must not contain emoji: {bad}"


def test_evolution_prompt_no_quick_ref_block() -> None:
    """evolution_prompt.md must not contain the implemented-features quick
    reference block (rant 2026-08-17T14:22:21 — removed in #822).

    pm25coder follow-up on #822: cheap insurance against accidental
    re-insertion of the static in-prompt history table. The memory system
    + git log handle dedup instead.
    """
    prompt = (REPO_ROOT / "emrg" / "server" / "evolution_prompt.md").read_text(
        encoding="utf-8"
    )
    # Case-insensitive match (pm25coder note): the original block's header was
    # capitalized ("Implemented-features quick reference") — the lowercase
    # body phrase is the guaranteed substring, but lowercasing the whole file
    # catches a header-only re-insertion too.
    assert "implemented-features quick reference" not in prompt.lower(), (
        "evolution_prompt.md must not contain the implemented-features quick "
        "reference block — dedup is handled by the memory system + git log "
        "(rant 2026-08-17T14:22:21, #822)"
    )



_RENDERER_TEST_SUFFIX = ".test"


def _static_renderer_counts() -> dict[str, int]:
    """Count renderer vitest cases per file, keyed by Agent.md's own label.

    Matches vitest's executed total exactly: for every renderer test file the
    ``^\\s*(it|test)(`` definition count equals the number of executed cases
    (verified for all 45 files, R2254). Files are under
    ``emrg/gui/renderer/src`` with ``.test.ts`` / ``.test.tsx`` suffixes, and
    Agent.md labels each one by its stem minus ``.test`` (``App.test.tsx`` ->
    ``App``, ``snapshot-store.test.ts`` -> ``snapshot-store``); a trailing
    descriptive word (``2 App smoke``) is not part of the label.
    """
    base = REPO_ROOT / "emrg" / "gui" / "renderer" / "src"
    files = sorted(base.rglob("*.test.ts")) + sorted(base.rglob("*.test.tsx"))
    assert files, "no renderer test files found under emrg/gui/renderer/src"
    counts: dict[str, int] = {}
    for f in files:
        label = f.stem[: -len(_RENDERER_TEST_SUFFIX)]
        # One label per file is an implicit requirement of Agent.md's breakdown
        # format, and `rglob` spans subdirectories, so two files may share a
        # stem. Without this assertion the dict keeps only the later file and
        # the earlier one's definitions vanish from the total - measured: a
        # second `utils` file (9 definitions) left the helper reporting 514
        # while the tree really had 523, and BOTH renderer guards stayed green
        # (the pre-refactor sum-over-every-file version caught it). Turn the
        # collision red instead of dropping definitions.
        assert label not in counts, (
            f"two renderer test files share the label {label!r} "
            f"({counts[label]} already counted); Agent.md's per-file breakdown "
            "cannot distinguish them and one file's definitions would be "
            "silently dropped from the total. Rename one file."
        )
        counts[label] = len(
            re.findall(r"^\s*(?:it|test)\(", f.read_text(encoding="utf-8"), re.M)
        )
    return counts


def _static_renderer_count() -> int:
    """Total renderer vitest cases (static, no node_modules needed)."""
    return sum(_static_renderer_counts().values())


def test_renderer_count_matches_docs() -> None:
    """Agent.md's Renderer headline must equal the real vitest count.

    R2254 (#1049/#1050): renderer tests grew 445 -> 448 without Agent.md being
    bumped. The GUI-breakdown guard only validates each "(N: ...)" line's
    *internal* sum (parts sum to headline) — it cannot see reality, and the
    pytest CI job has no node_modules to run vitest. The static definition
    count equals vitest's executed total, so this guard runs everywhere
    pytest does and turns the drift red immediately.
    """
    renderer = [b for b in _gui_breakdowns() if "Renderer" in b[0]]
    assert renderer, "Agent.md must document the Renderer test breakdown"
    label, headline, parts = renderer[0]
    static = _static_renderer_count()
    assert headline == static, (
        f"{label}: documents {headline} renderer tests but {static} are "
        f"counted statically (vitest-equivalent). Sync Agent.md when "
        "adding/removing renderer tests."
    )


def _renderer_doc_breakdown() -> tuple[int, dict[str, int]]:
    """Parse Agent.md's Renderer line into (headline, {label: count})."""
    text = (REPO_ROOT / "Agent.md").read_text(encoding="utf-8")
    line = next((ln for ln in text.splitlines() if "Renderer:" in ln), None)
    assert line, "Agent.md must document the Renderer suite (a 'Renderer:' line)"
    m = re.search(r"\((\d+): ([^)]+)\)", line)
    assert m, f"could not parse the Agent.md Renderer breakdown: {line}"
    parts: dict[str, int] = {}
    for part in m.group(2).split("+"):
        pm = re.match(r"\s*(\d+)\s+(\S+)", part)
        assert pm, f"could not parse Agent.md Renderer breakdown part: {part!r}"
        parts[pm.group(2)] = int(pm.group(1))
    return int(m.group(1)), parts


def test_renderer_breakdown_matches_static_counts() -> None:
    """Agent.md's per-file renderer counts must match the real definitions.

    The R2254 guard pins the renderer *headline* to the static total, and
    ``test_gui_breakdown_sums_to_headline`` pins the parts to the headline.
    Together they constrain the sum but not the parts: two compensating edits
    on the 45-entry breakdown (``13 markdown + 4 vendorMarkdown`` mistyped as
    ``14 markdown + 3 vendorMarkdown``) satisfy both and leave the doc wrong
    in two places. #1117 closed the same gap for the GUI line per file; this
    closes it for the renderer, so a new/renamed/deleted renderer test file or
    a transposed count turns red in the pytest job (no node_modules needed).
    """
    headline, documented = _renderer_doc_breakdown()
    static = _static_renderer_counts()

    assert set(documented) == set(static), (
        "Agent.md Renderer breakdown does not match the renderer test files — "
        f"undocumented files: {sorted(set(static) - set(documented))}; "
        f"stale labels: {sorted(set(documented) - set(static))}"
    )
    for label, count in sorted(static.items()):
        assert documented[label] == count, (
            f"Agent.md documents {documented[label]} {label} renderer tests "
            f"but {count} are defined in the file labelled {label} — sync the "
            "doc when adding/removing renderer tests."
        )
    total = sum(static.values())
    assert headline == total, (
        f"Agent.md's Renderer headline is {headline} but the test files define "
        f"{total} tests"
    )


_GUI_DOC_MARKER = "`cd emrg/gui && npm test`"
_GUI_TEST_SUFFIX = ".test.js"


def _gui_doc_breakdown() -> tuple[int, dict[str, int]]:
    """Parse Agent.md's GUI line into (headline, {test-file stem: count})."""
    text = (REPO_ROOT / "Agent.md").read_text(encoding="utf-8")
    line = next((ln for ln in text.splitlines() if _GUI_DOC_MARKER in ln), None)
    assert line, f"Agent.md must document the GUI suite as {_GUI_DOC_MARKER}"
    m = re.search(r"\((\d+): ([^)]+)\)", line)
    assert m, f"could not parse the Agent.md GUI breakdown: {line}"
    parts: dict[str, int] = {}
    for part in m.group(2).split("+"):
        pm = re.match(r"\s*(\d+)\s+(\S+)", part)
        assert pm, f"could not parse Agent.md GUI breakdown part: {part!r}"
        parts[pm.group(2)] = int(pm.group(1))
    return int(m.group(1)), parts


def _static_gui_counts() -> dict[str, int]:
    """Count node --test cases per GUI test file (keyed by file stem).

    ``npm test`` runs ``node --test "test/*.test.js"``; node reports one
    entry per ``test(``/``it(`` definition, so the definition count is the
    executed total. The one exception is integration.test.js's conditional
    module-level ``skip(<reason>)`` entry (#906), which is registered only
    when a live daemon owns the fixed port or EMRG_SKIP_INTEGRATION=1 — it
    is a runtime *reason* entry, not a test definition, so it is excluded
    here (the doc's 100 counts definitions; CI's ``EMRG_SKIP_INTEGRATION=1
    npm test`` prints 101 including that skip entry).
    """
    base = REPO_ROOT / "emrg" / "gui" / "test"
    files = sorted(base.glob(f"*{_GUI_TEST_SUFFIX}"))
    assert files, "no GUI test files found under emrg/gui/test"
    return {
        f.name[: -len(_GUI_TEST_SUFFIX)]: len(
            re.findall(r"^\s*(?:it|test)\(", f.read_text(encoding="utf-8"), re.M)
        )
        for f in files
    }


def test_gui_breakdown_matches_static_counts() -> None:
    """Agent.md's per-file GUI counts must match the real definitions.

    R2254 gave the *renderer* headline a static guard because it drifted
    (445 -> 448) without the doc being bumped; the GUI line still had only
    the #584 sum check, which validates the doc against itself. #906 flagged
    the gap explicitly when it hand-synced 260 -> 254 after #896-#905 drifted
    ("the doc-count guard only checks breakdown-sum consistency, not actual
    collection"). This guard closes it per file, so a new/renamed GUI test
    file or a stale label turns red in the pytest job (no node_modules
    needed) instead of waiting for a human to notice.
    """
    headline, documented = _gui_doc_breakdown()
    static = _static_gui_counts()

    assert set(documented) == set(static), (
        "Agent.md GUI breakdown does not match emrg/gui/test/*.test.js — "
        f"undocumented files: {sorted(set(static) - set(documented))}; "
        f"stale labels: {sorted(set(documented) - set(static))}"
    )
    for label, count in sorted(static.items()):
        assert documented[label] == count, (
            f"Agent.md documents {documented[label]} {label} GUI tests but "
            f"{count} are defined in emrg/gui/test/{label}{_GUI_TEST_SUFFIX} "
            "— sync the doc when adding/removing GUI tests."
        )
    total = sum(static.values())
    assert headline == total, (
        f"Agent.md's GUI headline is {headline} but the test files define "
        f"{total} tests"
    )


# --- self-tests: the guard's own machinery -----------------------------------
#
# The renderer/GUI guards above are the only thing standing between the
# Agent.md breakdown and silent drift, so the one behaviour they depend on but
# cannot observe in today's tree - two test files sharing a label - is pinned
# here. Measured before the assertion existed: a second `utils` file left the
# helper reporting 514 against a real 523, and both renderer guards stayed green.


def _loaded_guard_module():
    """Load this module by path, the repo's pattern for importing a test file.

    `monkeypatch.setattr("test_doc_counts.REPO_ROOT", ...)` does not resolve -
    pytest imports these files as `tests.test_doc_counts`, and a name that only
    exists as a plain top-level module raises at patch time.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_guard_under_test", Path(__file__))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _renderer_tree(root: Path, files: dict[str, str]) -> None:
    """Build a minimal renderer source tree under a fake REPO_ROOT."""
    base = root / "emrg" / "gui" / "renderer" / "src"
    for rel, body in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def test_renderer_counts_fail_loud_on_a_label_collision(tmp_path, monkeypatch) -> None:
    mod = _loaded_guard_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    body = "it('a', () => {});\nit('b', () => {});\n"
    _renderer_tree(tmp_path, {"lib/utils.test.ts": body, "components/utils.test.tsx": body})
    with pytest.raises(AssertionError, match="share the label 'utils'"):
        mod._static_renderer_counts()


def test_renderer_counts_accept_distinct_labels(tmp_path, monkeypatch) -> None:
    """The positive half: unique labels still count normally."""
    mod = _loaded_guard_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    body = "it('a', () => {});\nit('b', () => {});\n"
    _renderer_tree(tmp_path, {"lib/utils.test.ts": body, "components/other.test.tsx": body})
    counts = mod._static_renderer_counts()
    assert counts == {"utils": 2, "other": 2}
