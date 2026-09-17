"""One writer for `rants.jsonl`, and it is a tool.

Why this file exists
--------------------
`submit_rant` (four actions: submit / list / update / cleanup) was made the ONLY
writer of `~/.emrg/rants.jsonl` by rant 2026-08-18T16:42:52, and `emrg/server/rants.py`
says so in its own docstring. The reason is recorded there: hand-written rewrites of
that file had already drifted into array rows, lost fields and pruned history
(2026-08-18 incident).

The task templates did not follow. Measured 2026-09-14 (cyc20260914-180702 and
cyc20260914-181706): `promote_prompt.md` carried a `rants_file = os.path.expanduser(...)`
snippet that opened the file for writing, restated the field order, the sort and the
`ensure_ascii=False` rule the tool already owns, and never named the tool at all —
so a task collecting community feedback was pointed at the one write path the tool
replaced. `paper_prompt.md` did the same thing and went further: it told the agent to
write `status = "acknowledged"`, a value the state machine
(`pending → in_progress → completed`) does not contain (`emrg/server/rants.py`), so
the file it produced could not be moved by the tool that owns it. Both were deleted
from `evolution_prompt.md`'s own copy of the rule, which is why the rule surviving
here was invisible.

An earlier revision of this file left `paper_prompt.md` in a `PENDING_RANT_WRITER_SWEEP`
set on the stated grounds that PR #1226 (the state-file sweep) owned the paper half.
That was wrong, and measurably so: `git grep -n rants_file FETCH_HEAD` on #1226's own
head (`e044922d`) finds the snippet untouched — #1226 sweeps the state/reflection
mechanism, not the rant write path. A guard comment naming an owner that does not own
the thing is the same defect class as the snippets it guards, so the two halves were
fixed in the change that emptied the set. The set is gone rather than left empty:
with no pending template it asserted nothing, and a scan with an exception list is a
scan whose coverage can silently shrink.

That last sentence is then in tension with `_UNEDITABLE_TEMPLATES`, which is an
exception list, so the difference has to be stated rather than assumed. The pending-set
was "these lines are still wrong and someone else should fix them" — a claim about
ownership that was false, and one that shrinks the guard's *coverage* while looking
like a fix. `_UNEDITABLE_TEMPLATES` is "this file cannot be changed by routine
evolution at all" (host rant 2026-08-17T14:22:21), so no change to this guard could
sweep it. Three things keep it from becoming the first kind, and a test asserts each:
the set is pinned to exactly one name, that name must still be in the corpus, and the
exemption must still be **needed** (if the template is ever cleaned the test fails and
says to remove the exclusion).

Named limit: this is a *text* guard over the built-in task templates, and the
`cat ~/.emrg/rants.jsonl` read recipes (in `evolution_prompt.md`, which normal
evolution must not edit, and in the review steps of the other templates) are
**legal** here: they name the file without a mutating verb, and a read is not what
corrupted it. The read half does cover the one shape that is decidable — a template
that forbids opening the file, *even to read it*, while handing the reader a recipe
that opens it (measured 2026-09-17 on `paper_prompt.md`; see
`test_no_template_forbids_opening_the_rant_file_while_handing_out_a_read_recipe`).

A second, narrower limit found while widening it this cycle (cyc20260914-201054): the
instruction scan reads **line by line**, so a sentence that names the file and its
mutating verb in separate lines is invisible to it, and the vocabulary is a list of
verbs rather than a grammar. That is a real hole, and it is the hole chosen
deliberately: a sentence-level parse of task prose cannot be made reliable here, and
an unreliable guard that claims wide coverage is worse than a narrow one that states
its width. The compensating control is the line-level vocabulary itself — `mark`,
`update`, `write`, `sort`, `edit`, `curate`, `prune` and their inflections cover
every spelling of the write found in the corpus, and the mutation arms plant each.
"""

from __future__ import annotations

import re
from pathlib import Path

from emrg.server.scheduler import TASK_TEMPLATES

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "emrg" / "server"

# The write's fingerprint: the variable the hand-written snippets use to name the
# file, or an `open(...rants.jsonl..., "w"/"a")` in any spelling. A template that
# merely *mentions* rants.jsonl (reading it, or handing off to the tool) is legal
# — the corpus has several.
_RANT_FILE_WRITE = re.compile(
    r"rants_file|open\([^)\n]*rants\.jsonl[^)\n]*,\s*[\"'](?:w|a)[\"']",
    re.IGNORECASE,
)

# The status write's fingerprint, in the three spellings a Python snippet can use:
# `"status": "x"`, `status = "x"`, and the subscript `r["status"] = "x"`. The last
# one is the spelling the deleted `paper_prompt.md` snippet actually used, and the
# first version of this guard did not match it (measured 2026-09-14, cyc20260914-181706:
# reintroducing `r["status"] = "acknowledged"` left the guard green) — a guard whose
# self-test covered the spellings that happened to be convenient.
_RANT_STATUS_WRITE = re.compile(
    r"(?:\[\s*[\"']status[\"']\s*\]|\bstatus[\"']?)\s*[:=]\s*[\"']([a-z_]+)[\"']"
)

# The same write in its *instruction* form, which no snippet pattern can see: prose
# that names the file and tells the agent to change it. Measured 2026-09-14
# (cyc20260914-201054): with the snippets deleted, `paper_prompt.md` still said
# "always read all entries, sort by timestamp, and write back to rants.jsonl" — 54
# lines below the section that had just been swept — and `open_source_prompt.md`
# still said "mark the rant `in_progress` in `~/.emrg/rants.jsonl`", 99 lines below
# the line whose rule it restates. The snippet guard was green over both, which is
# what a guard written against one spelling of a defect always is.
#
# The line is the unit because the defect and its repair are both one line of
# config prose, and the repair has to survive the scan: the replacement sentences
# that were written this cycle ("Every move goes through `submit_rant` ... the only
# writer of `rants.jsonl`", "move the rant with `submit_rant(action=\"update\", ...)
# — never by editing `rants.jsonl`") name the file and carry a mutating verb too.
# So naming `submit_rant` is what makes a line legal — it hands the write to the
# tool — and the bypass clause is what stops that from being a loophole: a line
# that names the tool *and* offers a way around it ("if `submit_rant` is
# unavailable, edit `rants.jsonl` by hand") is exactly the fallback sentence #1229
# deleted, and it must still fail. A line that only *reads* the file (`cat
# ~/.emrg/rants.jsonl`) is legal, which is why the filename alone is not enough.
_RANT_MUTATING_VERB = re.compile(
    r"\b(?:mark\w*|updat\w*|rewrit\w*|modif\w*|edit\w*|append\w*|sort\w*"
    r"|rebuild\w*|curat\w*|prun\w*|write|writes|writing|wrote|written)\b",
    re.IGNORECASE,
)

_RANT_WRITE_BYPASS = re.compile(
    r"instead of|unavailable|not available|does not work|is broken|fails|failed"
    r"|if the tool|fallback|degrade to|without the tool",
    re.IGNORECASE,
)

# A rule the tool owns, restated as prose. This is not pedantry about duplication:
# the agent cannot tell that the copy has drifted from the implementation, and the
# copy is what produced `status = "acknowledged"` in a file the tool then refused
# to move. The field order, the sort and `ensure_ascii=False` are the store's.
_RANT_RULE_RESTATED = re.compile(
    r"read\s+all\s+entries"
    r"|sort\s+by\s+`?timestamp"
    r"|field\s+order\s+MUST"
    r"|JSON\s+line'?s\s+field\s+order"
    r"|json\.dumps\(\.\.\.,\s*ensure_ascii=False\)",
    re.IGNORECASE,
)

# The direct-read fingerprint: a line that hands the agent the *file*, rather than the
# tool. Two forms exist in the corpus — the shell recipe (`cat ~/.emrg/rants.jsonl`, in
# three templates), and the prose that names the path as where the feedback is read
# from (`paper_prompt.md:107` until 2026-09-17, quoted in the self-test below).
_RANT_DIRECT_READ = re.compile(
    r"cat\s+[^\s`'\"]*rants\.jsonl"
    r"|\bread(?:ing|s)?\b[^\n]{0,40}\bfrom\s+`[^`\n]*rants\.jsonl",
    re.IGNORECASE,
)

# The read-denial claim, as the tool's single-access rule grew into it in
# `paper_prompt.md` and `promote_prompt.md`: "there is nothing to write by hand — and
# no reason to open the file at all, not even to read it". The absolute half ("not even
# to read it") is what a template may not say while telling the phase to read the file.
_RANT_READ_DENIAL = re.compile(
    r"no\s+reason\s+to\s+open\s+the\s+file\s+at\s+all|not\s+even\s+to\s+read\s+it",
    re.IGNORECASE,
)

# The one template the scans above do not read, with the reason it cannot be
# fixed the way the others were. It is a *real* owner, unlike the pending-set entry
# this guard started with (which named #1226, a PR that never touched the snippet):
# `evolution_prompt.md` is the stable evolution template, and routine evolution is
# forbidden to edit it (host rant 2026-08-17T14:22:21) — its §6 restates the same
# rules and can only be changed through a prompt-specific rant. The set is pinned
# to exactly this name, and a test asserts the exclusion is still *needed*, so it
# cannot silently grow into a list of things someone did not want to fix.
_UNEDITABLE_TEMPLATES = {"evolution_prompt.md"}


def _template_names() -> list[str]:
    """The corpus under guard, with the scan's own health asserted here once."""
    names = sorted({name for _task_type, name in TASK_TEMPLATES.items()})
    # Every named template must exist; a renamed file would otherwise be scanned
    # as "" and pass by not being found.
    assert len(names) >= 5, f"only {len(names)} templates found — scan broken"
    for name in names:
        assert (PROMPTS_DIR / name).is_file(), f"{name} is not a file in {PROMPTS_DIR}"
    return names


def _scanned_templates() -> list[str]:
    """The corpus minus the one template this guard may not hold to account."""
    return [n for n in _template_names() if n not in _UNEDITABLE_TEMPLATES]


def _instructs_a_hand_written_write(line: str) -> bool:
    """Does this line tell the agent to change `rants.jsonl` itself?

    Three conditions, in order: the line names the file, it carries a mutating
    verb, and it does not hand the write to `submit_rant`. The third is what keeps
    the scan usable — see the comment on `_RANT_MUTATING_VERB` — and the bypass
    clause is what keeps the third from being a way out of the guard.
    """
    if "rants.jsonl" not in line:
        return False
    if not _RANT_MUTATING_VERB.search(line):
        return False
    if "submit_rant" in line and not _RANT_WRITE_BYPASS.search(line):
        return False
    return True


def test_the_detector_detects_the_shape_it_was_written_for() -> None:
    """A guard that silently stops matching reports success by not looking."""
    for planted in (
        'rants_file = os.path.expanduser("~/.emrg/rants.jsonl")',
        'with open(rants_file, "w", encoding="utf-8") as f:',
        'open("~/.emrg/rants.jsonl", "a")',
    ):
        assert _RANT_FILE_WRITE.search(planted), planted
    for legal in (
        'cat ~/.emrg/rants.jsonl 2>/dev/null || echo "[no rants.jsonl — skip]"',
        'submit_rant(action="submit", project="emrg", message="...")',
        "Every cycle must curate `~/.emrg/rants.jsonl`.",
    ):
        assert not _RANT_FILE_WRITE.search(legal), legal


def test_the_swept_corpus_covers_every_builtin_template() -> None:
    """The two guards below scan the whole corpus; say so out loud.

    Both the write check and the status check were once narrowed by a pending-set
    exception, and the one template excluded from them was the one still wrong.
    """
    names = _template_names()
    for pilot in ("promote_prompt.md", "paper_prompt.md"):
        assert pilot in names, f"{pilot} left the corpus — the check moved off its target"


def test_no_template_teaches_a_hand_written_rants_write() -> None:
    """Every built-in template that touches the file must go through the tool."""
    for name in _template_names():
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        hit = _RANT_FILE_WRITE.search(text)
        assert hit is None, (
            f"{name}: teaches a hand-written write of rants.jsonl "
            f"({hit.group(0)!r} at offset {hit.start()}) — `submit_rant` is the only "
            f"writer (rant 2026-08-18T16:42:52); it stamps the timestamp, fixes the "
            f"field order and the sort, and writes with ensure_ascii=False, so a "
            f"snippet here only re-states what the tool owns and can drift again"
        )


def test_no_template_writes_an_off_schema_rant_status() -> None:
    """`acknowledged` is not a status the store accepts.

    `_ALLOWED_STATUS_TRANSITIONS` in `emrg/server/rants.py` has three states
    (pending → in_progress → completed); `update_rant` returns an error for anything
    else. A hand-written snippet that sets `"acknowledged"` therefore writes a file
    the tool then refuses to move — the exact "invalid status" shape the unified tool
    exists to prevent.
    """
    statuses = {"pending", "in_progress", "completed"}
    for name in _template_names():
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        for m in _RANT_STATUS_WRITE.finditer(text):
            assert m.group(1) in statuses, (
                f"{name}: writes rant status {m.group(1)!r} at offset {m.start()}, "
                f"which the store does not accept ({sorted(statuses)})"
            )


def test_the_status_detector_covers_every_spelling_a_snippet_can_use() -> None:
    """The three ways a snippet names the field, and one legal way to mention it.

    The miss this test exists for was real: `paper_prompt.md` wrote
    `r["status"] = "acknowledged"`, the detector only understood `status = "x"` and
    `"status": "x"`, and the template was excluded from the scan by a pending-set —
    so the guard was green over the one line it was written for.
    """
    for planted in (
        '"status": "acknowledged"',
        "status = 'acknowledged'",
        'r["status"] = "acknowledged"',
        "r['status']='acknowledged'",
    ):
        m = _RANT_STATUS_WRITE.search(planted)
        assert m and m.group(1) == "acknowledged", planted
    for legal in (
        "`completed` is set only when status=completed",
        "- `status: active` (cycle in progress)",
        "the status machine is `pending → in_progress → completed`",
        'submit_rant(action="update", status="completed")',
    ):
        m = _RANT_STATUS_WRITE.search(legal)
        assert m is None or m.group(1) in {"pending", "in_progress", "completed"}, legal


def test_the_instruction_detector_separates_the_write_from_its_delegation() -> None:
    """Both arms, on the real lines — the defect's and its repair's.

    The positives are copied verbatim from master (`git show master:<template>`),
    not invented: these are the sentences that survived the snippet deletion and
    kept teaching the write. The negatives are the lines this cycle wrote to
    replace them. A detector tested only against strings it was designed from
    proves nothing about the corpus; these are the corpus.
    """
    for planted in (
        # master:emrg/server/paper_prompt.md:273
        "- When marking rants, always read all entries, sort by timestamp, and "
        "write back to rants.jsonl; do not change the chronological order",
        # master:emrg/server/open_source_prompt.md:286
        "- Before implementing: mark the rant `in_progress` in "
        "`~/.emrg/rants.jsonl` with a `progress` description (e.g. "
        '"implementing X (PR #N)")',
        # master:emrg/server/promote_prompt.md:236
        "Promotion is two-way. Community feedback gathered during promotion — "
        "**proactively write valuable items to rants.jsonl**",
        # master:emrg/server/evolution_prompt.md:253 (excluded template, but the
        # detector must still recognise the shape it is excluded for)
        "Every cycle must curate `~/.emrg/rants.jsonl`. Each rant has a "
        "three-state `status` + `progress` description:",
        # The fallback shape #1229 deleted: names the tool AND a way around it.
        "- If `submit_rant` is unavailable, edit `rants.jsonl` by hand",
        "- When `submit_rant` fails, rewrite `rants.jsonl` directly",
    ):
        assert _instructs_a_hand_written_write(planted), planted

    for legal in (
        # A read recipe: names the file, no mutating verb.
        'cat ~/.emrg/rants.jsonl 2>/dev/null || echo "[no rants.jsonl — skip]"',
        # The repaired read mandate (master:paper_prompt.md:107): names the file and
        # hands the *read* to the tool, which is why the write detector must keep
        # treating it as legal — and why the read contradiction needs its own scan.
        "Every cycle you MUST first review user feedback, and the queue is read "
        'through the tool — `submit_rant(action="list")` — not by opening '
        "`~/.emrg/rants.jsonl`",
        # The repairs written this cycle: names the file and a verb, hands off.
        "- Every move goes through `submit_rant` (`action=\"update\"`), the only "
        "writer of `rants.jsonl`: the sort, the field order and the on-disk "
        "encoding are its business, not a rule to restate here",
        "- Before implementing: move the rant with `submit_rant(action=\"update\", "
        "timestamp=\"<the rant's timestamp>\", status=\"in_progress\", "
        "progress=\"implementing X (PR #N)\")` — never by editing `rants.jsonl`",
        "- Move a rant only with the `submit_rant` tool (`action=\"update\"`): it is "
        "the only writer of `rants.jsonl` and it owns the file's shape",
        "all reads/writes of `~/.emrg/rants.jsonl` MUST go through the "
        "`submit_rant` tool's actions",
        "- Check the queue with `submit_rant(action=\"list\")` rather than by "
        "opening the file",
    ):
        assert not _instructs_a_hand_written_write(legal), legal


def test_no_template_instructs_a_hand_written_rants_write() -> None:
    """The instruction form of the write: prose that names the file and edits it.

    The snippet patterns above cannot see this shape, and the corpus proved it has
    one: a template can lose its Python snippet and keep the rule the snippet
    implemented, in prose, for a later round to follow by hand.
    """
    for name in _scanned_templates():
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            assert not _instructs_a_hand_written_write(line), (
                f"{name}:{lineno} tells the agent to write `rants.jsonl`: "
                f"{line.strip()[:120]!r}. `submit_rant` is the only writer (rant "
                f"2026-08-18T16:42:52); an instruction that names the file and a "
                f"mutating verb — without handing the write to the tool — is the "
                f"same write in a form no snippet pattern sees"
            )


def test_no_template_restates_a_rule_the_tool_owns() -> None:
    """The field order, the sort and the encoding belong to the store, not here.

    A restated rule is a copy that can disagree with the code path, and one of them
    did: the copy taught a status the store does not have.
    """
    for name in _scanned_templates():
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            hit = _RANT_RULE_RESTATED.search(line)
            assert hit is None, (
                f"{name}:{lineno} restates a rule `submit_rant` owns "
                f"({hit.group(0)!r}): {line.strip()[:120]!r}. The tool stamps the "
                f"timestamp, fixes the field order and the sort and writes with "
                f"ensure_ascii=False; a second copy is a copy that drifts"
            )


def test_the_read_detector_separates_the_read_recipe_from_the_read_denial() -> None:
    """Both halves of the read shape, on the lines the corpus really has.

    The contradiction is a *pair*, so each detector is pinned to the lines it must
    see: the denial sentence as `paper_prompt.md` and `promote_prompt.md` carry it
    (two lines of one sentence), and the read forms — the shell recipe, and the prose
    mandate `paper_prompt.md:107` carried until this change, quoted verbatim from
    master so the shape the guard was written for stays measured rather than
    remembered. The negatives are the repair and the lines that were already legal.
    """
    for planted in (
        # master:emrg/server/paper_prompt.md:202-203 — one sentence, two lines
        "nothing for this prompt to write by hand — and no reason to open the file at all,\n"
        "not even to read it.",
        "and no reason to open the file at all",
        "not even to read it",
    ):
        assert _RANT_READ_DENIAL.search(planted), planted
    for planted in (
        # master:emrg/server/paper_prompt.md:107 — the prose half of the contradiction
        "Every cycle you MUST first read user feedback from `~/.emrg/rants.jsonl`.",
        # the shell recipes in the journal / open_source / evolution templates
        'cat ~/.emrg/rants.jsonl 2>/dev/null || echo "[no rants.jsonl — skip rant scan]"',
        "cat ~/.emrg/rants.jsonl",
    ):
        assert _RANT_DIRECT_READ.search(planted), planted
    for legal in (
        # The repair: the mandate names the tool, and the file only as the store.
        "Every cycle you MUST first review user feedback, and the queue is read "
        'through the tool — `submit_rant(action="list")` — not by opening '
        "`~/.emrg/rants.jsonl`",
        '- Check the queue with `submit_rant(action="list")` rather than by opening '
        "the file",
        "all reads/writes of `~/.emrg/rants.jsonl` MUST go through the `submit_rant` "
        "tool's actions",
    ):
        assert not _RANT_DIRECT_READ.search(legal), legal


def test_no_template_forbids_opening_the_rant_file_while_handing_out_a_read_recipe() -> None:
    """The read half of the same rule, in the one shape that is decidable.

    Measured 2026-09-17: `paper_prompt.md` sent the phase to read user feedback *from*
    `~/.emrg/rants.jsonl` (:107) and, 95 lines later, said there is no reason to open
    the file at all, "not even to read it" (:202-203) — while the same file twice sends
    the reader to the tool (:209, :255). A phase cannot both open the file and be told
    there is nothing to open it for; rant 2026-08-18T16:42:52 made `submit_rant` the
    single access point, and its `list` action is the queue view, so the mandate was
    the stale half and was repaired rather than the denial being deleted.

    Only the *contradiction* is scanned, not reads: the `cat` recipe three templates
    use is legal on its own — they never claim the file is not to be opened — and that
    limit is named in this module's docstring instead of being left to be inferred.

    The second half keeps the scan honest: if no template makes the denial claim any
    more, this test asserts nothing about the corpus, and it says so rather than
    staying green over nothing. Its limit is the write detector's limit, named rather
    than implied: the denial is a phrase vocabulary and not a grammar, so a template
    that forbids reading the file in some other spelling is invisible here — the shape
    the repair chose keeps the vocabulary wide (both halves of the sentence, which is
    how a reworded third spelling was still caught when this guard was mutation-tested).
    """
    claimers = []
    for name in _scanned_templates():
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        if not _RANT_READ_DENIAL.search(text):
            continue
        claimers.append(name)
        hit = _RANT_DIRECT_READ.search(text)
        assert hit is None, (
            f"{name}: forbids opening `rants.jsonl` even to read it, and hands out a "
            f"read recipe for the same file ({hit.group(0)!r} at offset {hit.start()}) "
            f"— the read path is `submit_rant(action=\"list\")` (rant 2026-08-18T16:42:52)"
        )
    assert claimers, (
        "no scanned template claims the file is not to be opened, so the scan above is "
        "vacuous — delete it rather than leaving a green guard over nothing"
    )


def test_the_excluded_template_is_excluded_because_it_cannot_be_edited() -> None:
    """The exception must name a real owner, and must still be needed.

    The pending-set this guard started with exempted the one template that still
    had the defect and named a PR that never touched it — a fake owner. This
    exclusion is the opposite: the file cannot be changed by routine evolution at
    all. So it is pinned to exactly one name, and the second half of the test
    asserts the exemption is doing something — if the prompt is ever cleaned, this
    fails and tells you to drop the exclusion rather than leaving a stale hole.
    """
    assert _UNEDITABLE_TEMPLATES == {"evolution_prompt.md"}, (
        "the exclusion set is not the single stable template — an exception list "
        "that grows is how the first version of this guard went green over the one "
        "line it was written for"
    )
    excluded = sorted(_UNEDITABLE_TEMPLATES)
    assert excluded[0] in _template_names(), (
        "the excluded template is no longer in the corpus — the exclusion is stale"
    )
    text = (PROMPTS_DIR / excluded[0]).read_text(encoding="utf-8")
    assert _RANT_RULE_RESTATED.search(text), (
        f"{excluded[0]} no longer restates a rule the tool owns, so the exclusion "
        f"in _UNEDITABLE_TEMPLATES is no longer needed — remove it (routine "
        f"evolution may not edit that template, so the clean-up needs a "
        f"prompt-specific rant)"
    )

