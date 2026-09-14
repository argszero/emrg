"""git's answer to "what does merging these two commits produce" — asked once.

Every gate in this family (`check-merge-*.py`) rests on the same two facts, and
both are git's, not theirs:

1. **A merge either names its tree or was not answered.** `git merge-tree
   --write-tree A B` prints the merged tree's OID on the first line of stdout for
   a clean merge *and* for a conflicting one, exits 0 for a clean merge (and for
   `--quiet`, printing nothing) and 1 both for a conflict and for a failure to
   merge the two *inputs* (which prints nothing at all). The exit code therefore
   separates none of these; the named tree separates all of them. Reading `[]` -
   the clean answer - out of an unnamed merge is the one direction a gate must
   never invent, because the next step then judges a tree that was never built.

2. **The conflicted paths come from the stage block, decoded.** `merge-tree`
   writes the block first, one line per side per conflicted path, then a blank
   line, then prose. Measured 2026-09-14 (`cyc20260914-104220`, git 2.50.1, in
   scratch repos - every row below read against the paths the merged *inputs*
   actually contain), the paths git writes in the *block* are its **spelling** of
   the name - C-quoted for any path holding a non-ASCII byte, a quote, a backslash
   or a control byte - and the paths it writes in the *prose* are real but exist
   only for the conflict kinds whose message happens to be `Merge conflict in
   <path>`. Three readings have been live in this family; the middle one is the
   interesting failure, because it is right wherever the other two agree:

       arm                              prose     any line    this module
                                        "…in X"   with a tab  (block + decode)
       -------------------------------  --------  ----------  ---------------
       plain content conflict           `plain.txt` `plain.txt` `plain.txt`
       binary content conflict          `bin.dat`   `bin.dat`   `bin.dat`
       add/add                          `new.txt`   `new.txt`   `new.txt`
       non-ASCII `中文.txt`              `中文.txt`  *quoted*    `中文.txt`
       a TAB `f<TAB>tab.txt`            `f<TAB>…`   *`tab.txt`* `f<TAB>tab.txt`
       a quote `q"uote.txt`             `q"uote…`   *quoted*    `q"uote.txt`
       modify/delete (a rename too)     *sentence*  `gone.txt`  `gone.txt`
       a failure to merge the inputs    (nothing)   (nothing)   (nothing)
       a clean merge                    (nothing)   (nothing)   (nothing)

       *the reading is wrong here: the name it prints is not one git was talking
        about (`git cat-file -e <side>:<that name>` is false), so a refusal built
        on it sends a resolver after a file nobody has - and for the TAB arm the
        "any line with a tab" split invents a second one, `tab.txt`
        **the regex does not match, so the fallback returns the whole sentence,
        which then travels as if it were a path

   Neither of the prose-shaped readings alone is complete, and each is wrong in the
   *other* direction: the block is always there and always structured but is quoted;
   the prose is already decoded but names the paths only sometimes. So the one
   complete reading is the block **plus** `unquote_path`, and that is what this
   module does.

Why this module exists at all
-----------------------------
These two facts used to be implemented in **five** gates, three of them with a
copy of the reading, each copy blind to a different arm - so every repair (five
PRs between #1210 and #1216) fixed the arm its own instance could see while the
other copies stayed blind. A rule with five implementations is five rules, and no
amount of repairing them one at a time converges. The rule lives here once; the
gates ask for it. `tests/test_merge_tree_is_the_only_reading.py` is what keeps it
that way - a sixth copy fails there instead of surviving until someone reads a
file that is not the file they were told to resolve.

What this module deliberately does *not* decide
-----------------------------------------------
What an unmeasurable question *means* downstream: one gate refuses to name a
conflict, another aborts the plan, a third says "reporting only". It reports the
facts and lets each caller map them to its own verdict.

One mapping it does own, because the callers had *drifted* on it: **which of the
three answers is a tree**. `merged_tree` (and `merge_commit` on top of it) exists
because two callers wrote that mapping as "a tree was named, so here it is" -
`merged_tree_sha` for a conflicting merge, `check-merge-landing-diff.py` for an
answer it did not understand - and a conflicting merge *does* name a tree, whose
file is the conflict with its markers (measured 2026-09-14, `cyc20260914-114057`).
Only `verdict == "clean"` is a tree; the rest is `None` or a raise.

The runner (`run`) is the caller's: every gate pins its own locale/env/error
discipline, and its tests replace it, so this module never owns a subprocess.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence


class MeasurementError(Exception):
    """The question could not be answered. Never a verdict."""


#: `100644 <blob> <stage>\t<path>` - one line per side per conflicted path, stage
#: 1/2/3 being base/ours/theirs. A content conflict writes three (one per stage),
#: a modify/delete writes two - the count is per side that has a blob, not fixed.
#: The lookahead-free shape is the rule: the prose after the block is not it.
_STAGE_LINE = re.compile(r"^[0-7]{6} [0-9a-f]+ [123]\t(?P<path>.+)$")

#: The escapes git's path quoting uses - `quote_c_style`'s set, as bytes.
_C_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "f": 0x0C,
    "n": 0x0A,
    "r": 0x0D,
    "t": 0x09,
    "v": 0x0B,
    "\\": 0x5C,
    '"': 0x22,
}

#: Author/committer date for the synthetic fold commits below. Pinned to a fixed
#: instant because a merge commit's *tree* is what these gates compare, and a
#: machine- or clock-dependent sha would make two runs of the same measurement
#: disagree for no reason (`cyc20260913-200715`). One constant, one place: the
#: guards read it from here.
PLAN_COMMIT_DATE = "2000-01-01T00:00:00 +0000"


def is_object_name(line: str) -> bool:
    """Whether a line is a bare object name (a tree's or a commit's OID).

    Both the SHA-1 (40 hex) and SHA-256 (64 hex) object formats are accepted: the
    shape of the answer must not depend on the clone, and `git init
    --object-format=sha256` clones exist.
    """
    return bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", line))


def unquote_path(path: str) -> str:
    """The path git *means*, from the path git wrote.

    `merge-tree` quotes a path that holds a quote, a backslash, a control byte, or
    - with the default `core.quotePath=true` - a non-ASCII byte, C-style:
    `f<TAB>tab.txt` arrives as `"f\\ttab.txt"` and `中文.txt` as
    `"\\344\\270\\255\\346\\226\\207.txt"` (measured 2026-09-14, git 2.50.1, in
    scratch repos). Passed through as written, both name a file nobody has, and
    the refusal that prints them sends a resolver after a file that does not exist.

    Decoding here also makes the path independent of the *reader's* locale: the
    escapes are ASCII, so the real bytes are reassembled by this function rather
    than by whatever encoding the subprocess reader happened to pin.
    """
    if len(path) < 2 or not (path.startswith('"') and path.endswith('"')):
        # Unquoted: git wrote the path's bytes as they are (with
        # `core.quotePath=false`, or a path that needed no quoting at all).
        return path
    body = path[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out.extend(char.encode("utf-8"))
            i += 1
            continue
        i += 1
        if i >= len(body):
            # A lone trailing backslash: not a quoted path after all. Keep it.
            out.extend(b"\\")
            break
        escape = body[i]
        if escape in _C_ESCAPES:
            out.append(_C_ESCAPES[escape])
            i += 1
            continue
        if escape in "01234567":
            digits = body[i : i + 3]
            if len(digits) < 3 or any(d not in "01234567" for d in digits):
                # A partial octal escape is not one; keep the bytes as written.
                out.extend(escape.encode("utf-8"))
                i += 1
                continue
            out.append(int(digits, 8) & 0xFF)
            i += len(digits)
            continue
        out.extend(escape.encode("utf-8"))
        i += 1
    # `errors="replace"`: the bytes may be any encoding, and the reader that
    # produced `path` was already pinned to UTF-8. A name outside UTF-8 degrades
    # the same way here as it would anywhere else in these tools.
    return out.decode("utf-8", errors="replace")


def stage_block_paths(lines: Sequence[str]) -> list[str]:
    """The conflicted paths a merge report's stage block names, decoded.

    **The stage block, not "every line with a tab in it".** Measured, a conflict
    in a file named `f<TAB>tab.txt` reports

        100644 <blob> 1\t"f\\ttab.txt"       <- the stage block: git's spelling
        ...
        Auto-merging f<TAB>tab.txt           <- prose: a real tab, not a separator
        CONFLICT (content): Merge conflict in f<TAB>tab.txt

    and the "any line with a tab" reading answered `"f\\ttab.txt", tab.txt` for
    it: one name nobody can open, and one file that collides with nothing. The
    blank line git writes after the block is where the prose begins, but it is not
    what excludes it - the shape is the only rule, and it is the one the tests
    weaken to prove it is load bearing.

    One entry per stage line, in the order git wrote them: stages 1/2/3 of the
    same path appear once per stage, which is what lets a rename conflict name
    every side. Callers that print a count dedupe.
    """
    paths: list[str] = []
    for line in lines[1:]:  # lines[0] is the merged tree's name
        match = _STAGE_LINE.match(line)
        if match:
            paths.append(unquote_path(match.group("path")))
    return paths


@dataclass(frozen=True)
class Fold:
    """What `git merge-tree --write-tree A B` answered, read once.

    `tree` is the merged tree's OID when git named one, `None` when it did not -
    and that is the whole of "was the question answered", because the exit code
    does not carry it (see this module's docstring). `paths` are the conflicted
    paths, decoded, empty when the merge is clean or when git named none.
    """

    code: int
    stdout: str
    stderr: str
    tree: str | None
    paths: tuple[str, ...]

    @property
    def answered(self) -> bool:
        return self.tree is not None

    @property
    def verdict(self) -> str:
        """`"clean"`, `"conflict"` or `"unmeasured"` - the only three answers.

        The exit code is read *after* the named tree, never before it: 0 with no
        tree is not a clean merge (that is `--quiet`, or a failure that printed
        nothing), and 1 with no tree is not a conflict (that is a failure to merge
        the inputs). Anything else with a tree named is a code this module does not
        know, so it is unmeasured rather than guessed at.
        """
        if not self.answered:
            return "unmeasured"
        if self.code == 0:
            return "clean"
        if self.code == 1:
            return "conflict"
        return "unmeasured"

    @property
    def diagnosis(self) -> str:
        """The tail of what git said, for a caller that has to explain itself."""
        detail = (self.stdout[-500:] + self.stderr[-500:]).strip()
        return detail or f"no output (exit {self.code})"


def fold(a: str, b: str, run: Callable[..., subprocess.CompletedProcess], cwd: str | None = None) -> Fold:
    """Ask git what merging `a` and `b` produces.

    `run(argv)` (or `run(argv, cwd)` when a cwd is given) is the caller's runner
    and pins locale/env/decoding. Nothing is written: the working tree is never
    touched, which is the property that lets these gates run inside a review.
    """
    argv = ["git", "merge-tree", "--write-tree", a, b]
    proc = run(argv) if cwd is None else run(argv, cwd)
    lines = proc.stdout.splitlines()
    named = lines[0].strip() if lines else ""
    tree = named if is_object_name(named) else None
    return Fold(
        code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        tree=tree,
        paths=tuple(stage_block_paths(lines)) if tree is not None else (),
    )


def merged_tree(
    a: str,
    b: str,
    run: Callable[..., subprocess.CompletedProcess],
    cwd: str | None = None,
) -> str | None:
    """The tree a clean merge of `a` and `b` produces, or `None` when it conflicts.

    **A conflicting merge names a tree too.** Measured 2026-09-14
    (`cyc20260914-114057`, git 2.50.1, scratch repo): `git merge-tree --write-tree
    master side` exits 1 and names `740ed768...`, and the blob it puts at the
    conflicted path is the two sides' text with git's conflict markers between them.
    (Not quoted here: `tests/test_conflict_markers.py` reads any line of a tracked
    file that begins with a marker as a marker somebody committed -
    `test_a_conflicting_merge_names_a_tree_full_of_markers` in
    `tests/test_merge_tree.py` re-measures it on real git instead.)

    So "git named a tree" is not "git merged". A caller that reads the tree out of a
    conflicting answer measures a file full of markers - and a guard that never opens
    that file reports the merge as healthy. Only `verdict == "clean"` is a tree;
    a conflict is `None`; an answer this module does not understand raises (see
    `Fold.verdict` for why an unknown code is not a merge).

    Two callers had written this mapping themselves and drifted from it -
    `merged_tree_sha` and `check-merge-landing-diff.py`, each of which handed back
    the tree of an answer that was not a merge - which is why the mapping lives here
    rather than in the callers that ask.
    """
    answer = fold(a, b, run=run, cwd=cwd)
    if answer.verdict == "clean":
        return answer.tree
    if answer.verdict == "conflict":
        return None
    raise MeasurementError(
        f"merge-tree exited {answer.code}"
        + (
            ""
            if answer.answered
            else " without naming a merged tree, so this is not a conflict but a "
            "failure to merge the inputs"
        )
        + ": " + answer.diagnosis
    )


def merged_tree_sha(
    a: str,
    b: str,
    run: Callable[..., subprocess.CompletedProcess],
    cwd: str | None = None,
) -> str:
    """The tree of the **clean** merge of `a` and `b`, or raise `MeasurementError`.

    For a caller that has no answer to give without it. Two answers are not a tree
    here: a **conflict** names a tree whose content is the conflict with its markers
    (measured - see `merged_tree`), which is not what this function's name promises
    to the caller that measures it, and an unmeasured answer is not a merge at all.
    `MeasurementError` rather than a falsy value: an unhandled exception used to
    leave these tools as exit 1, the code that *means* "the tree this step lands is
    unhealthy" - a crash reported as a finding about a tree nobody measured.
    """
    tree = merged_tree(a, b, run=run, cwd=cwd)
    if tree is None:
        raise MeasurementError(
            "the merge conflicts, so there is no clean-merge tree to measure"
        )
    return tree


def commit_env() -> dict[str, str]:
    """The environment that pins the identity and date of a synthetic commit.

    Measured in this family (`cyc20260913-200715`): with no ambient identity and
    `user.useConfigOnly = true`, `git commit-tree` refuses, and a gate reports a
    *false* "could not measure" about a question the machine's git config has no
    bearing on.
    """
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "emrg-merge-gate",
        "GIT_AUTHOR_EMAIL": "merge-gate@emrg.invalid",
        "GIT_COMMITTER_NAME": "emrg-merge-gate",
        "GIT_COMMITTER_EMAIL": "merge-gate@emrg.invalid",
        "GIT_AUTHOR_DATE": PLAN_COMMIT_DATE,
        "GIT_COMMITTER_DATE": PLAN_COMMIT_DATE,
    }


def commit_tree(
    tree: str,
    parents: Sequence[str],
    message: str,
    run: Callable[..., subprocess.CompletedProcess],
    env: dict[str, str] | None = None,
) -> str:
    """Wrap a merged tree in a real commit, so it can be one side of the next merge.

    `merge-tree` yields a tree, and a tree cannot serve as a parent; every gate
    that folds a *plan* needs the intermediate results as commits. `env` carries
    the pinned identity (`commit_env`); it is passed to the runner as a keyword.
    """
    argv = ["git", "commit-tree", tree]
    for parent in parents:
        argv += ["-p", parent]
    argv += ["-m", message]
    proc = run(argv, env=env) if env is not None else run(argv)
    if proc.returncode != 0:
        raise MeasurementError(f"commit-tree failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def merge_commit(
    a: str,
    b: str,
    run: Callable[..., subprocess.CompletedProcess],
    message: str | None = None,
    cwd: str | None = None,
) -> str | None:
    """The merge of commits `a` and `b` as a commit, or `None` if it conflicts.

    Unmeasurable raises (never `None`, which is the conflict answer). The result
    is exactly what a merge would have produced, without touching the working
    tree - the uncommitted-repair trap these gates exist to avoid.

    An **unmeasured** merge raises too, even when it named a tree: a code this
    module does not know is git saying something it has not taught us, and wrapping
    that tree in a commit would turn "I do not understand this answer" into "here is
    the merge" - the same trade in the reassuring direction that the named-tree rule
    exists to refuse. That rule is `merged_tree`'s, so it is written once.
    """
    tree = merged_tree(a, b, run=run, cwd=cwd)
    if tree is None:
        return None
    return commit_tree(
        tree,
        [a, b],
        message if message is not None else f"merge {b[:8]} into {a[:8]}",
        run=run,
        env=commit_env(),
    )
