"""The user rows a client echoed locally and still owes a clock.

Rant 2026-10-09T09:25:00. A client shows the host's own message the instant it is
typed — echo has to stay instant — but the message's *moment* belongs to the
daemon, which writes the record and answers with a frame carrying the same
string. So the row is parked here when it is drawn, and filled when that frame
lands.

The parking is **keyed by request id**, and that is the whole point of the type:

* The frame is a broadcast. A peer client's turn, or a scheduled task's cycle,
  reaches this client too, so "fill the last parked row" would let a stranger's
  frame stamp a row it did not cause. Matching on the id cannot.
* More than one row can be in flight. A message typed while the turn is running
  is answered by `steer_committed` at a *round boundary*, which can be many
  seconds later — long enough for the host to submit again. One slot would have
  overwritten the first row, and the first frame would then be dropped by the id
  comparison while its own row kept no clock (finding on #1978, confirmed by
  reading the eight sites that shared `pending_user_row`/`pending_user_rid`).

Nothing here renders, and nothing here reads a clock: the moment is handed in.
That is deliberate — a value this module produced would be a second clock, which
is the defect the rant is about.
"""

from __future__ import annotations


class ParkedUserRows:
    """User rows awaiting their daemon-supplied moment, keyed by request id."""

    def __init__(self) -> None:
        self._rows: dict[str, object] = {}

    def park(self, request_id: str, row: object) -> None:
        """Take the row for ``request_id`` into custody.

        Called at submit, before the request is sent, so the id is already known
        to the caller: a row parked under an unset id could not be matched by the
        frame that answers it, which is a window this closes rather than
        documents. (`send_task` accepts an explicit ``id``, and the id was always
        minted client-side in the first place — `TaskRequest`'s own default.)
        """
        self._rows[str(request_id)] = row

    def fill(self, request_id: str, moment: str) -> bool:
        """Give the parked row its moment. Returns whether a row was filled.

        ``False`` is the ordinary answer for a frame about a message this client
        never drew — a peer's turn, or a message sent from the CLI — and the
        caller does nothing with it. The row leaves custody either way: the frame
        is the only one coming for this request, so keeping it parked would only
        be a leak that a later frame could never use.

        An empty ``moment`` fills **nothing** into the row rather than a
        placeholder. A daemon that could not report an instant is a fact to leave
        as an absence: rendering `()`, `Invalid Date` or a local `now()` here
        would claim a time was known, and the second of those is exactly what the
        rant refuses.
        """
        row = self._rows.pop(str(request_id), None)
        if row is None:
            return False
        if moment:
            row.timestamp = moment  # type: ignore[attr-defined]
            row.dirty = True  # type: ignore[attr-defined]
        return True

    def clear(self) -> None:
        """Drop every parked row — the session they belonged to is gone."""
        self._rows.clear()

    def __len__(self) -> int:
        return len(self._rows)
