"""A pipe inside a code span of a table row must be escaped, or GFM splits the row.

Why this file exists
--------------------
GitHub's table extension splits a table row on unescaped pipes, and a **code span
does not protect one** — the escape is ``\\|``. `README.md`'s `/skills` row wrote
its span unescaped:

    | `/skills` | List loaded skills; `/skills available|install|update` |

so that row parsed as four cells where its header has two, and the table broke
there. The control was already in the repository: `README.cn.md` carries the same
sentence in the escaped form, so the unescaped spelling was a drift from this
repository's own text rather than a house style.

The reading is structural, not textual: every pipe inside a code span of a table
row, in the markdown this repository publishes. Two controls keep it from being a
check that reports the healthy answer whatever it is given — a synthetic pair in
`test_the_reading_separates_the_escape_from_the_missing_one` (escaped passes,
unescaped fails) and a positive control on the real corpus in
`test_the_instrument_sees_the_shape_it_reads_for` (if the walk or the extractor
sees nothing, the test that asserts "no site" has not measured anything).

**Named scope.** The walk is `git ls-files -z -- '*.md'`: this repository's
tracked markdown, which is what leaves the checkout and what a reader renders. A
markdown file that is not committed — a scratch note, the agent's own state under
`.emrg/` — is outside it by construction. Lines inside a fenced block are skipped,
because a fenced block is not a table row.

**Named limit.** A pipe preceded by a backslash is read as escaped, so a span
wanting a *literal* backslash before a pipe (`\\\\|`) would be read as "already
escaped". No such span exists in this corpus; the reading is stated here so a
future reader does not take the silence for coverage.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A code span, with its content. Table cells hold single-line spans.
CODE_SPAN = re.compile(r"`([^`]*)`")

#: A pipe that is not preceded by a backslash — the one GFM reads as a cell boundary.
UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")

#: The opening *or* closing line of a fenced block.
FENCE = re.compile(r"^\s*(```|~~~)")


def _tracked_markdown() -> list[str]:
    """Repo-relative paths of every tracked markdown file, via `git ls-files`.

    Git's index is used rather than a directory walk for the reason recorded in
    `test_conflict_markers.py`: a walk drifts from "what is published" as soon as
    a directory is added, and the index does not. `-z` plus an explicit UTF-8
    decode keeps non-ASCII paths intact, and the names are normalised to `/` so
    the same list is produced on the Windows and POSIX CI legs.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    return sorted(name.replace("\\", "/") for name in listed.split("\0") if name)


def _table_code_spans(text: str) -> list[tuple[int, str]]:
    """`(line number, span)` for every code span on a table row of `text`.

    A table row is a line whose first non-space character is `|`; a line inside a
    fenced block is not, and is skipped — including the fence's own lines.
    """
    found: list[tuple[int, str]] = []
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            fence = None if fence else line.lstrip()[:3]
            continue
        if fence is not None:
            continue
        if not line.lstrip().startswith("|"):
            continue
        found.extend((number, span) for span in CODE_SPAN.findall(line))
    return found


def _offending_code_spans(text: str) -> list[tuple[int, str]]:
    """The subset of `_table_code_spans` that GFM would split the row on."""
    return [(n, span) for n, span in _table_code_spans(text) if UNESCAPED_PIPE.search(span)]


def test_no_tracked_markdown_unescapes_a_pipe_inside_a_table_code_span() -> None:
    """The reading: no published markdown carries the spelling that splits a row."""
    sites: list[str] = []
    for name in _tracked_markdown():
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        sites.extend(f"{name}:{number}: `{span}`" for number, span in _offending_code_spans(text))

    assert not sites, (
        "an unescaped pipe inside a code span splits the table row it sits in — GFM reads "
        "the pipe as a cell boundary, and only `\\|` escapes it. Found: " + "; ".join(sites)
    )


def test_the_instrument_sees_the_shape_it_reads_for() -> None:
    """The control on the real corpus: a walk or an extractor that sees nothing proves nothing.

    "No offending span" and "no span at all" are different readings, and only the
    first is the healthy one. This requires both halves of the instrument to be
    live on the tracked corpus, so the reading above cannot pass because it looked
    at nothing.
    """
    names = _tracked_markdown()
    assert len(names) >= 10, (
        f"the walk reached only {len(names)} tracked markdown file(s): a scope this narrow "
        "would make the reading above vacuous rather than clean"
    )

    spans = 0
    for name in names:
        spans += len(_table_code_spans((REPO_ROOT / name).read_text(encoding="utf-8")))
    assert spans > 0, (
        "the extractor found no code span on any table row of the tracked markdown, so it "
        "has not been shown to read the shape it exists for"
    )


def test_the_reading_separates_the_escape_from_the_missing_one() -> None:
    """The synthetic control: both directions on the same text, one escaped, one not."""
    escaped = "| `/skills` | List loaded skills; `/skills available\\|install\\|update` |\n"
    unescaped = "| `/skills` | List loaded skills; `/skills available|install|update` |\n"

    assert _offending_code_spans(escaped) == []
    assert [span for _, span in _offending_code_spans(unescaped)] == [
        "/skills available|install|update"
    ]


def test_a_fenced_block_is_not_a_table_row() -> None:
    """The scope boundary, driven with a line that looks like a row but is inside a fence.

    No tracked markdown carries such a line today, so this arm is exercised on
    synthetic text: without the fence rule a shell pipeline quoted in a fenced
    block would be reported as a broken table.
    """
    fenced = "```markdown\n| `/skills` | `/skills available|install|update` |\n```\n"
    assert _offending_code_spans(fenced) == []

    # The same line outside the fence *is* a row, which is what makes the arm above
    # a reading rather than a blanket exemption.
    assert _offending_code_spans(fenced.split("\n")[1] + "\n") != []
