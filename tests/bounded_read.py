"""Read through a deadline, so a blocked `open()` fails the test instead of hanging it.

Why this exists
---------------
A reader that opens a path without asking its kind cannot be bounded by `try/except`: a
FIFO blocks at the **open** until a writer appears and raises nothing, so the call never
returns and no handler runs. A test that calls such a reader directly therefore has only
two outcomes — the reader returns, or the whole pytest run wedges.

The second outcome is not merely slow, it is **unreadable as a failure**. Measured by this
repository's own instruments: a CI job that overruns its limit is cancelled, and
`scripts/check-merge-freshness.py` classifies a head whose non-successful jobs are all
cancelled as `no_verdict` — whose prescribed remedy is to **re-trigger the same head**
(`cyc20261006-065715`). A regression that hangs would be re-triggered, hang again, and be
reported as `no_verdict` forever, never as the red it is. The same shape shows up locally:
the first run of this class of test had to be killed at 60 s by hand.

So the deadline is what keeps the failure *diagnosable*, and the assertion it raises names
the cause rather than only the expiry.

How a regression reads
----------------------
The call is made on a **daemon** thread, so an `open()` still blocked at the deadline does
not keep the interpreter alive at shutdown: pytest finishes the session and the arm's exit
code is 1 with a failing assertion pytest echoes. That echo is what a mutation arm pins
with `--expect`.

The deadline is generous by three orders of magnitude on purpose: the same reads measured
**0.00-0.03 s** on the fixed tree, so 5 s is headroom for a slow CI machine, not a
performance assertion. A window on this pattern is a fact about the reader, never about
how fast it is.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

#: Seconds any single bounded read may take. See the module docstring: measured 0.00-0.03 s
#: on the fixed tree, so this is headroom, not a budget.
DEADLINE_SECONDS = 5.0


def within(seconds: float, fn: Callable[[], Any]) -> Any:
    """Return `fn()`'s result, failing the test if it has not returned within `seconds`.

    The exception `fn` raised, if any, is re-raised here — a read that raises is a
    different outcome from a read that never returns, and the caller's own assertions
    decide which it expected.
    """
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # carried out, not swallowed
            box["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), (
        f"the read did not return within {seconds}s — a path was opened without asking its "
        f"kind first (a FIFO blocks at the open and raises nothing, so no handler can fire)"
    )
    if "error" in box:
        raise box["error"]
    return box["value"]
