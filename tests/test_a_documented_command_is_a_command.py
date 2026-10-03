"""A documented command must be a *command*: a continuation with an **even** number of
trailing backslashes continues nothing.

The shell rule is arithmetic, not style. `\\` at the end of a line is an escaped backslash
— a literal character — and the newline after it still ends the command; only an *odd*
count (the last one escaping the newline) joins the next line. So a block written with a
doubled continuation reads as **N separate commands**, and the variables a reader meant to
hand to the last one never reach it.

Measured 2026-10-03 (PR #1828, `DEVELOPMENT.md:244-245`; the block copied out of the file
verbatim into a script and run under `/bin/bash`):

```
APPLE_ID=<id> MACOS_NOTARY_APP_PASSWORD=<pw> \\\\        # written in the file as two characters
    MACOS_NOTARY_TEAM_ID=<team> \\\\
    uv run --no-sync python3 scripts/check-notary-credentials.py
$ bash doc.sh
doc.sh: line 1: \\: command not found
doc.sh: line 2: \\: command not found    # rc=127, and the preflight never ran
```

With a single backslash the same block is **one** command — the only difference between
landing at ``uv: command not found`` (the tool is absent, so it *did* reach the call) and
never invoking it at all. That block is the host-facing remedy for a release that failed
in CI, so a host who copies it gets three failures instead of one diagnosis.

**Population, measured 2026-10-03**: 67 shell fences across 16 tracked `*.md`; the shape
above occurs **twice**, both on that one PR's branch, and never on master. This guard is
therefore narrow on purpose — it is the one slice of "the documented command runs" that is
decidable from the text, and a rule with a measured false-positive rate of zero over the
whole tree is a rule worth keeping narrow (the wider sibling — "every path a doc names
exists" — was measured the same day at 29 hits, all of them either a bare basename whose
directory is prose, or a path the doc explicitly guards with `2>/dev/null || echo`, so it
was **not** added).

What this cannot see: a block that is syntactically one command and still wrong (an option
the tool does not take, a variable it never reads), and a fence whose language string is
not in `SHELL_FENCES`. Both are stated so a green run is read as what it is.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Fence languages a shell reads. `text`/`markdown`/`toml` blocks are documentation *of*
#: something else, and a two-backslash line inside one is not a claim about bash.
SHELL_FENCES = frozenset({"bash", "sh", "shell", "zsh", "console"})


def tracked_markdown() -> list[str]:
    """Repo-relative `/`-separated paths of the tracked `*.md` files, via `git ls-files`.

    The index, not a directory walk: `.venv/` and `node_modules/` hold thousands of
    markdown files nobody published, and a walk would also miss the point — what matters
    is what ships. `encoding="utf-8"` because the default text mode decodes with the
    locale codec (cp1252 on the `windows-2025` runner) while git emits UTF-8 bytes, and
    `Path(...).as_posix()` because git emits the index path with the *platform*
    separator — Windows hands back `emrg\\server\\x.md`.
    """
    out = subprocess.run(
        ["git", "ls-files", "--", "*.md"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    return [Path(line.strip()).as_posix() for line in out.splitlines() if line.strip()]


def shell_blocks(text: str) -> list[tuple[int, str]]:
    """Every fenced shell block in `text`, as `(line number of its first body line, body)`.

    Line-based rather than a regex over the file, because the unit a reader copies is the
    block and its line numbers are what a failure message has to name. An unterminated
    fence is reported by `unterminated_fences`, not silently swallowed here.
    """
    blocks: list[tuple[int, str]] = []
    lines = text.split("\n")
    inside = False
    info = ""
    body: list[str] = []
    start = 0
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not inside:
            if stripped.startswith("```"):
                inside = True
                info = stripped[3:].strip().split()[0].lower() if stripped[3:].strip() else ""
                body = []
                start = number + 1
            continue
        if stripped.startswith("```"):
            inside = False
            if info in SHELL_FENCES:
                blocks.append((start, "\n".join(body)))
            continue
        body.append(line)
    return blocks


def unterminated_fences(text: str) -> int:
    """1 when `text` opens one more fence than it closes, else 0.

    Counted rather than ignored: an unclosed fence swallows the rest of the file, so a
    scanner that skipped it quietly would answer "no defect here" about text it never
    read — the shape this repo has spent cycles removing from its instruments.
    """
    markers = sum(1 for line in text.split("\n") if line.strip().startswith("```"))
    return markers % 2


def continuations_that_continue_nothing(body: str) -> list[tuple[int, str, int]]:
    """Lines in a block that look like a continuation and are not one.

    A line qualifies when it ends with an **even** number of backslashes (two or more —
    so every one of them is escaped) *and* the next line is indented, which is the only
    reason anyone writes that: they meant to join the two. Trailing spaces after the
    backslashes are cosmetic and ignored.

    Returns `(line number within the block, the line, how many backslashes)`.
    """
    lines = body.split("\n")
    hits: list[tuple[int, str, int]] = []
    for index, line in enumerate(lines[:-1]):
        stripped = line.rstrip(" \t")
        trailing = len(stripped) - len(stripped.rstrip("\\"))
        if trailing < 2 or trailing % 2:
            continue
        following = lines[index + 1]
        if following[:1] in (" ", "\t") and following.strip():
            hits.append((index + 1, line, trailing))
    return hits


# ── The rule, over the tree ───────────────────────────────────────────────────


def test_no_tracked_markdown_continues_with_an_even_number_of_backslashes() -> None:
    offenders: list[str] = []
    for path in tracked_markdown():
        text = (REPO / path).read_text(encoding="utf-8")
        for start, body in shell_blocks(text):
            for offset, line, count in continuations_that_continue_nothing(body):
                offenders.append(
                    f"{path}:{start + offset - 1} ends with {count} backslashes and the "
                    f"next line is indented, so bash runs {count // 2 + 1} commands and the "
                    f"variables never reach the last one: {line!r}"
                )
    assert not offenders, (
        "a documented shell command does not run as written — the line ends with an even "
        "number of backslashes, which is a literal backslash, not a line continuation. "
        "Write one backslash (an odd number) to join the lines:\n  " + "\n  ".join(offenders)
    )


def test_the_scan_reads_the_tree_it_names() -> None:
    """The control leg: a scan that read nothing would pass the rule above.

    `DEVELOPMENT.md` alone carried 16 shell fences when this was written, and the whole
    tree 67, so a floor of 10 there and 40 overall is a floor a silent scanner cannot
    clear — and it fails loudly if the reader itself breaks.
    """
    files = tracked_markdown()
    assert len(files) >= 10, f"only {len(files)} tracked *.md files — is this the repo?"
    per_file = {path: len(shell_blocks((REPO / path).read_text(encoding="utf-8"))) for path in files}
    total = sum(per_file.values())
    assert per_file.get("DEVELOPMENT.md", 0) >= 10, (
        f"DEVELOPMENT.md yielded {per_file.get('DEVELOPMENT.md', 0)} shell fences, expected "
        f"at least 10 — the fence reader is not reading it. Per-file counts: {per_file}"
    )
    assert total >= 40, f"{total} shell fences across {len(files)} files, expected at least 40"


def test_every_fence_in_the_tree_is_closed() -> None:
    unclosed = [
        path
        for path in tracked_markdown()
        if unterminated_fences((REPO / path).read_text(encoding="utf-8"))
    ]
    assert not unclosed, (
        f"these files open a fence and never close it, so the rest of the file renders as "
        f"code: {unclosed}"
    )


# ── The rule, on the shape that motivated it ──────────────────────────────────


def test_the_shape_that_motivated_this_guard_is_caught() -> None:
    """The measured instance, kept beside the rule that outlaws it.

    PR #1828's block, with its placeholders filled in so the backslashes are the only
    thing under test — the same two lines that printed `\\: command not found` twice and
    never reached the preflight.
    """
    block = (
        "APPLE_ID=a@b.c MACOS_NOTARY_APP_PASSWORD=pw \\\\\n"
        "    MACOS_NOTARY_TEAM_ID=TEAM \\\\\n"
        "    uv run --no-sync python3 scripts/check-notary-credentials.py\n"
    )
    hits = continuations_that_continue_nothing(block)
    assert [line for _, line, _ in hits] == [
        "APPLE_ID=a@b.c MACOS_NOTARY_APP_PASSWORD=pw \\\\",
        "    MACOS_NOTARY_TEAM_ID=TEAM \\\\",
    ], f"the measured shape was not caught: {hits!r}"
    assert [count for _, _, count in hits] == [2, 2]


def test_a_real_continuation_is_not_flagged() -> None:
    """The other direction: the fix must not outlaw line continuations.

    One backslash (odd) with an indented continuation line is exactly how the sibling
    example in the same file is written, and how every other fence in the tree is.
    """
    good = "APPLE_ID=a@b.c \\\n    MACOS_NOTARY_TEAM_ID=t \\\n    echo ok\n"
    assert continuations_that_continue_nothing(good) == []


def test_the_reader_looks_only_inside_shell_fences() -> None:
    """A two-backslash line in prose or in a non-shell fence is not a claim about bash."""
    text = (
        "A doubled continuation like \\\\\n"
        "    this one in prose is not a command.\n"
        "\n"
        "```markdown\n"
        "a \\\\\n"
        "    b\n"
        "```\n"
        "\n"
        "```bash\n"
        "echo \\\\\n"
        "    nested\n"
        "```\n"
    )
    assert [body for _, body in shell_blocks(text)] == ["echo \\\\\n    nested"]
    offenders = continuations_that_continue_nothing(shell_blocks(text)[0][1])
    assert len(offenders) == 1, offenders


def test_a_continuation_with_nothing_after_it_is_not_flagged() -> None:
    """The rule names a shape, and this is the boundary of that shape.

    A doubled backslash is only suspicious when a line follows that someone meant to join:
    at the end of a block (the last line) there is nothing to continue, and a following
    line that is not indented is a new command by construction.
    """
    assert continuations_that_continue_nothing("echo done \\\\") == []
    assert continuations_that_continue_nothing("echo a \\\\\necho b\n") == []


def test_the_rule_is_about_the_number_of_backslashes_not_their_presence() -> None:
    """Parity is the whole rule, so both halves of it are read here.

    Three backslashes are a real continuation (the last one escapes the newline, the pair
    before it is a literal), and four are not — so a detector that asked only "does this
    line end in a backslash" would call the working form broken and the broken form fine.
    """
    odd = "echo a \\\\\\\n    echo b\n"  # three backslashes
    assert continuations_that_continue_nothing(odd) == []

    even = "echo a \\\\\\\\\n    echo b\n"  # four
    hits = continuations_that_continue_nothing(even)
    assert len(hits) == 1 and hits[0][2] == 4, hits


def test_an_unterminated_fence_is_reported_not_skipped() -> None:
    assert unterminated_fences("```bash\necho hi\n") == 1
    assert unterminated_fences("```bash\necho hi\n```\n") == 0
