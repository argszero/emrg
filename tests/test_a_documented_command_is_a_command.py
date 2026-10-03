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

**The subject is the shell text this repo ships, in every carrier it ships it in** — the
fence a human copies is the motivating case, not the only one:

| carrier | read as | why it is in scope |
|---|---|---|
| fenced `bash`/`sh`/`shell`/`zsh`/`console` blocks in tracked `*.md` | `shell_blocks` | a reader copies the block and runs it |
| `run:` block scalars in `.github/workflows/*.yml` | `workflow_run_blocks` | **CI** runs it; the same arithmetic decides it, and the failure is a step that never invoked what it names |
| tracked `*.sh` files | `shell_scripts` | the host runs them |

Widened 2026-10-03 (`cyc20261003-100623`), one cycle after the rule was written, because the
carrier that matters most was the one not read: a `run:` block with this bug is not a doc
that misleads a host, it is **CI skipping the work the step names** while reporting a
failure that points at nothing. Population of the two new carriers, measured before adding
them: 32 `run:` keys across the two workflows (20 of them block scalars, which is what can
carry the shape; a one-line `run:` cannot — its next line is another YAML key), and 10
tracked `*.sh` files. The shape occurs **0 times** in either, so this widening changes no
verdict today and closes a blind spot rather than fixing an instance.

**Population, measured 2026-10-03**: 67 shell fences across 16 tracked `*.md`; the shape
above occurs **twice**, both on that one PR's branch, and never on master. This guard is
therefore narrow on purpose — it is the one slice of "the command runs" that is decidable
from the text, and a rule with a measured false-positive rate of zero over the whole tree is
a rule worth keeping narrow. Two wider siblings were measured the same day and **not**
added: "every path a doc names exists" (29 hits, all a bare basename whose directory is
prose or a path the doc guards with `2>/dev/null || echo`), and "the block parses under
`bash -n`" (7 of 67 fences fail, **all** of them on an angle-bracket metavariable such as
`<修复后commit>` or `{{ source_dir }}`, which is a placeholder and not a defect — a rule whose
false positives outnumber its true ones is not a rule).

What this cannot see: a block that is syntactically one command and still wrong (an option
the tool does not take, a variable it never reads); a fence whose language string is not in
`SHELL_FENCES`; and a `run:` written as a one-line scalar, which cannot carry the shape (its
next line is another key) but is also not read as a block. Stated so a green run is read as
what it is.
"""

from __future__ import annotations

import re
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
    return _tracked("*.md")


def tracked_workflows() -> list[str]:
    """Repo-relative `/`-separated paths of the tracked workflow files.

    `*.yml`/`*.yaml` under `.github/workflows/`, not every YAML in the repo: the
    question here is what **CI executes**, and a YAML file that is not a workflow has no
    `run:` for anything to run.
    """
    return [p for p in _tracked("*.yml", "*.yaml") if p.startswith(".github/workflows/")]


def tracked_shell_scripts() -> list[str]:
    """Repo-relative `/`-separated paths of the tracked shell scripts.

    By extension, and the extension is the whole claim: a `.sh` file is a script the
    host or a build runs, so its every line is shell. A file without the suffix that
    happens to hold shell (a heredoc writer, say) is not read — stated in the module
    docstring rather than guessed at.
    """
    return _tracked("*.sh")


def _tracked(*patterns: str) -> list[str]:
    """`git ls-files` restricted to `patterns`, normalised to `/`-separated paths."""
    out = subprocess.run(
        ["git", "ls-files", "--", *patterns],
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


def workflow_run_blocks(text: str) -> list[tuple[int, str]]:
    """Every `run:` block scalar in a workflow, as `(line of its first body line, body)`.

    Only the **block scalar** forms (`run: |`, `run: |-`, `run: |+`, `run: >`, `run: >-`,
    `run: >+`), because a one-line `run: <command>` cannot carry the shape this guard
    looks for: a doubled continuation needs a line to *continue*, and in the one-line form
    the next line is the next YAML key, not part of the command. Making that explicit here
    is the difference between "not read" and "cannot apply".

    The body is the following lines more indented than the `run:` key, exactly as YAML
    defines a block scalar. Blank lines are kept (they are part of the block and YAML
    treats them as content), and the block ends at the first line that is not blank and
    not more indented — the next key.

    What this does not do: parse YAML. A `run:` key inside a quoted string or a comment
    would be read as a block here where a parser would not, and a folded (`>`) scalar's
    real text differs from the source lines. Both are acceptable for a rule about *what a
    line ends with* — the source line is the thing being judged, not the scalar's value —
    and the control leg pins the population against a real parser's count.
    """
    blocks: list[tuple[int, str]] = []
    lines = text.split("\n")
    pattern = re.compile(r"^(\s*)run:\s*[|>][+-]?\s*(?:#.*)?$")
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        indent = len(match.group(1))
        body: list[str] = []
        for following in lines[index + 1:]:
            if following.strip() and len(following) - len(following.lstrip(" ")) <= indent:
                break
            body.append(following)
        while body and not body[-1].strip():
            body.pop()
        blocks.append((index + 2, "\n".join(body)))
    return blocks


def shell_scripts(path: str) -> list[tuple[int, str]]:
    """A tracked `*.sh` file as one block: the whole file is shell by construction."""
    return [(1, (REPO / path).read_text(encoding="utf-8"))]


# ── The rule, over the tree ───────────────────────────────────────────────────


def _carriers() -> list[tuple[str, list[tuple[int, str]]]]:
    """Every `(path, blocks)` this rule governs, from all three carriers.

    One home for "which text is in scope", so the rule and the control leg cannot come to
    disagree about it — the shape that lets a rule quietly stop reading a carrier.
    """
    carried: list[tuple[str, list[tuple[int, str]]]] = []
    for path in tracked_markdown():
        carried.append((path, shell_blocks((REPO / path).read_text(encoding="utf-8"))))
    for path in tracked_workflows():
        carried.append((path, workflow_run_blocks((REPO / path).read_text(encoding="utf-8"))))
    for path in tracked_shell_scripts():
        carried.append((path, shell_scripts(path)))
    return carried


def _offenders_in(path: str, blocks: list[tuple[int, str]]) -> list[str]:
    found: list[str] = []
    for start, body in blocks:
        for offset, line, count in continuations_that_continue_nothing(body):
            found.append(
                f"{path}:{start + offset - 1} ends with {count} backslashes and the "
                f"next line is indented, so bash runs {count // 2 + 1} commands and the "
                f"variables never reach the last one: {line!r}"
            )
    return found


def test_no_shell_text_in_the_tree_continues_with_an_even_number_of_backslashes() -> None:
    offenders: list[str] = []
    for path, blocks in _carriers():
        offenders += _offenders_in(path, blocks)
    assert not offenders, (
        "a shell command this repo ships does not run as written — the line ends with an "
        "even number of backslashes, which is a literal backslash, not a line continuation. "
        "Write one backslash (an odd number) to join the lines:\n  " + "\n  ".join(offenders)
    )


def test_the_scan_reads_every_carrier_it_names() -> None:
    """The control leg: a scan that read nothing would pass the rule above.

    A floor per carrier, because the failure this catches is one carrier silently dropping
    out — a reader that returns nothing for every workflow still leaves the markdown rule
    green, and the carrier no one reads is exactly the one that decays. The floors come
    from measured populations, not taste: `DEVELOPMENT.md` alone carried 16 shell fences
    and the tree 67; the two workflows carry 20 block scalars between them (a real YAML
    parser's count of `run` keys is pinned separately below); there are 10 tracked `*.sh`.
    """
    md = tracked_markdown()
    wf = tracked_workflows()
    sh = tracked_shell_scripts()
    assert len(md) >= 10, f"only {len(md)} tracked *.md files — is this the repo?"
    assert len(wf) >= 2, f"only {len(wf)} tracked workflows: {wf}"
    assert len(sh) >= 5, f"only {len(sh)} tracked *.sh files: {sh}"

    per_file = {p: len(shell_blocks((REPO / p).read_text(encoding="utf-8"))) for p in md}
    assert per_file.get("DEVELOPMENT.md", 0) >= 10, (
        f"DEVELOPMENT.md yielded {per_file.get('DEVELOPMENT.md', 0)} shell fences, expected "
        f"at least 10 — the fence reader is not reading it. Per-file counts: {per_file}"
    )
    total = sum(per_file.values())
    assert total >= 40, f"{total} shell fences across {len(md)} files, expected at least 40"

    run_blocks = {
        p: len(workflow_run_blocks((REPO / p).read_text(encoding="utf-8"))) for p in wf
    }
    assert run_blocks.get(".github/workflows/build-release.yml", 0) >= 10, (
        f"build-release.yml yielded {run_blocks.get('.github/workflows/build-release.yml', 0)} "
        f"run blocks, expected at least 10 — the block-scalar reader is not reading it. "
        f"Per-file counts: {run_blocks}"
    )
    assert sum(run_blocks.values()) >= 15, (
        f"{sum(run_blocks.values())} run blocks across {len(wf)} workflows, expected at least 15"
    )

    script_lines = sum(len(shell_scripts(p)[0][1].split("\n")) for p in sh)
    assert script_lines >= 200, f"the {len(sh)} tracked scripts hold only {script_lines} lines"


def test_the_run_reader_counts_what_a_real_yaml_parser_counts() -> None:
    """The block-scalar reader against the authority on what a `run:` block is.

    `workflow_run_blocks` is a line scanner, so the risk is that it agrees with itself and
    with nothing else. A real parser is the independent path: every multi-line `run:` value
    this file finds must be one the parser also sees, and the parser must see no *more*
    multi-line runs than the scanner — a one-line `run:` is deliberately out of scope, and
    the two counts differing in that direction would mean the scanner is missing blocks.

    The comparison is on counts rather than text: PyYAML strips the block's indentation and
    a folded scalar's value is not the source lines, so matching by value would be a
    comparison of two different things. But a missing *block* changes the count, which is
    the property that matters here.
    """
    import yaml

    def run_values(node: object, out: list[str]) -> list[str]:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "run" and isinstance(value, str):
                    out.append(value)
                run_values(value, out)
        elif isinstance(node, list):
            for value in node:
                run_values(value, out)
        return out

    scanned = 0
    parsed = 0
    for path in tracked_workflows():
        text = (REPO / path).read_text(encoding="utf-8")
        scanned += len(workflow_run_blocks(text))
        values: list[str] = []
        parsed += sum(1 for value in run_values(yaml.safe_load(text), values) if "\n" in value)
    assert parsed >= 10, f"PyYAML found only {parsed} multi-line runs — is this a workflow?"
    assert scanned >= parsed, (
        f"the scanner found {scanned} run blocks where PyYAML found {parsed} multi-line "
        f"runs — the scanner is missing blocks, which is the direction that hides a defect"
    )


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
