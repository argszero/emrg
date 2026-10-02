"""One answer to "what is in this JSON?", for every reader that wants a mapping.

Several readers in the daemon and the scheduler hold a JSON file (or one JSONL
line) that is *meant* to be an object and then index it with ``.get``. Each of
them guarded a different subset of the ways that can fail, and the subset every
one of them missed is the same: the text parses fine and yields something that
is not an object — ``[]``, ``null``, ``"x"``, ``5``. The guard list is
``(JSONDecodeError, OSError)`` in one place, ``ValueError`` in another, nothing
at all in a third, so a single ``[]`` in a log directory reached ``.get`` as an
``AttributeError`` and took the caller with it (measured 2026-10-02: it left
``EmrgServer._process_message``, whose ``except Exception`` in
``_handle_client`` ends the message loop — the client is dropped, not answered).

The decision is the same in every one of those places, so it is taken once,
here, and the two entry points are named for the two shapes the callers have:
a path on disk (:func:`read_json_object`) and text already in hand
(:func:`parse_json_object`, for a JSONL line). "Not a mapping" covers the same
four states for both — unreadable or undecodable text, text that does not
parse, and a value that parses to something other than an object — plus, for
the path form, the file simply not being there. A reader that needs to tell
those apart is not served by this module, because no call site in the tree
does; each of them wants the same thing, "the payload, or nothing".

Note the deliberate asymmetry with the writer side: a reader that treats a
non-object as absent is *not* the same as a writer that produces one. This
module is only the read half.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["parse_json_object", "read_json_object"]


def parse_json_object(text: str) -> dict | None:
    """The mapping ``text`` parses to, or ``None`` when it is not one.

    ``None`` therefore means "no payload here", which the same thing means for
    a JSONL line that is blank or a line whose write was cut short as it means
    for a value of the wrong shape.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_json_object(path) -> dict | None:
    """The mapping the file at ``path`` holds, or ``None`` when it holds none.

    ``None`` for a missing file, an unreadable one, undecodable bytes, text
    that does not parse, and a JSON value that is not an object — the caller
    that wants "the payload, or nothing" does not need to know which.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("json file %s could not be read: %s", path, exc)
        return None
    return parse_json_object(text)
