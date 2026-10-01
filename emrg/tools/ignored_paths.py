"""Which paths a discovery walk visits — one rule, read by `glob` and `grep`.

Both discovery tools walk a tree and report what they find, and both skip paths
that would otherwise drown the answer. The skip used to be keyed on the path
alone, so a glob that **named** a hidden path still came back empty — while the
very same file was reachable through `read` and through `grep(path=…)`. Measured
2026-10-02 on master `bc114ab9`, in this repository, where each path exists:

===============================  ==========================================
``glob('.gitignore')``           ``No files matched pattern '.gitignore'``
``glob('**/.gitignore')``        ``No files matched pattern '**/.gitignore'``
``glob('.github/workflows/*.yml')``  ``No files matched pattern …``
``read('.gitignore')``           ``     1\t*.pyc``   — the file, right there
===============================  ==========================================

So a caller that named a path was told there was none, in the shape of a real
answer. The rule below restores the shell's own reading of the two dots: a
leading dot in a **pattern** is a request, and a leading dot in a **name** is
noise only until something asks for it.

What is still never walked, and why it is a decision rather than an oversight:
``.git`` is the repository's object store rather than a project file, and
``node_modules``/``__pycache__``/``.venv`` are build trees that can hold tens of
thousands of entries — a discovery walk that entered one on a pattern such as
``**/*`` would spend the answer's budget on it. Naming one of them in a pattern
is still answered without a walk, and `read`/`bash` remain the way to one
specific file inside. That list is stated once, here, so both tools read it.
"""

from __future__ import annotations

import fnmatch

#: Directory names no discovery walk enters, however they are spelled. Each is a
#: build or repository tree rather than project content, and each can be large
#: enough that walking one would spend the answer's budget on it.
NEVER_WALKED = (".git", "node_modules", "__pycache__", ".venv")

#: Hidden directories that are walked even when the pattern did not name them.
#: EMRG's own state lives under ``.emrg/`` (memory, sessions, logs); a discovery
#: walk that could not reach it could not read its own records. Measured
#: 2026-10-02: ``**/*.md`` lists ``.emrg/memory/*.md``, and must keep doing so.
ALWAYS_WALKED = (".emrg",)


def _asked_for(part: str, segments: list[str]) -> bool:
    """Whether `pattern`'s segments contain one that can match this hidden `part`.

    The shell's rule, stated where it is applied: a pattern segment that begins
    with a literal dot can match a name that begins with a dot, and one that
    begins with a wildcard cannot. ``.env`` and ``.*rc`` are requests for hidden
    names; ``*.env`` is not.
    """
    return any(
        segment.startswith(".") and fnmatch.fnmatchcase(part, segment)
        for segment in segments
    )


def is_ignored(rel_parts: tuple[str, ...], pattern: str) -> bool:
    """True when a discovery walk should not report the path `rel_parts`.

    :param rel_parts: the path's components relative to the walk's root.
    :param pattern: the glob the walk was given, spelled as the caller gave it.
    :returns: True when the path is noise for this walk.
    """
    segments = pattern.split("/")
    for part in rel_parts:
        if part in NEVER_WALKED:
            return True
        if part in ALWAYS_WALKED:
            continue
        if part.startswith(".") and not _asked_for(part, segments):
            return True
    return False
