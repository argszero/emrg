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

# The definition regex below sees only `it(` / `test(` at the start of a line.
# vitest and `node --test` also register *chained* forms whose executed-case
# count this regex cannot reproduce, because the `(` follows a modifier instead
# of the keyword: `it.each([...])("name", ...)` runs one case per row/table, and
# `test.skip/only/todo/fails/concurrent/skipIf/...(...)` register a case without
# ever matching `test(`. A file using one of them would be under-counted while
# both guards stayed green - the same silent-drift shape as the label collision
# fixed in #1120 and the 445 -> 448 renderer drift before it. Today's tree has
# none of these spellings (measured 2026-09-10: 0 matches across the 54 renderer
# and GUI test files), so this is a tripwire, not a filter.
_DEFINITION_KEYWORD = r"(?:it|test)"

# The keyword and its call can be separated by a *newline*, and the call is still
# one call the runner registers. Measured with the real runner (cyc20260910-234907):
# a file containing
#     it
#       ('a', () => {})
# reported `Tests 2 passed (2)` while the static count said **1** - the drift this
# guard exists to catch, arriving through line position rather than spelling.
# Prettier rejoins the statement when it reformats a file, but nothing in this repo
# runs prettier (no config, no CI step), so a hand-authored file carries it.
#
# The separator admits a newline but deliberately **not** a bare space: `it (` is
# legal JS, yet on a line of its own it is indistinguishable from prose such as
# "it (the count) is 514", and this file is written in prose about these exact
# spellings. A guard that reds on a sentence is a guard that gets deleted.
_NEWLINE_THEN_INDENT = r"(?:\n\s*)?"
_DEFINITION_FORM = re.compile(
    rf"^\s*{_DEFINITION_KEYWORD}{_NEWLINE_THEN_INDENT}\(", re.M
)

# A definition form is not always *called* - it can be *tagged*. `it.each` is also
# a tag function in vitest, so the table syntax
#     it.each`
#       a    | b
#       ${1} | ${2}
#     `('adds $a to $b', ({ a, b }) => { ... })
# registers one case **per table row** while ending in a backtick rather than a
# paren. Measured with the real runner (cyc20260910-001002): that file reported
# `Tests 2 passed (2)` while the counted pattern and both tripwires returned **0** -
# two executed cases recorded as zero, with every guard green. `it.skip.each` and
# `it.only.each` behave the same (measured: `1 skipped` / `1 passed`).
#
# So the tripwires accept either terminal. The *counted* pattern (`_DEFINITION_FORM`
# above) deliberately still requires `(`: widening it would change the count itself,
# and the tree currently has no tagged form (measured: 0 across all 54 tracked
# `.test.ts`/`.test.tsx`/`.test.js` files), so the honest move is to be *told* to
# teach the counter if one ever appears - which is exactly what these tripwires say.
_TRIPWIRE_TERMINAL = r"[(`]"

# The *suite* a definition can hang off. `it`/`test` are the counted keywords;
# `describe` is not counted at all, but both runners let it own parameterised
# definitions that still register cases the plain regex never sees:
#   vitest    - `describe.each([...])("name", () => { ... })` runs the whole
#               callback once per row, so every `it(` inside it is executed N
#               times while the static count records them once.
#   node:test - `describe.for(rows)("name", ...)` is the parameterised suite form
#               (`test.each` is undefined in node:test).
# A second regex rather than a wider first one: the counted count must stay
# `^it(|^test(` only, or the count itself changes.
#
# Same nesting rule as the chained pattern below, for the same measured reason:
# `@vitest/runner`'s `ChainableSuiteAPI` (tasks.d-*.d.ts:1204) is
# `TypedChainableFunction<ChainableSuiteContextMap, ..., { each, for }>`, so
# `describe.skip.each([...])` is valid and registers its rows. Verified
# (cyc20260910-232400) with the real runner: `describe.skip.each([[1],[2]])`
# reported `Tests 2 skipped (2)` while the single-link pattern found 0.
_SUITE_KEYWORD = r"(?:describe)"
_SUITE_PARAMETERISED_FORM = re.compile(
    rf"^\s*{_SUITE_KEYWORD}{_NEWLINE_THEN_INDENT}\."
    rf"(?:[A-Za-z_$][\w$]*{_NEWLINE_THEN_INDENT}\.)*"
    rf"(?:each|for){_NEWLINE_THEN_INDENT}{_TRIPWIRE_TERMINAL}",
    re.M,
)

# Every *callable* member the two runners put on the definition function, none of
# which the regex above can match (it requires `(` right after the keyword), and
# every one of which registers a case the runner still executes:
#   vitest  - `each`/`for` (one case per row/table) plus the option setters
#             `skip`/`only`/`todo`/`fails`/`concurrent`/`sequential`/`skipIf`/`runIf`
#             (ChainableTestContextMap + TestForFunction in @vitest/runner).
#   node:test - `skip`/`todo`/`only` (`test.each` is undefined there).
# Derived from the counted keyword so the two cannot drift apart.
#
# Anchored to line start (`^\s*`, as the counted regex and MODULE_SKIP_ENTRY
# are) rather than `\b`: called anywhere in the line, this pattern is *text*,
# so a comment or a string literal merely mentioning `it.each(` would trip it.
# The guard's job is to stop drift, not to police prose about drift - prose is
# what this file is full of. A real call is the statement on its own line.
#
# The chain is not one link deep. `@vitest/runner`'s `ChainableTestAPI`
# (tasks.d-*.d.ts:721) is `TypedChainableFunction<ChainableTestContextMap,
# ..., { each, for }>` - the *same* object exposes the option setters *and*
# `each`/`for`, so `it.skip.each([...])` and `it.concurrent.each([...])` are
# valid and were silently invisible to the single-link version of this pattern
# (measured cyc20260910-232400: `npx vitest run` on a probe file reported
# `Tests 3 passed | 2 skipped (5)` for `it.concurrent.each` + `test.skip.each`
# while both regexes reported 0 hits). Intermediate links are therefore any
# dotted member, and only the *terminal* one is required to be a known
# collectable - a real call the first pattern cannot count, one or more
# modifiers deep.
#
# Stated boundary: the anchor means an *indirect* construction such as
# `const run = it.each(cases);` is still not seen. That is a deliberate trade
# for prose immunity, not an oversight - a guard that reds on a comment gets
# deleted, and this file is written in prose about these exact spellings.
_CHAINED_DEFINITION_FORM = re.compile(
    rf"^\s*{_DEFINITION_KEYWORD}{_NEWLINE_THEN_INDENT}\."
    rf"(?:[A-Za-z_$][\w$]*{_NEWLINE_THEN_INDENT}\.)*"
    rf"(?:each|for|skip|only|todo|fails|concurrent|sequential|skipIf|runIf)"
    rf"{_NEWLINE_THEN_INDENT}{_TRIPWIRE_TERMINAL}",
    re.M,
)


# A definition can also be *nested* inside another one, and node:test's nested
# forms are reached without ever putting `it(`/`test(` at the start of a line -
# so the counted pattern and both tripwires above are blind to them. Measured
# with the real runner (cyc20260911-001002) on a file whose single visible
# `test('outer')` body holds two more cases:
#
#     test('outer', async (t) => { await t.test('inner', ...); });   -> tests 3
#     test('outer', async () => { await test('inner', ...); });      -> tests 3
#
# In both files `_DEFINITION_FORM` and both tripwires above returned 0 for the
# nested calls, so the static count said **1** while the runner executed **3**:
# two executed cases recorded as zero with every guard green. Same silent drift
# as the tagged-template form, arriving through *nesting* rather than spelling.
#
# Two shapes, both line-anchored:
#   1. a receiver member chain - `t.test(`, `ctx.test(`, `sub.it(`. `t` is only
#      the conventional name (node:test's examples use it); the spec reserves
#      nothing, so pinning `t` would pin one spelling of an open set and any
#      dotted member chain is the honest shape.
#   2. an `await`-prefixed bare keyword - `await test(`, `await it(`. The
#      counted pattern requires the keyword at the start of the line, so the
#      `await` moves it out of reach, and the call still registers.
#
# Prose immunity, measured the same cycle: **0** line-anchored hits for either
# shape across all 54 renderer/GUI test files. The tree is full of *method*
# calls like `expect(FENCE_END_RE.test("```"))` (markdown.test.ts:58), but those
# put an identifier before the dot and a parenthesis before it on the line, so
# neither shape matches; the `^\s*` anchor keeps a comment or a docstring line
# that merely mentions `t.test(` from redding the guard, and this file is
# written in prose about exactly these spellings.
_NESTED_DEFINITION_FORM = re.compile(
    rf"^\s*(?:await\s+)?(?:[A-Za-z_$][\w$]*{_NEWLINE_THEN_INDENT}\.)+"
    rf"(?:it|test|specify){_NEWLINE_THEN_INDENT}{_TRIPWIRE_TERMINAL}"
    rf"|^\s*await\s+(?:it|test|specify){_NEWLINE_THEN_INDENT}{_TRIPWIRE_TERMINAL}",
    re.M,
)


def _count_definitions(path: Path) -> int:
    """Count a JS/TS test file's definitions, red on forms the count cannot see.

    Used by both static counters so the renderer and GUI guards share one
    counting rule (and one tripwire). Note the negative case: a *method* call
    such as `expect(FENCE_END_RE.test("```"))` is not a definition - the chained
    form above requires a modifier keyword after the dot, so RegExp's own
    `.test(` matches neither regex, and a file full of such calls still counts
    correctly. Likewise a comment *describing* `it.each(` is not a definition;
    both patterns are line-anchored so prose cannot red the guard.

    Both checks are line-anchored, so each is tripped by a real call and only a
    real call. A tripwire that fires on prose would be trained away, and one
    that misses a spelling is worse than none because it reads as coverage.
    """
    text = path.read_text(encoding="utf-8")
    uncounted = _CHAINED_DEFINITION_FORM.findall(text)
    assert not uncounted, (
        f"{path} uses test-definition forms the doc-count guard cannot count: "
        f"{sorted(set(uncounted))}. The guard counts only `it(`/`test(` at the "
        "start of a line, so these definitions would be missing from Agent.md's "
        "breakdown while the runner still registers them. Teach "
        "_count_definitions to count this form (and sync Agent.md) before "
        "using it."
    )
    suites = _SUITE_PARAMETERISED_FORM.findall(text)
    assert not suites, (
        f"{path} uses parameterised *suite* forms the doc-count guard cannot "
        f"count: {sorted(set(suites))}. The guard counts `it(`/`test(` once "
        "each, but a suite body runs once per row, so every definition inside "
        "it executes more times than Agent.md records while the runner's own "
        "total stays honest - the same silent drift, one level up. Teach "
        "_count_definitions to expand this form (and sync Agent.md) before "
        "using it."
    )
    nested = _NESTED_DEFINITION_FORM.findall(text)
    assert not nested, (
        f"{path} uses *nested* test-definition forms the doc-count guard cannot "
        f"count: {sorted(set(nested))}. The guard counts only `it(`/`test(` at "
        "the start of a line, so a nested definition - `t.test(...)` (node:test "
        "subtests) or `await test(...)` - is executed by the runner while "
        "Agent.md's breakdown records nothing for it. Measured 2026-09-11: such "
        "a file recounts as 1 against a runner total of 3. Teach "
        "_count_definitions to count this form (and sync Agent.md) before "
        "using it."
    )
    return len(_DEFINITION_FORM.findall(text))


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
        counts[label] = _count_definitions(f)
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
        f.name[: -len(_GUI_TEST_SUFFIX)]: _count_definitions(f) for f in files
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


def _gui_tree(root: Path, files: dict[str, str]) -> None:
    """Build a minimal GUI test tree under a fake REPO_ROOT."""
    base = root / "emrg" / "gui" / "test"
    for rel, body in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


@pytest.mark.parametrize(
    "body",
    [
        "it.each([[1], [2]])('%i runs', () => {});\n",
        "it.for([1, 2])('%i runs', () => {});\n",
        "test.skipIf(process.platform === 'win32')('a', () => {});\n",
        "test.runIf(process.platform === 'darwin')('a', () => {});\n",
        "it.todo('later');\n",
        "it.fails('known broken', () => {});\n",
    ],
    ids=["each", "for", "skipIf", "runIf", "todo", "fails"],
)
def test_renderer_counts_fail_loud_on_a_parameterized_form(tmp_path, monkeypatch, body) -> None:
    """Every chained spelling must be red, not silently under-counted.

    `it.each([...])('name', ...)` runs one case per row and `test.skipIf(cond)(...)`
    registers a case either way - the plain definition regex matches neither, so
    before this tripwire a file could add cases that never reached Agent.md's
    breakdown while both renderer guards stayed green.

    Measured while widening the tripwire (cyc20260910-202123): `for`, `skipIf`
    and `runIf` were still counted as 0 with no complaint, i.e. the first four
    spellings tripped while these three stayed invisible. The list is now every
    callable member of the runners' definition API, pinned one test per spelling.
    """
    mod = _loaded_guard_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    _renderer_tree(tmp_path, {"lib/utils.test.ts": body})
    modifier = body.split("(")[0].split(".")[-1]
    with pytest.raises(AssertionError, match=rf"\.{modifier}\("):
        mod._static_renderer_counts()


def test_gui_counts_fail_loud_on_a_skipped_form(tmp_path, monkeypatch) -> None:
    """The GUI counter shares the same tripwire (it shares the same helper)."""
    mod = _loaded_guard_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    _gui_tree(
        tmp_path,
        {"state.test.js": "test('a', () => {});\ntest.skip('later', () => {});\n"},
    )
    with pytest.raises(AssertionError, match=r"test\.skip\("):
        mod._static_gui_counts()


def test_count_definitions_ignores_a_regex_method_call(tmp_path, monkeypatch) -> None:
    """The negative half: `expect(RE.test(...))` is not a definition.

    Measured 2026-09-10: `markdown.test.ts` has 13 definitions but 17 loose
    `\\b(it|test)\\(` matches - the extra 4 are `FENCE_END_RE.test(...)` calls.
    An over-broad tripwire would make every such file red.
    """
    mod = _loaded_guard_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    body = (
        "it('a', () => { expect(FENCE_END_RE.test('```')).toBe(true); });\n"
        "test('b', () => {});\n"
    )
    _renderer_tree(tmp_path, {"lib/markdown.test.ts": body})
    assert mod._static_renderer_counts() == {"markdown": 2}


@pytest.mark.parametrize(
    "body",
    [
        "describe.each([[1], [2]])('suite %i', () => {\n  it('a', () => {});\n});\n",
        "describe.for([1, 2])('suite %i', () => {\n  it('a', () => {});\n});\n",
        "describe.skip.each([[1], [2]])('suite %i', () => {\n  it('a', () => {});\n});\n",
        "describe.only.each([[1]])('suite %i', () => {\n  it('a', () => {});\n});\n",
    ],
    ids=["vitest-describe-each", "node-test-describe-for", "describe-skip-each", "describe-only-each"],
)
def test_doc_counts_fail_loud_on_a_parameterized_suite(tmp_path, monkeypatch, body) -> None:
    """A parameterised *suite* hides the same drift one level up.

    `describe.each([...])(...)` is vitest's parameterised suite and
    `describe.for(rows)(...)` is node:test's; both run the whole callback once
    per row, so the `it(` definitions inside execute N times while the static
    count records them once. The prior tripwire's own comment named
    `describe.for` as a live form while its pattern could not match it - a
    documented hole that read as coverage (cyc20260910-230247).

    Suites chain too: `ChainableSuiteAPI` carries the option setters *and*
    `each`/`for`, so `describe.skip.each(...)` registers its rows as well -
    measured with the real runner, which reported `Tests 2 skipped (2)` for it
    while the single-link pattern found 0 (cyc20260910-232400).
    """
    mod = _loaded_guard_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    _renderer_tree(tmp_path, {"lib/utils.test.ts": body})
    with pytest.raises(AssertionError, match=r"parameterised \*suite\*"):
        mod._static_renderer_counts()


@pytest.mark.parametrize(
    "body",
    [
        "it.concurrent.each([[1], [2], [3]])('%i runs', () => {});\n",
        "it.skip.each([[1], [2]])('%i runs', () => {});\n",
        "it.only.each([[1]])('%i runs', () => {});\n",
    ],
    ids=["concurrent-each", "skip-each", "only-each"],
)
def test_renderer_counts_fail_loud_on_a_nested_chained_form(tmp_path, monkeypatch, body) -> None:
    """A chain is not one link deep, so the pattern must not be either.

    `ChainableTestAPI` in @vitest/runner exposes the option setters *and*
    `each`/`for` on the same object (`tasks.d-*.d.ts:721`), so
    `it.concurrent.each([...])` is a real definition form. Measured
    (cyc20260910-232400) with the real runner: a probe file with
    `it.concurrent.each` + `test.skip.each` reported
    `Tests 3 passed | 2 skipped (5)`, while the single-link pattern found **0**
    of them - i.e. five executed cases documented as zero. The previous
    revision of this tripwire enumerated every *terminal* member but assumed
    one link, so it missed every nested spelling.
    """
    mod = _loaded_guard_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    _renderer_tree(tmp_path, {"lib/utils.test.ts": body})
    with pytest.raises(AssertionError, match=r"cannot count"):
        mod._static_renderer_counts()


def test_doc_counts_stay_green_on_prose_about_a_chained_form(tmp_path, monkeypatch) -> None:
    """A comment mentioning a chained form must NOT trip the tripwire.

    Both patterns are line-anchored (`^\\s*`), so only a real call - the
    statement on its own line - counts as a hit. A text-level pattern would make
    this file's own explanatory comments unspeakable, and a guard that fires on
    prose is a guard that gets disabled rather than fixed.
    """
    mod = _loaded_guard_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    body = (
        "// chain forms like it.each(...) or test.skip(...) are not counted,\n"
        "// and a parameterised suite such as describe.for(...) hides it\n"
        "const NOTE = 'call test.only( to isolate a case';\n"
        "it('a', () => {});\n"
    )
    _renderer_tree(tmp_path, {"lib/utils.test.ts": body})
    assert mod._static_renderer_counts() == {"utils": 1}


@pytest.mark.parametrize(
    "body",
    [
        "it\n  .each([[1], [2]])('%i runs', () => {});\n",
        "it\n    .each([[1]])('%i runs', () => {});\n",
        "it\n  .skip\n  .each([[1]])('%i runs', () => {});\n",
    ],
    ids=["split-before-each", "split-4-space", "split-multi-link"],
)
def test_renderer_counts_fail_loud_on_a_line_split_chain(tmp_path, monkeypatch, body) -> None:
    """A chain split across lines is still one chain the runner executes.

    The multi-link fix covered chains that are *written* on one line; it still
    required the dot to follow the keyword on the same line. Measured with the
    real runner (cyc20260910-234907): a probe file containing

        it
          .each([[1], [2]])('%i', () => {})

    reported `Tests 2 passed (2)` while both patterns found **0** - two executed
    cases recorded as zero, reached through line position rather than spelling.

    Prettier rejoins the statement (verified: `prettier --parser typescript
    --print-width 80` collapses it back to one line), but nothing in this repo
    runs prettier - there is no config and no CI step - so a hand-authored file
    carries the split. The boundary is stated rather than hidden: `it (` with a
    bare space stays uncounted, because on its own line that is indistinguishable
    from prose and this guard must not fire on a sentence.
    """
    mod = _loaded_guard_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    _renderer_tree(tmp_path, {"lib/utils.test.ts": body})
    with pytest.raises(AssertionError, match=r"cannot count"):
        mod._static_renderer_counts()


def test_renderer_count_matches_the_runner_for_a_line_split_call(tmp_path, monkeypatch) -> None:
    """A plain call split from its paren must be *counted*, not tripped.

    This is the case the tripwire alone cannot cover, because the form is
    countable once the pattern tolerates the newline. Measured with the real
    runner (cyc20260910-234907): a file with

        it
        ('a', () => {})
        it('b', () => {})

    reported `Tests 2 passed (2)` while the static count said **1** - a silent
    under-count with every guard green, i.e. exactly the drift class this file
    exists to prevent. The pattern now counts both, so the count equals the
    runner's total and the guard stays quiet.
    """
    mod = _loaded_guard_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    body = (
        "describe('split-call', () => {\n"
        "  it\n"
        "  ('a', () => { expect(1).toBe(1); });\n"
        "  it('b', () => { expect(1).toBe(1); });\n"
        "});\n"
    )
    _renderer_tree(tmp_path, {"lib/utils.test.ts": body})
    assert mod._static_renderer_counts() == {"utils": 2}


def test_doc_counts_stay_green_on_prose_about_a_split_call(tmp_path, monkeypatch) -> None:
    """Prose using the keyword followed by a parenthetical must stay green.

    The newline tolerance is the one change here that could plausibly turn a
    sentence into a false positive, so it gets its own counter-test. `it (the
    runner) registers two cases` is prose, not a call - which is why the
    separator admits a newline but not a bare space.
    """
    mod = _loaded_guard_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    body = (
        "// It (the runner) registers two cases here, per the measurement.\n"
        "// test (singular) is node:test's spelling.\n"
        "it('a', () => {});\n"
    )
    _renderer_tree(tmp_path, {"lib/utils.test.ts": body})
    assert mod._static_renderer_counts() == {"utils": 1}


@pytest.mark.parametrize(
    "body",
    [
        "it.each`\n  a    | b\n  ${1} | ${2}\n`('%i runs', () => {});\n",
        "test.each`\n  a\n  ${1}\n`('%i runs', () => {});\n",
        "it.skip.each`\n  a\n  ${1}\n`('%i runs', () => {});\n",
        "it.only.each`\n  a\n  ${1}\n`('%i runs', () => {});\n",
    ],
    ids=["it-each-tagged", "test-each-tagged", "skip-tagged", "only-tagged"],
)
def test_renderer_counts_fail_loud_on_a_tagged_template_each(tmp_path, monkeypatch, body) -> None:
    """A definition form can be *tagged* rather than *called*.

    `it.each` is a tag function in vitest, so the table syntax

        it.each`
          a    | b
          ${1} | ${2}
        `('adds $a to $b', ({ a, b }) => { ... })

    registers one case per table row while ending in a **backtick**, not a paren.
    Measured with the real runner (cyc20260910-001002): that file reported
    `Tests 2 passed (2)` while the counted pattern **and both tripwires** returned
    0 - two executed cases documented as zero with every guard green.
    `it.skip.each` / `it.only.each` behave the same (measured `1 skipped` /
    `1 passed`, and a valid tagged `it`-family member at the top level).

    This is the third composition axis found in three cycles - after "which
    members exist?" (a chain is not one link deep) and "which line?" (a call can
    be split from its paren) comes "which terminal?" (a definition can be tagged).
    The counted pattern deliberately still requires `(`, so a tagged form is
    *reported* rather than silently mis-counted.
    """
    mod = _loaded_guard_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    _renderer_tree(tmp_path, {"lib/utils.test.ts": body})
    with pytest.raises(AssertionError, match=r"cannot count"):
        mod._static_renderer_counts()


def test_renderer_counts_fail_loud_on_a_tagged_parameterised_suite(tmp_path, monkeypatch) -> None:
    """The tagged template works for suites too, so the suite guard needs it.

    Measured with the real runner (cyc20260910-001002): `describe.each\\`...\\`(...)`
    is valid and runs its body once per table row. It reaches the guard through the
    *suite* pattern, which is a separate regex (widening the counted pattern would
    change the count), so it needs the same two-terminal treatment.
    """
    mod = _loaded_guard_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    body = "describe.each`\n  a\n  ${1}\n`('suite %i', () => {\n  it('a', () => {});\n});\n"
    _renderer_tree(tmp_path, {"lib/utils.test.ts": body})
    with pytest.raises(AssertionError, match=r"parameterised \*suite\*"):
        mod._static_renderer_counts()


@pytest.mark.parametrize(
    "body",
    [
        "it('a', () => {});\n",
        "it('a', () => {});\nit('b', () => {});\n",
    ],
    ids=["one-call", "two-calls"],
)
def test_renderer_counts_stay_green_on_ordinary_calls(tmp_path, monkeypatch, body) -> None:
    """Counter-test for the two-terminal change: ordinary calls must stay quiet.

    Adding a backtick alternative to the terminal must not make an ordinary call
    trip - this pins the boundary the widening could have broken. Note the array
    forms (`it.each([...])`, `describe.each([...])`) are deliberately **not** here:
    they are uncountable and *should* trip, which their own tests assert.

    `it\\`...\\`` (the bare keyword as a tag) is intentionally absent too: it is a
    runtime `TypeError: it(...) is not a function` (measured with the real runner),
    so it is not a definition and the guard is right to stay silent.
    """
    mod = _loaded_guard_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    _renderer_tree(tmp_path, {"lib/utils.test.ts": body})
    assert mod._static_renderer_counts() == {
        "utils": len(mod._DEFINITION_FORM.findall(body))
    }


@pytest.mark.parametrize(
    "body",
    [
        "test('outer', async (t) => {\n  await t.test('inner', () => {});\n});\n",
        "test('outer', async () => {\n  await test('inner', () => {});\n});\n",
        "describe('outer', () => {\n  await t.it('inner', () => {});\n});\n",
    ],
    ids=["receiver-subtest", "awaited-bare-keyword", "receiver-it"],
)
def test_renderer_counts_fail_loud_on_a_nested_definition(tmp_path, monkeypatch, body) -> None:
    """A definition can be *nested*, reached without the keyword starting a line.

    Measured with the real runner (cyc20260911-001002) - the same probe file, one
    visible `test('outer')` holding two nested cases:

        test('outer', async (t) => { await t.test('inner', ...); ... })  -> tests 3
        test('outer', async () => { await test('inner', ...); ... })     -> tests 3

    In both, `_DEFINITION_FORM` and both existing tripwires returned 0 for the
    nested calls, so the static count said **1** against a runner total of **3**:
    two executed cases documented as zero with every guard green.

    This is the fourth composition axis in four cycles - after "which members
    exist?" (a chain is not one link deep), "which line?" (a call can be split
    from its paren) and "which terminal?" (a definition can be tagged) comes
    "where is it nested?". The counted pattern is still `^it(|^test(`; a nested
    form is therefore *reported* rather than silently mis-counted.
    """
    mod = _loaded_guard_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    _renderer_tree(tmp_path, {"lib/utils.test.ts": body})
    with pytest.raises(AssertionError, match=r"\*nested\*"):
        mod._static_renderer_counts()


def test_doc_counts_stay_green_on_method_calls_and_prose_about_nesting(
    tmp_path, monkeypatch
) -> None:
    """Counter-test for the nested tripwire: it must fire on calls, not text.

    The tree is full of *method* calls whose name ends in `test` -
    `expect(FENCE_END_RE.test("```"))` (markdown.test.ts:58) - and the line
    anchor plus the `\\.`-before-keyword requirement is what keeps those quiet.
    A docstring or comment mentioning `t.test(` must stay quiet too; this file is
    written in prose about exactly these spellings, and a guard that reds on a
    sentence gets deleted.

    Measured the same cycle: 0 line-anchored hits across all 54 renderer/GUI test
    files for the nested shapes, so this is a tripwire for a form the tree does
    not use, not a filter over forms it does.
    """
    mod = _loaded_guard_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    body = (
        "it('a', () => {\n"
        "  // t.test( is how node:test nests, but this line is a comment\n"
        "  expect(FENCE_END_RE.test('```')).toBe(true);\n"
        "  const re = /^it\\.each\\(/;\n"
        "});\n"
    )
    _renderer_tree(tmp_path, {"lib/utils.test.ts": body})
    counts = mod._static_renderer_counts()
    assert counts == {"utils": 1}, counts


def test_nested_definition_tripwire_is_empty_on_the_real_tree() -> None:
    """The pinned boundary, on the real tree: the tripwire covers forms none use.

    Stated as a measurement rather than an assumption - if a real test file ever
    adopts a nested definition, this fails first and the fix is to teach the
    counter, which is exactly the signal the tripwire is for.
    """
    guard = _loaded_guard_module()
    # Walk the same two trees the static counters walk.
    test_files = sorted(
        list((REPO_ROOT / "emrg" / "gui" / "renderer" / "src").rglob("*.test.ts"))
        + list((REPO_ROOT / "emrg" / "gui" / "renderer" / "src").rglob("*.test.tsx"))
        + list((REPO_ROOT / "emrg" / "gui" / "test").rglob("*.test.js"))
    )
    assert len(test_files) >= 50, f"expected >=50 test files, found {len(test_files)}"
    hits = {
        path.relative_to(REPO_ROOT).as_posix(): guard._NESTED_DEFINITION_FORM.findall(
            path.read_text(encoding="utf-8")
        )
        for path in test_files
    }
    tripped = {name: found for name, found in hits.items() if found}
    assert not tripped, (
        "a real test file now uses a nested definition form, so the static count "
        f"would under-report it: {tripped}. Teach _count_definitions to count the "
        "form and sync Agent.md."
    )
