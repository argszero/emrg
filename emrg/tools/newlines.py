"""A file's own line endings, kept across an edit.

Measured 2026-10-02 (issue #1803) on master `6b417c4c`, with the real tools in a temp
directory — a one-word edit to a three-line CRLF file:

```
before   : b'first\\r\\nsecond\\r\\nthird\\r\\n'
read tool: '     1\\tfirst\\n     2\\tsecond\\n     3\\tthird\\n     4\\t'
edit     : Made 1 replacement in .../install.cmd
after    : b'first\\nSECOND\\nthird\\n'      ← every terminator in the file changed
```

The cause is the round trip, not the replacement: `Path.read_text` applies
universal-newline translation, so an on-disk `\\r\\n` becomes `\\n` in the string the
tool matches against *and writes back*, and the file's own convention is nowhere in the
string that gets written. Two ends, lossy in opposite directions.

Why it matters: `.gitattributes` pins `*.cmd`/`*.bat`/`*.ps1` to `text eol=crlf`, so
those files are CRLF on every platform, and an LF-only `.cmd` is misparsed by `cmd.exe`
— the v0.2.25–v0.2.27 installer "exit code 1" series (rant 2026-08-12T12:30:41). An
agent adding one line to `bin/emrgd.cmd` converted the whole launcher to LF, and
`tests/test_cmd_crlf.py` only runs after the commit. The tool's plainer contract was
broken too: asked to replace one word, it reported one replacement and rewrote every
line of the file.

The rule
--------
An edit replaces the region `old_string` named and **nothing else**, and it is stated in
three parts:

* **both sides are read the way the reader reads them.** The text matched against is the
  universal-newline view — `\\r\\n`, a lone `\\r` and `\\n` all read as `\\n` — which is
  exactly what the read tool shows the model, so an `old_string` copied from a read keeps
  matching a CRLF file. `old_string` is read the same way, so a caller that hands over
  CRLF text matches the same file;
* **every byte outside the matched region is carried across untouched.** This is what
  keeps a file's terminators, its mixed endings and its stray `\\r` — the module never
  re-renders text it was not asked to change;
* **a line break inside `new_string` takes the terminator the file already uses**, decided
  by the breaks the file has (the most common spelling; a tie goes to the first one in the
  file, and a file with no line break at all gets `\\n`). So an edit does not introduce a
  terminator the file was not already using.

The third part is a decision, not a reading: a mixed file has no single convention, and
"the file's own" is the best available answer to which one a newly written line should
join. The choice is visible in one place (`terminator`) rather than spread through the
splice.
"""

from __future__ import annotations

#: The raw spellings a line break can have. The view is the same `\n` for all three,
#: which is why the mapping below has to carry width: `\r\n` is two bytes of the file
#: and one character of the view.
CRLF = "\r\n"
CR = "\r"
LF = "\n"


def view(text: str) -> tuple[str, list[tuple[int, int]]]:
    """`text` as a reader sees it, plus the raw span of each of its characters.

    The same translation Python's text mode applies (`\\r\\n` and a lone `\\r` each read
    as `\\n`), done here so the *raw* text can be spliced rather than re-rendered: the
    spans are what let a match found in the view be replaced in the file itself.

    :returns: ``(view_text, spans)`` — `spans[i]` is the half-open raw range the view's
        character `i` came from. A raw `\\r` that is not followed by `\\n` is one byte and
        still one view character, so the lengths differ and only this map relates them.
    """
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == CR:
            width = 2 if i + 1 < n and text[i + 1] == LF else 1
            chars.append(LF)
            spans.append((i, i + width))
            i += width
        else:
            chars.append(text[i])
            spans.append((i, i + 1))
            i += 1
    return "".join(chars), spans


def terminator(spans: list[tuple[int, int]], text: str) -> str:
    """The line terminator this file already uses — what a newly written line joins.

    The most common spelling among the file's line breaks; a tie goes to the first one in
    the file (``max`` returns the first maximal element). A file with no line break at all
    answers `\\n`.
    """
    counts = {CRLF: 0, CR: 0, LF: 0}
    order: list[str] = []
    for start, end in spans:
        if text[start] == LF:
            spelling = LF
        elif text[start] == CR:
            spelling = CRLF if end - start == 2 else CR
        else:
            continue  # an ordinary character, not a break
        if spelling not in order:
            order.append(spelling)
        counts[spelling] += 1
    if not order:
        return LF
    return max(order, key=lambda spelling: counts[spelling])


def retarget(text: str, terminator_: str) -> str:
    """`text` with its line breaks spelled `terminator_`, whatever it was written with.

    The caller's text is read the way the file is read, so a `new_string` written with
    `\\r\\n` by a caller who saw one does not become `\\r\\r\\n` in a CRLF file.
    """
    lines, _ = view(text)
    if terminator_ == LF:
        return lines
    return lines.replace(LF, terminator_)


def replace(text: str, old: str, new: str, *, replace_all: bool) -> tuple[str, int]:
    """Replace `old` in `text`, leaving every byte outside the match untouched.

    :returns: ``(new_text, count)`` — the count is of matches in the view, matching what
        `str.count` on today's normalised read would report, and `text` comes back
        unchanged when the caller is going to refuse anyway (no match, or more than one
        without `replace_all`). The caller owns those two verdicts; this function owns
        only the splice.
    """
    if not old:
        return text, 0

    seen, spans = view(text)
    sought, _ = view(old)
    count = seen.count(sought)
    if count == 0 or (not replace_all and count > 1):
        return text, count

    # The matches to splice, left to right and non-overlapping — the same set
    # `str.replace` takes, so `replace_all` keeps meaning what it meant.
    starts: list[int] = []
    at = seen.find(sought)
    while at != -1:
        starts.append(at)
        if not replace_all:
            break
        at = seen.find(sought, at + len(sought))

    inserted = retarget(new, terminator(spans, text))

    out: list[str] = []
    cursor = 0
    for at in starts:
        raw_start = spans[at][0]
        raw_end = spans[at + len(sought) - 1][1]
        out.append(text[cursor:raw_start])
        out.append(inserted)
        cursor = raw_end
    out.append(text[cursor:])
    return "".join(out), count
