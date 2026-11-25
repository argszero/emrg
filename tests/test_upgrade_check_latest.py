"""The on-demand release reading (issue #3212).

The subject is the one path in the upgrade module that *wants* to ask GitHub, so every test
here stubs the releases client explicitly: `tests/conftest.py::_guard_upgrade_hermeticity`
turns any real request into an assertion failure, and the stub is what makes the request a
reading instead of a side effect.

The first test is the issue's core: the reading answers with `[update] enabled = false`. It
fails if that gate is ever put back at the top of `check_latest`, which is the shape this
whole change exists to rule out.
"""

import asyncio
import time

from emrg.config import UpdateConfig
from emrg.server.upgrade import (
    CHECK_CACHE_SECONDS,
    UpgradeManager,
    is_newer,
    parse_version,
)


def _release(tag: str, age_seconds: int) -> dict:
    """A release dict published `age_seconds` before now."""
    published = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_seconds)
    )
    return {"tag_name": tag, "published_at": published}


class _Feed:
    """The stubbed releases endpoint: one queued outcome per request.

    An outcome is a list (a 200 whose body it is), an int (that HTTP status) or an
    exception (a request that never completed). Queued rather than fixed so one test can
    take a good reading and then a failing one from the same manager.
    """

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests: list[str] = []

    def client(self):
        feed = self

        class _Resp:
            def __init__(self, status_code, body):
                self.status_code = status_code
                self._body = body

            def json(self):
                return self._body

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                feed.requests.append(url)
                outcome = feed.outcomes.pop(0) if feed.outcomes else []
                if isinstance(outcome, Exception):
                    raise outcome
                if isinstance(outcome, int):
                    return _Resp(outcome, None)
                return _Resp(200, outcome)

        return _Client


def _manager(monkeypatch, feed, *, enabled: bool = False) -> UpgradeManager:
    """A manager over the stubbed feed, with an install path that must never be reached."""

    async def _must_not_install(session_id, cwd, prompt):
        raise AssertionError(
            "the on-demand reading must never start an upgrade session"
        )

    import emrg.server.upgrade as up

    monkeypatch.setattr(up.httpx, "AsyncClient", feed.client())
    return UpgradeManager(
        UpdateConfig(enabled=enabled, delay_minutes=1440), _must_not_install
    )


def test_the_reading_answers_with_auto_upgrade_off(monkeypatch):
    """The issue's core: turning auto-upgrade off stops installs, not the answer.

    Red the moment `check_latest` grows `if not self._config.enabled: return` — the
    switch governs whether this daemon *installs* a release, and the host who turned it
    off is the one with no other way to ask.
    """
    feed = _Feed([_release("v0.3.77", 3000), _release("v0.3.79", 3000)])
    mgr = _manager(monkeypatch, feed, enabled=False)

    answer = asyncio.run(mgr.check_latest("0.3.78"))

    assert answer["ok"] is True
    assert answer["tag"] == "v0.3.79"
    assert answer["version"] == "0.3.79"
    assert answer["is_newer"] is True
    assert answer["cached"] is False
    assert answer["compare_version"] == "0.3.78"
    assert feed.requests, "the reading must have asked the releases endpoint"


def test_the_reading_reports_a_release_the_install_path_is_not_allowed_to_take(monkeypatch):
    """A release inside the delay window exists, so the reading reports it.

    This is the difference from `_find_target_tag`, which answers "what may I install
    now" and returns nothing here. Pinned as a pair so neither side drifts into the
    other's question.
    """
    feed = _Feed([_release("v0.3.79", 60)])
    mgr = _manager(monkeypatch, feed, enabled=False)

    answer = asyncio.run(mgr.check_latest("0.3.78"))

    assert answer["ok"] is True
    assert answer["tag"] == "v0.3.79"
    assert answer["is_newer"] is True

    # The same feed, asked the automatic path's question: nothing is eligible yet.
    feed = _Feed([_release("v0.3.79", 60)])
    mgr = _manager(monkeypatch, feed, enabled=True)
    assert asyncio.run(mgr._find_target_tag()) is None


def test_a_second_press_inside_the_window_does_not_ask_again(monkeypatch):
    """The cache is what keeps a button from becoming the rate limit's cause."""
    feed = _Feed([_release("v0.3.79", 3000)], [_release("v9.9.9", 3000)])
    mgr = _manager(monkeypatch, feed, enabled=False)

    first = asyncio.run(mgr.check_latest("0.3.78"))
    second = asyncio.run(mgr.check_latest("0.3.78"))

    assert first["cached"] is False
    assert second["cached"] is True
    assert second["version"] == "0.3.79", "the cache answered, not the second feed entry"
    assert len(feed.requests) == 1, "the second press reached GitHub"


def test_force_bypasses_the_cache(monkeypatch):
    feed = _Feed([_release("v0.3.79", 3000)], [_release("v0.3.80", 3000)])
    mgr = _manager(monkeypatch, feed, enabled=False)

    asyncio.run(mgr.check_latest("0.3.78"))
    forced = asyncio.run(mgr.check_latest("0.3.78", force=True))

    assert forced["version"] == "0.3.80"
    assert forced["cached"] is False
    assert len(feed.requests) == 2


def test_a_failed_reading_is_not_stored_and_keeps_the_last_good_one(monkeypatch):
    """"I could not ask" must not become "there is nothing", nor lose a right answer."""
    feed = _Feed([_release("v0.3.79", 3000)], 500)
    mgr = _manager(monkeypatch, feed, enabled=False)

    good = asyncio.run(mgr.check_latest("0.3.78"))
    assert good["ok"] is True

    failed = asyncio.run(mgr.check_latest("0.3.78", force=True))
    assert failed["ok"] is False
    assert failed["error"], "a failure names its cause"
    assert failed["version"] is None
    # The last good reading still stands, with the instant it was taken.
    assert failed["checked_at"] == good["checked_at"]
    assert mgr.last_reading()["version"] == "0.3.79"


def test_an_unreachable_endpoint_is_a_reading_that_names_itself(monkeypatch):
    feed = _Feed(RuntimeError("connection reset"))
    mgr = _manager(monkeypatch, feed, enabled=False)

    answer = asyncio.run(mgr.check_latest("0.3.78"))

    assert answer["ok"] is False
    assert "releases endpoint" in answer["error"]
    assert mgr.last_reading() is None


def test_an_empty_feed_is_not_reported_as_current(monkeypatch):
    """No release in the answer is not "you are up to date" — that is the act-on-able lie."""
    feed = _Feed([])
    mgr = _manager(monkeypatch, feed, enabled=False)

    answer = asyncio.run(mgr.check_latest("0.3.78"))

    assert answer["ok"] is False
    assert mgr.last_reading() is None


def test_nothing_is_known_before_the_first_press():
    """`last_reading` is None, never a stand-in — a pong must not claim a comparison."""
    mgr = UpgradeManager(UpdateConfig(enabled=False, delay_minutes=1440), None)
    assert mgr.last_reading() is None


def test_is_newer_is_strictly_newer():
    """The reading's verdict is the module's own rule, not a second one written here."""
    assert is_newer(parse_version("v0.3.79"), parse_version("0.3.78")) is True
    assert is_newer(parse_version("v0.3.78"), parse_version("0.3.78")) is False
    assert is_newer(parse_version("v0.3.77"), parse_version("0.3.78")) is False
    assert CHECK_CACHE_SECONDS > 0
