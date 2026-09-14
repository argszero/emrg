"""One writer for `rants.jsonl`, and it is a tool.

Why this file exists
--------------------
`submit_rant` (four actions: submit / list / update / cleanup) was made the ONLY
writer of `~/.emrg/rants.jsonl` by rant 2026-08-18T16:42:52, and `emrg/server/rants.py`
says so in its own docstring. The reason is recorded there: hand-written rewrites of
that file had already drifted into array rows, lost fields and pruned history
(2026-08-18 incident).

The task templates did not follow. Measured 2026-09-14 (cyc20260914-180702):
`promote_prompt.md` carried a `rants_file = os.path.expanduser(...)` snippet that
opened the file for writing, restated the field order, the sort and the
`ensure_ascii=False` rule the tool already owns, and never named the tool at all —
so a task collecting community feedback was pointed at the one write path the tool
replaced. `paper_prompt.md` still does the same thing, and goes further: it tells
the agent to write `status = "acknowledged"`, a value the state machine
(`pending → in_progress → completed`) does not contain (`emrg/server/rants.py`).
Both were deleted from `evolution_prompt.md`'s own copy of the rule, which is why
the rule surviving here was invisible.

Named limit: this is a *text* guard over the built-in task templates, and it covers
**writes** only. The evolution task's own `cat ~/.emrg/rants.jsonl` scan (in
`evolution_prompt.md`, which normal evolution must not edit) is a read and is out of
scope here; the write path is the one that corrupted the file.
"""

from __future__ import annotations

import re
from pathlib import Path

from emrg.server.scheduler import TASK_TEMPLATES

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "emrg" / "server"

# Templates still teaching a hand-written `rants.jsonl` write. Each entry is
# removed in the SAME change that routes its template through `submit_rant`, so
# the set only shrinks: empty is what "one writer" means.
#
# `paper_prompt.md` is the remaining one and it is owned by PR #1226 (the
# state-file sweep), which is why it is fixed by whoever lands the paper half
# rather than here.
PENDING_RANT_WRITER_SWEEP = {"paper_prompt.md"}

# The write's fingerprint: the variable the hand-written snippets use to name the
# file, or an `open(...rants.jsonl..., "w"/"a")` in any spelling. A template that
# merely *mentions* rants.jsonl (reading it, or handing off to the tool) is legal
# — the corpus has several.
_RANT_FILE_WRITE = re.compile(
    r"rants_file|open\([^)\n]*rants\.jsonl[^)\n]*,\s*[\"'](?:w|a)[\"']",
    re.IGNORECASE,
)


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


def test_no_template_teaches_a_hand_written_rants_write() -> None:
    """Every built-in template that touches the file must go through the tool."""
    templates = {name for _task_type, name in TASK_TEMPLATES.items()}
    assert len(templates) >= 5, f"only {len(templates)} templates found — scan broken"
    swept = sorted(templates - PENDING_RANT_WRITER_SWEEP)
    assert "promote_prompt.md" in swept, (
        "the swept set lost the pilot template — the check is not looking where it "
        "thinks it is"
    )

    for name in swept:
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        hit = _RANT_FILE_WRITE.search(text)
        assert hit is None, (
            f"{name}: teaches a hand-written write of rants.jsonl "
            f"({hit.group(0)!r} at offset {hit.start()}) — `submit_rant` is the only "
            f"writer (rant 2026-08-18T16:42:52); it stamps the timestamp, fixes the "
            f"field order and the sort, and writes with ensure_ascii=False, so a "
            f"snippet here only re-states what the tool owns and can drift again"
        )

    for name in sorted(PENDING_RANT_WRITER_SWEEP):
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        assert _RANT_FILE_WRITE.search(text), (
            f"{name} is listed in PENDING_RANT_WRITER_SWEEP but no longer teaches a "
            f"hand-written write — drop it from the set in the same change that "
            f"routed it through submit_rant, so the set keeps meaning 'not yet done'"
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
    for _task_type, name in TASK_TEMPLATES.items():
        if name in PENDING_RANT_WRITER_SWEEP:
            continue
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        for m in re.finditer(r"status[\"']?\s*[:=]\s*[\"']([a-z_]+)[\"']", text):
            assert m.group(1) in statuses, (
                f"{name}: writes rant status {m.group(1)!r}, which the store does "
                f"not accept ({sorted(statuses)})"
            )
