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

Named limit: this is a *text* guard over the built-in task templates, and it covers
**writes** only. The `cat ~/.emrg/rants.jsonl` read recipes (in `evolution_prompt.md`,
which normal evolution must not edit, and in the review steps of the other templates)
are out of scope here; the write path is the one that corrupted the file.
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


def _template_names() -> list[str]:
    """The corpus under guard, with the scan's own health asserted here once."""
    names = sorted({name for _task_type, name in TASK_TEMPLATES.items()})
    # Every named template must exist; a renamed file would otherwise be scanned
    # as "" and pass by not being found.
    assert len(names) >= 5, f"only {len(names)} templates found — scan broken"
    for name in names:
        assert (PROMPTS_DIR / name).is_file(), f"{name} is not a file in {PROMPTS_DIR}"
    return names


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

