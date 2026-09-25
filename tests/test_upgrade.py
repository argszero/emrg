"""UpgradeManager tests (rant 2026-08-20T12:33:59 — 自动升级重构).

Covers the host-specified acceptance items:
- [update] new config fields (enabled/delay_minutes; defaults true/1440)
- tick: delay-filter → newest eligible tag ≠ local version → trigger once
- in-flight guard: no re-trigger while an upgrade session runs
- enabled=false / local==target / network failure → no trigger
- daemon integration: tick fires the session callback, in-flight resets
- no residual references to the removed update_check mechanism
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from emrg.config import UpdateConfig
from emrg.server.upgrade import (
    INEFFECTIVE_ATTEMPT_BACKOFF_SECONDS,
    MAX_INEFFECTIVE_ATTEMPT_BACKOFF_SECONDS,
    RELEASES_URL,
    SESSION_ID,
    UpgradeManager,
    is_newer,
    parse_version,
)

# ── parse_version / is_newer (migrated from update_check.py) ──────────────


def test_parse_version_basic():
    assert parse_version("v0.2.18") == (0, 2, 18)
    assert parse_version("0.2.18") == (0, 2, 18)
    assert parse_version("v0.2.57") == (0, 2, 57)


def test_parse_version_prerelease_suffix_stops_parsing():
    # prerelease/build suffixes must never parse as a full version
    assert parse_version("v0.2.18-beta1") == (0, 2)
    assert parse_version("v0.2.18-rc.2") == (0, 2)
    assert parse_version("") == ()
    assert parse_version("garbage") == ()


def test_is_newer():
    assert is_newer((0, 2, 57), (0, 2, 56))
    assert not is_newer((0, 2, 56), (0, 2, 57))
    assert not is_newer((0, 2, 18), (0, 2, 18))
    assert not is_newer((), (0, 2, 57))  # unparseable never newer


# ── delay filter: takes the NEWEST tag within the eligibility window ──────


def _release(tag: str, age_seconds: int) -> dict:
    """A release dict published `age_seconds` before now."""
    published = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_seconds)
    )
    return {"tag_name": tag, "published_at": published}


async def _tick_with_releases(monkeypatch, releases, delay_minutes=1440, enabled=True):
    """Run one tick with a stubbed releases API response; return trigger calls."""
    calls = []

    async def fake_run_session(session_id, cwd, prompt):
        calls.append({"session_id": session_id, "cwd": cwd, "prompt": prompt})

    mgr = UpgradeManager(
        UpdateConfig(enabled=enabled, delay_minutes=delay_minutes), fake_run_session
    )

    async def fake_get(url):
        class _Resp:
            status_code = 200

            def json(self):
                return releases

        return _Resp()

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return await fake_get(url)

    import emrg.server.upgrade as up

    monkeypatch.setattr(up.httpx, "AsyncClient", _FakeClient)
    await mgr.tick()
    return calls


def test_tick_delay_filter_takes_newest_eligible(monkeypatch, tmp_path):
    # Two eligible (older than 1 day) + one too-recent (must be excluded)
    releases = [
        _release("v0.2.56", 60 * 60 * 24 * 5),
        _release("v0.2.57", 60 * 60 * 24 * 2),
        _release("v0.2.99", 60 * 5),  # too recent — delay window not elapsed
    ]
    monkeypatch.setattr(
        "emrg.server.upgrade.VERSION_FILE", tmp_path / "version.txt"
    )
    (tmp_path / "version.txt").write_text("0.2.55\n", encoding="utf-8")
    calls = asyncio.run(_tick_with_releases(monkeypatch, releases, delay_minutes=1440))
    assert len(calls) == 1
    assert calls[0]["session_id"] == SESSION_ID
    assert "v0.2.57" in calls[0]["prompt"]  # newest ELIGIBLE tag (0.2.99 excluded)


def test_tick_no_trigger_when_local_matches_target(monkeypatch, tmp_path):
    releases = [_release("v0.2.57", 60 * 60 * 24 * 2)]
    monkeypatch.setattr(
        "emrg.server.upgrade.VERSION_FILE", tmp_path / "version.txt"
    )
    (tmp_path / "version.txt").write_text("0.2.57\n", encoding="utf-8")
    calls = asyncio.run(_tick_with_releases(monkeypatch, releases))
    assert calls == [], "local == target → no trigger"


def test_tick_disabled_by_config(monkeypatch, tmp_path):
    releases = [_release("v0.2.57", 60 * 60 * 24 * 2)]
    monkeypatch.setattr(
        "emrg.server.upgrade.VERSION_FILE", tmp_path / "version.txt"
    )
    (tmp_path / "version.txt").write_text("0.2.55\n", encoding="utf-8")
    calls = asyncio.run(_tick_with_releases(monkeypatch, releases, enabled=False))
    assert calls == [], "enabled=false → no trigger at all"


def test_tick_network_failure_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "emrg.server.upgrade.VERSION_FILE", tmp_path / "version.txt"
    )
    (tmp_path / "version.txt").write_text("0.2.55\n", encoding="utf-8")

    class _FailClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise Exception("network down")

    import emrg.server.upgrade as up

    monkeypatch.setattr(up.httpx, "AsyncClient", _FailClient)
    mgr = UpgradeManager(UpdateConfig(), lambda **kw: asyncio.sleep(0))
    asyncio.run(mgr.tick())  # must not raise


def test_inflight_guard_skips_retrigger(monkeypatch, tmp_path):
    """While an upgrade session is running, tick() must not re-trigger."""
    releases = [_release("v0.2.57", 60 * 60 * 24 * 2)]
    monkeypatch.setattr(
        "emrg.server.upgrade.VERSION_FILE", tmp_path / "version.txt"
    )
    (tmp_path / "version.txt").write_text("0.2.55\n", encoding="utf-8")

    async def scenario():
        calls = []
        session_done = asyncio.Event()

        async def slow_session(session_id, cwd, prompt):
            calls.append(session_id)
            await session_done.wait()

        mgr = UpgradeManager(UpdateConfig(), slow_session)

        class _Resp:
            status_code = 200

            def json(self):
                return releases

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                return _Resp()

        import emrg.server.upgrade as up

        monkeypatch.setattr(up.httpx, "AsyncClient", _FakeClient)

        # First tick triggers the session (blocked on the event).
        t1 = asyncio.create_task(mgr.tick())
        await asyncio.sleep(0.05)
        assert mgr._inflight is True, "session start → in-flight"
        assert len(calls) == 1

        # Second tick while in-flight → skipped.
        await mgr.tick()
        assert len(calls) == 1, "in-flight → no re-trigger"

        # Session finishes → in-flight resets.
        session_done.set()
        await t1
        assert mgr._inflight is False, "session end → in-flight reset"

    asyncio.run(scenario())


# ── an attempt that does not install is paid for (issue #1598) ────────────
#
# `_trigger` starts a full LLM session per tick. A target that cannot be installed
# therefore costs one session every 300 s, forever, and before this the code could
# not tell that attempt from the first one. Measured live while this was written: the
# upgrade session's own record rewritten at 06:44:30 local, `version.txt` never
# moving because the session's writes are refused (the issue holds the probe table).


def _repeatable_manager(
    monkeypatch, tmp_path, releases, *, version="0.2.55", delay_minutes=1440
):
    """A manager whose `tick()` can be run over and over with no real endpoint.

    The clock (`upgrade._now`) and the session callback are test-owned, so a test
    states how many **attempts** a sequence of ticks spends rather than how many
    ticks happened. A session writes a version into `version.txt` only when the
    returned `installs` list holds one — that is how a test says "this attempt
    installs". Everything else the chain touches is stubbed by `tests/conftest.py`'s
    autouse guard; the releases client is stubbed here because exercising `tick()`
    at all is what that guard requires a test to do for itself.

    :returns: `(manager, attempts, clock, installs, damage)` — `attempts` collects
        the rendered prompt of every session started, `clock` is a one-element list
        the test advances in seconds, `installs` is what the next session writes into
        `version.txt` (its first element, popped nowhere: a test that wants the write
        to happen once sets it and clears it), and `damage` makes the next session
        leave the file **unreadable** instead — the crashed-overwrite case a backup
        restore would produce, which is how a test reaches the empty read.
    """
    import emrg.server.upgrade as up

    monkeypatch.setattr(up, "VERSION_FILE", tmp_path / "version.txt")
    version_file = tmp_path / "version.txt"
    version_file.write_text(f"{version}\n", encoding="utf-8")

    clock = [1_000_000.0]
    monkeypatch.setattr(up, "_now", lambda: clock[0])

    attempts: list[str] = []
    installs: list[str] = []
    damage: list[bool] = []

    async def session(session_id, cwd, prompt):
        attempts.append(prompt)
        if damage:
            version_file.unlink()
        elif installs:
            version_file.write_text(f"{installs[0]}\n", encoding="utf-8")

    class _Resp:
        status_code = 200

        def json(self):
            return releases

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(up.httpx, "AsyncClient", _FakeClient)
    return UpgradeManager(UpdateConfig(delay_minutes=delay_minutes), session), (
        attempts,
        clock,
        installs,
        damage,
    )


def test_an_attempt_that_did_not_install_is_not_repeated_at_tick_cadence(monkeypatch, tmp_path):
    """The defect itself: two ticks, one session.

    The gap used is 50 s — well inside the daemon's 300 s cadence — so the second
    tick stands for the next real one. Without the wait it starts a second session.
    """
    mgr, (attempts, clock, _installs, damage) = _repeatable_manager(
        monkeypatch, tmp_path, [_release("v0.2.57", 60 * 60 * 24 * 3)]
    )

    asyncio.run(mgr.tick())
    assert len(attempts) == 1, "the first attempt must happen"

    clock[0] += 50
    asyncio.run(mgr.tick())
    assert len(attempts) == 1, (
        "a target whose install did not move version.txt must not be attempted "
        "again at tick cadence — that is one full LLM session per 300 s (issue #1598)"
    )

    clock[0] += INEFFECTIVE_ATTEMPT_BACKOFF_SECONDS
    asyncio.run(mgr.tick())
    assert len(attempts) == 2, "once the wait has elapsed the tag is attempted again"


def test_the_wait_grows_per_attempt_and_stops_at_the_ceiling():
    """Named numbers: "exponential" is not a reading, a list is."""
    mgr = UpgradeManager(UpdateConfig(), lambda **kw: asyncio.sleep(0))
    assert mgr._ineffective_backoff() == 0.0, "nothing owing before any attempt"

    waits = []
    for _ in range(7):
        mgr._ineffective_attempts += 1
        waits.append(mgr._ineffective_backoff())

    assert waits == [1800, 3600, 7200, 14400, 21600, 21600, 21600]
    assert waits[0] == INEFFECTIVE_ATTEMPT_BACKOFF_SECONDS
    assert max(waits) == MAX_INEFFECTIVE_ATTEMPT_BACKOFF_SECONDS, (
        "the ceiling must be reached and then held: 4 attempts a day, not 288"
    )


def test_a_newer_target_is_attempted_at_once(monkeypatch, tmp_path):
    """The clause that keeps the wait from delaying the repair it pays for.

    For a host blocked by the release it keeps re-installing, the next release *is*
    the fix arriving — holding it back behind the previous target's failure would
    push a repair out by the ceiling.
    """
    releases = [_release("v0.2.57", 60 * 60 * 24 * 3)]
    mgr, (attempts, clock, _installs, damage) = _repeatable_manager(
        monkeypatch, tmp_path, releases
    )

    asyncio.run(mgr.tick())
    assert len(attempts) == 1
    clock[0] += 50  # inside the wait for v0.2.57

    releases.insert(0, _release("v0.2.58", 60 * 60 * 24 * 2))
    asyncio.run(mgr.tick())
    assert len(attempts) == 2, "a different target must not wait out the previous one's failure"
    assert "v0.2.58" in attempts[1], "and it is the newer target that is attempted"


def test_an_install_clears_the_wait(monkeypatch, tmp_path):
    """The other direction: the wait answers an outcome, it is not a state to drift into."""
    releases = [_release("v0.2.57", 60 * 60 * 24 * 3)]
    mgr, (attempts, clock, installs, damage) = _repeatable_manager(
        monkeypatch, tmp_path, releases
    )

    asyncio.run(mgr.tick())
    assert mgr._ineffective_attempts == 1, "a session that changed nothing leaves a debt"

    clock[0] += 50
    releases.insert(0, _release("v0.2.58", 60 * 60 * 24 * 2))
    installs.append("0.2.58")  # this session does its job
    asyncio.run(mgr.tick())
    assert len(attempts) == 2
    assert mgr._ineffective_attempts == 0, "version.txt moving clears the debt"
    assert mgr._ineffective_backoff() == 0.0
    assert mgr._is_waiting("v0.2.58") is False


def test_a_destroyed_version_file_is_not_read_as_progress(monkeypatch, tmp_path):
    """The third outcome: an attempt that leaves no version at all.

    `_read_local_version` answers `""` for a file that is missing or empty, and the
    charge for an attempt compared `after != before` — so a session that *deleted*
    `version.txt` (the crashed overwrite the upgrade prompt's own `previous-version`
    step exists for) read as "the install moved" and **cleared the wait**, at exactly
    the moment the chain is most broken. An empty answer is not a version: it is the
    absence of evidence, and the debt must survive it.
    """
    releases = [_release("v0.2.57", 60 * 60 * 24 * 3)]
    mgr, (attempts, clock, _installs, damage) = _repeatable_manager(
        monkeypatch, tmp_path, releases
    )

    asyncio.run(mgr.tick())
    assert mgr._ineffective_attempts == 1

    clock[0] += 50  # inside the wait
    damage.append(True)  # the next session leaves the file unreadable
    releases.insert(0, _release("v0.2.58", 60 * 60 * 24 * 2))
    asyncio.run(mgr.tick())
    assert len(attempts) == 2, "a different target is still attempted at once"
    assert not (tmp_path / "version.txt").exists(), "the fixture must really destroy it"
    assert mgr._ineffective_attempts == 2, (
        "an attempt that left no version at all is not progress: reading `after != "
        "before` counts the empty string as a new version, so a destroyed version.txt "
        "cleared the wait (issue #1600's self-review)"
    )
    assert mgr._is_waiting("v0.2.58") is True, "so the next attempt of that tag waits"


# ── config: new [update] fields ───────────────────────────────────────────


def test_update_config_defaults():
    cfg = UpdateConfig()
    assert cfg.enabled is True
    assert cfg.delay_minutes == 1440
    assert not hasattr(cfg, "check"), "old [update] check field must be gone"
    assert not hasattr(cfg, "ttl_hours"), "old [update] ttl_hours field must be gone"
    assert not hasattr(cfg, "auto_download"), "old auto_download field must be gone"


def test_load_update_config_new_fields(tmp_path, monkeypatch):
    from emrg import config as cfg_mod

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[update]\nenabled = false\ndelay_minutes = 1\n", encoding="utf-8"
    )
    monkeypatch.setattr(cfg_mod, "config_path", lambda: cfg_path)
    cfg = cfg_mod.load_update_config()
    assert cfg.enabled is False
    assert cfg.delay_minutes == 1


def test_load_config_full_new_fields(tmp_path, monkeypatch):
    from emrg import config as cfg_mod

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[llm]\nbase_url = 'x'\napi_key = 'k'\n"
        "[update]\nenabled = false\ndelay_minutes = 5\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg_mod, "config_path", lambda: cfg_path)
    cfg = cfg_mod.load_config()
    assert cfg.update.enabled is False
    assert cfg.update.delay_minutes == 5


# ── daemon integration: tick → run_session_cb (public runner) ─────────────


def test_daemon_upgrade_session_runner(monkeypatch, tmp_path):
    """The daemon's _run_upgrade_session executes the prompt as an agent
    session: session created, busy lock set and released, tool loop invoked."""
    from tests.test_daemon import _make_server  # reuse the daemon test helper

    server = _make_server()
    monkeypatch.setattr(server, "_max_tool_rounds", 3)
    # ⛔ Red line (host 2026-08-21T10:35:57): tests must never create/write the
    # real emrg-upgrade session — isolate the session factory (the conftest
    # autouse guard raises on the real one for SESSION_ID).
    monkeypatch.setattr(server, "_get_or_create_session", lambda sid, cwd: object())
    ran = []

    async def fake_loop(req, ws, session, cancel_event, allow_tools=True):
        ran.append((req.session_id, req.cwd, req.prompt, allow_tools))
        # mirror the real _run_tool_loop_locked finally: release the busy lock
        server._session_busy[req.session_id] = False

    monkeypatch.setattr(server, "_run_tool_loop_locked", fake_loop)

    asyncio.run(server._run_upgrade_session("emrg-upgrade", str(tmp_path), "PROMPT"))

    assert len(ran) == 1
    assert ran[0][0] == "emrg-upgrade"
    assert ran[0][1] == str(tmp_path)
    assert ran[0][2] == "PROMPT"
    assert ran[0][3] is True, "upgrade sessions run with tools"
    assert server._session_busy.get("emrg-upgrade") is False, "busy lock released"


def test_upgrade_chain_hermeticity_guards():
    """⛔ Red line (host 2026-08-21T10:35:57): the conftest autouse guard must
    block the real auto-upgrade chain by default — no real GitHub releases
    request, no real install/version.txt access. A long-running pytest session
    really executed the upgrade chain every 5 minutes (PID 72994, 21h).
    """
    from pathlib import Path

    import emrg.server.upgrade as up

    # 1. Network: the upgrade module's httpx.AsyncClient raises by default.
    with pytest.raises(AssertionError, match="red-line"):
        up.httpx.AsyncClient()

    # 2. Version file: not the real ~/.emrg/install/version.txt.
    assert up.VERSION_FILE != Path.home() / ".emrg" / "install" / "version.txt"

    # 3. Retention (issue #1389): the upgrade tick prunes the snapshot
    #    directory, and on a real host that directory holds the only rollback
    #    snapshot there is. If this assertion ever fails, every test that calls
    #    tick() is deleting the host's rollback path.
    assert up.BACKUP_DIR != Path.home() / ".emrg" / "upgrade-backup"


# ── no residual references to the removed mechanism ───────────────────────


def test_no_residual_update_check_references():
    """The old update_check mechanism must be fully removed (host §7/§8):
    emrg/update_check.py gone; no references to the module, its state file,
    or the removed [update] fields outside upgrade.py's own docstring."""
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).parent.parent
    files = [
        "emrg/server/daemon.py",
        "emrg/server/upgrade.py",
        "emrg/config.py",
        "emrg/client/app.py",
        "emrg/gui/main.js",
        "emrg/gui/preload.js",
    ]
    assert not (repo / "emrg/update_check.py").exists(), "update_check.py must be deleted"
    assert not (repo / "tests/test_update_check.py").exists(), "test_update_check.py must be deleted"
    for rel in files:
        text = (repo / rel).read_text(encoding="utf-8")
        # allow the upgrade.py docstring itself + config.py removal note to
        # mention the old names; everything else must be clean
        if rel == "emrg/server/upgrade.py":
            continue
        if rel == "emrg/config.py" and "removed" in text:
            continue
        # daemon.py legitimately keeps the SKILLS updater's run_update_check_once
        # (emrg.skills.installer — a separate skills mechanism, not the removed
        # auto-upgrade module); strip those lines before asserting.
        if rel == "emrg/server/daemon.py":
            text = "\n".join(
                ln for ln in text.split("\n") if "run_update_check_once" not in ln
            )
        assert "update_check" not in text, f"{rel} still references update_check"
        assert "ttl_hours" not in text, f"{rel} still references ttl_hours"
        assert "auto_download" not in text, f"{rel} still references auto_download"
        assert ".last_update_check" not in text, f"{rel} still references the state file"


# ── upgrade_prompt.j2 structure guard (R2249) ─────────────────────────────
# v0.2.82/v0.2.83 教训：upgrade_prompt.j2 的 GUI 构建行原本是单行
# `npm install && npm run dist`，升级 agent 只执行了 `npm install`（把链截断或
# 改写为后台命令），`npm run dist`（electron-builder）从未运行 → GUI 停留在旧版
# 本。修复（#1043）把两步拆开并加 app.asar 新鲜度闸门；本测试把修复后的结构钉
# 死——未来任何编辑若把两步重新合并、删除 MUST RUN 指令或移除新鲜度检查，立即红。
def test_upgrade_prompt_gui_build_steps_are_separate_and_gated() -> None:
    prompt = (Path(__file__).parent.parent / "emrg/server/prompts/upgrade_prompt.j2").read_text(
        encoding="utf-8"
    )
    lines = prompt.splitlines()

    # 1. npm install 与 npm run dist 必须在不同行（单行 && 链会被 agent 截断）
    #    —— 原始 bug 的精确形态，禁止回归。
    chain = [ln for ln in lines if "npm install" in ln and "npm run dist" in ln]
    assert not chain, (
        "upgrade_prompt.j2 must keep `npm install` and `npm run dist` on separate "
        f"lines — a single-line && chain gets truncated by the upgrade agent (v0.2.82/"
        "v0.2.83 regression). Found: {chain}"
    )

    # 2. 两步必须都存在，且 dist 步带 MUST RUN 指令（不可跳过语义）。
    install_ln = next((ln for ln in lines if "npm install" in ln), None)
    assert install_ln is not None, "upgrade_prompt.j2 must contain an npm install step"
    dist_ln = next((ln for ln in lines if "npm run dist" in ln), None)
    assert dist_ln is not None, "upgrade_prompt.j2 must contain an npm run dist step"
    dist_idx = lines.index(dist_ln)
    must_run_ctx = "\n".join(lines[max(0, dist_idx - 3) : dist_idx + 1])
    assert "MUST RUN" in must_run_ctx, (
        "the npm run dist step must carry a MUST RUN directive (a stale dist will "
        "NOT rebuild on its own). Context: " + must_run_ctx
    )

    # 3. app.asar 新鲜度闸门必须存在（stat + app.asar，mtime 证明产物被重建）。
    gate_ln = next((ln for ln in lines if "stat" in ln and "app.asar" in ln), None)
    assert gate_ln is not None, (
        "upgrade_prompt.j2 must verify the rebuilt artifact via app.asar mtime "
        "(stat) — npm run dist can silently reuse a stale dist."
    )

    # 4. 顺序：install → dist → 新鲜度闸门（闸门在 dist 之后，验证其产物）。
    gate_idx = lines.index(gate_ln)
    install_idx = lines.index(install_ln)
    assert install_idx < dist_idx < gate_idx, (
        "step order must be: npm install → npm run dist → app.asar freshness gate "
        f"(got install={install_idx} dist={dist_idx} gate={gate_idx})"
    )



# ── upgrade_prompt.j2 macOS re-seal chain guard (R2250) ───────────────────
# rant 2026-08-25T09:18:19：electron-builder dir 产物只有主二进制 ad-hoc 签名，
# bundle 未密封 + 带 com.apple.provenance 等 xattr；macOS 26 把"未密封 + 隔离属性"
# 的部署副本判为恶意软件移入废纸篓。该修复是 prompt-only——本守卫把 re-seal →
# copy → verify 的顺序与命令钉死，未来编辑若删除/调换这些步骤立即红。
def test_upgrade_prompt_macos_reseal_chain_guarded() -> None:
    prompt = (Path(__file__).parent.parent / "emrg/server/prompts/upgrade_prompt.j2").read_text(
        encoding="utf-8"
    )
    lines = prompt.splitlines()

    # 1. re-seal（3e）必须在 Replace/copy 之前：产物先修好再复制。
    reseal = next(
        (ln for ln in lines if "codesign --force --deep --sign -" in ln), None
    )
    assert reseal is not None, (
        "upgrade_prompt.j2 must re-seal the built bundle (codesign --force --deep "
        "--sign -) before copying — rant 2026-08-25T09:18:19 (macOS 26 moves "
        "unsealed+xattr copies to Trash as malware)."
    )
    replace = next(
        (ln for ln in lines if ln.strip().startswith("4.") and "Replace" in ln), None
    )
    assert replace is not None, "upgrade_prompt.j2 must contain the Replace step"
    reseal_idx, replace_idx = lines.index(reseal), lines.index(replace)
    assert reseal_idx < replace_idx, (
        "re-seal (3e) must happen BEFORE the Replace/copy step — copying an "
        f"unsealed bundle ships the malware-flagged artifact (got reseal={reseal_idx} "
        f"replace={replace_idx})"
    )

    # 2. 构建产物的 xattr 清除必须在复制前（ditto 会传播 xattr）。
    xattr_build = next(
        (ln for ln in lines if 'xattr -cr "{{ upgrade_work }}' in ln), None
    )
    assert xattr_build is not None, (
        "upgrade_prompt.j2 must clear xattrs on the BUILT artifact (xattr -cr on "
        "the dist app) before copying — ditto propagates source xattrs."
    )
    assert lines.index(xattr_build) < replace_idx, (
        "built-artifact xattr clear must precede the Replace/copy step"
    )

    # 3. 运行副本的 xattr 清除必须存在（step 4 copy 后）。
    xattr_run = next(
        (ln for ln in lines if "xattr -cr ~/Applications/EMRG.app" in ln), None
    )
    assert xattr_run is not None, (
        "upgrade_prompt.j2 must clear xattrs on the run copy "
        "(xattr -cr ~/Applications/EMRG.app) after copying."
    )

    # 4. verify：3e sanity + step5 install/run 双检 = 至少 3 次 codesign --verify
    verify = [ln for ln in lines if "codesign --verify --deep --strict" in ln]
    assert len(verify) >= 3, (
        "upgrade_prompt.j2 must verify signatures at least 3 times (pre-copy sanity "
        f"+ install + run copy). Found {len(verify)}: {verify}"
    )


# ── upgrade_prompt.j2 backup/rollback chain guard (R2252) ──────────────────
# 数据安全链：升级前必须备份现有 app（install 源 + 运行副本两个位置），verify 任何
# 失败必须从备份恢复——否则一次坏升级会同时毁掉工作副本且无回退（数据丢失 > 坏部署）。
# 该链此前 prompt-only、无自动化守卫；本测试钉死 backup-before-copy + restore-on-failure。
def test_upgrade_prompt_backup_rollback_chain_guarded() -> None:
    prompt = (Path(__file__).parent.parent / "emrg/server/prompts/upgrade_prompt.j2").read_text(
        encoding="utf-8"
    )
    lines = prompt.splitlines()

    # 1. step 4 必须备份两个 app 位置（install 源 + 运行副本）到 backup_dir。
    step4 = next(
        (i for i, ln in enumerate(lines) if ln.strip().startswith("4.") and "Replace" in ln), None
    )
    assert step4 is not None, "upgrade_prompt.j2 must contain the Replace (step 4) header"
    step4_block = "\n".join(lines[step4 : step4 + 10])
    assert "{{ install_dir }}/emrg-gui/EMRG.app" in step4_block, (
        "step 4 must back up the install-source app ({{ install_dir }}/emrg-gui/EMRG.app)"
    )
    assert "~/Applications/EMRG.app" in step4_block, (
        "step 4 must back up the run copy (~/Applications/EMRG.app)"
    )
    assert "{{ backup_dir }}" in step4_block, (
        "step 4 must back up into {{ backup_dir }}/<current_version>/"
    )

    # 2. backup 必须发生在 copy 之前（back-up 语言先于 copy 语言）。
    backup_marker = next((i for i, ln in enumerate(lines) if "back up the current" in ln), None)
    copy_marker = next((i for i, ln in enumerate(lines) if "copy the fresh" in ln), None)
    assert backup_marker is not None, "step 4 must say 'back up the current ...'"
    assert copy_marker is not None, "step 4 must say 'copy the fresh build'"
    assert backup_marker < copy_marker, (
        "backup must precede copy — replacing the app without a backup leaves no "
        f"rollback path on failure (got backup={backup_marker} copy={copy_marker})"
    )

    # 3. restore-from-backup 必须存在于失败路径（step 5 verify 失败 → restore）。
    restores = [ln for ln in lines if "restore from" in ln]
    assert len(restores) >= 2, (
        "restore-from-backup must be instructed at least twice: the step-5 verify "
        "failure path AND the '## 4. Backup & rollback' section. "
        f"Found {len(restores)}: {restores}"
    )

    # 4. '## 4. Backup & rollback' 段必须同时含 backup 与 restore 语义。
    sec = next(
        (i for i, ln in enumerate(lines) if ln.strip().startswith("## 4.") and "Backup" in ln), None
    )
    assert sec is not None, "upgrade_prompt.j2 must contain a '## 4. Backup & rollback' section"
    sec_block = "\n".join(lines[sec : sec + 8])
    assert "back up" in sec_block and "restore" in sec_block, (
        "the Backup & rollback section must instruct both backing up before touching "
        "and restoring on failure."
    )


# ── snapshot retention (issue #1389) ───────────────────────────────────────
# ~/.emrg/upgrade-backup 只增不减：每次升级写入一份完整 install 快照（宿主实测
# 599 MB/份，14 份 = 8.2 GB），而升级 prompt 只从 backup_dir/<current_version>
# 恢复——即"被替换的那个版本"，也就是 previous-version.txt 记录的那一份。其余
# 快照永远不可能被恢复，纯累积。本组测试把保留策略钉死：策略是纯函数（可在临时
# backup_dir 上直接跑），删除路径单独测，且必须永不删掉"可恢复的那一份"。


def _snapshot(root: Path, name: str, marker: str = "payload") -> Path:
    """Create a snapshot directory the way the upgrade prompt would."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.txt").write_text(marker, encoding="utf-8")
    return d


def _install_versions(monkeypatch, tmp_path, current: str, previous: str | None) -> None:
    """Point version.txt / previous-version.txt at a scratch install."""
    import emrg.server.upgrade as up

    current_file = tmp_path / "version.txt"
    current_file.write_text(current, encoding="utf-8")
    monkeypatch.setattr(up, "VERSION_FILE", current_file)
    prev_file = tmp_path / "previous-version.txt"
    if previous is not None:
        prev_file.write_text(previous, encoding="utf-8")
    monkeypatch.setattr(up, "PREVIOUS_VERSION_FILE", prev_file)


def test_prune_keeps_the_rollback_snapshot_and_drops_the_rest(monkeypatch, tmp_path):
    from emrg.server.upgrade import prune_upgrade_backups

    backups = tmp_path / "upgrade-backup"
    for ver in ("0.2.90", "0.2.91", "0.2.92", "0.2.93", "0.2.94", "0.2.95"):
        _snapshot(backups, ver)
    # 0.2.96 running, replacing 0.2.95 — the state measured on the host that
    # filed the issue (8.2 GB across 14 snapshots, one of them restorable).
    _install_versions(monkeypatch, tmp_path, "0.2.96", "0.2.95")

    removed = prune_upgrade_backups(backups)

    assert removed == ["0.2.90", "0.2.91", "0.2.92", "0.2.93", "0.2.94"], (
        "the superseded snapshots are removed oldest-first"
    )
    assert (backups / "0.2.95").is_dir(), (
        "the snapshot previous-version.txt names IS the rollback target — "
        "removing it would leave a failed upgrade with nothing to restore from"
    )
    assert sorted(p.name for p in backups.iterdir()) == ["0.2.95"]


def test_prune_removes_only_the_snapshot_directory(monkeypatch, tmp_path):
    """The removed bytes are the snapshot's — nothing else in its place."""
    from emrg.server.upgrade import prune_upgrade_backups

    backups = tmp_path / "upgrade-backup"
    _snapshot(backups, "0.2.90", marker="old-install")
    _snapshot(backups, "0.2.95", marker="rollback-install")
    _install_versions(monkeypatch, tmp_path, "0.2.96", "0.2.95")

    assert prune_upgrade_backups(backups) == ["0.2.90"]
    assert (backups / "0.2.95" / "source.txt").read_text(encoding="utf-8") == "rollback-install"


def test_prune_is_a_no_op_on_a_fresh_install(monkeypatch, tmp_path):
    """No snapshot directory at all — a fresh install must stay a non-event."""
    from emrg.server.upgrade import prune_upgrade_backups

    _install_versions(monkeypatch, tmp_path, "0.2.96", None)
    assert prune_upgrade_backups(tmp_path / "never-created") == []

    empty = tmp_path / "upgrade-backup"
    empty.mkdir()
    assert prune_upgrade_backups(empty) == []


def test_prune_leaves_alone_what_it_cannot_read_as_a_version(monkeypatch, tmp_path):
    """A name this module cannot parse is not a file it may delete.

    The upgrade prompt names snapshots `<current_version>`; anything else in the
    directory was put there by someone else and is not this policy's to remove —
    including a symlink, which `rmtree` would follow out of the directory.
    """
    from emrg.server.upgrade import prune_upgrade_backups

    backups = tmp_path / "upgrade-backup"
    _snapshot(backups, "0.2.90")
    _snapshot(backups, "0.2.95")
    _snapshot(backups, "nightly")
    _snapshot(backups, "v0.2.89-broken.rc")
    (backups / "notes.txt").write_text("not a snapshot", encoding="utf-8")
    (backups / "0.2.88").symlink_to(tmp_path / "elsewhere")
    _install_versions(monkeypatch, tmp_path, "0.2.96", "0.2.95")

    assert prune_upgrade_backups(backups) == ["0.2.90"]
    surviving = sorted(p.name for p in backups.iterdir())
    assert surviving == ["0.2.88", "0.2.95", "nightly", "notes.txt", "v0.2.89-broken.rc"]
    assert (backups / "nightly").is_dir()
    assert (backups / "0.2.88").is_symlink()


def test_prune_keeps_a_named_version_that_is_not_the_newest(monkeypatch, tmp_path):
    """The protection is the *name*, not the rank (a downgrade or a hand copy).

    Here the install runs 0.2.90 and replaced 0.2.80, while a newer 0.2.95
    snapshot sits in the directory. Keeping only the newest would delete both
    restorable snapshots — the versions the install can name outrank recency.
    """
    from emrg.server.upgrade import prune_upgrade_backups

    backups = tmp_path / "upgrade-backup"
    for ver in ("0.2.70", "0.2.80", "0.2.90", "0.2.95"):
        _snapshot(backups, ver)
    _install_versions(monkeypatch, tmp_path, "0.2.90", "0.2.80")

    assert prune_upgrade_backups(backups) == ["0.2.70"]
    assert sorted(p.name for p in backups.iterdir()) == ["0.2.80", "0.2.90", "0.2.95"]


def test_prune_without_version_files_keeps_the_newest(monkeypatch, tmp_path):
    """Unreadable/absent version files: the floor is the newest snapshot.

    The policy may never be the reason a directory of snapshots is emptied — if
    nothing can be named, the newest one is kept and reported as kept.
    """
    from emrg.server.upgrade import prune_upgrade_backups

    backups = tmp_path / "upgrade-backup"
    for ver in ("0.2.90", "0.2.91", "0.2.95"):
        _snapshot(backups, ver)
    import emrg.server.upgrade as up

    monkeypatch.setattr(up, "VERSION_FILE", tmp_path / "absent-version.txt")
    monkeypatch.setattr(up, "PREVIOUS_VERSION_FILE", tmp_path / "absent-previous.txt")

    assert prune_upgrade_backups(backups) == ["0.2.90", "0.2.91"]
    assert [p.name for p in backups.iterdir()] == ["0.2.95"]


def test_prune_orders_snapshots_by_version_not_by_name_or_mtime(monkeypatch, tmp_path):
    """'0.2.100' is newer than '0.2.99'; mtime is not consulted at all."""
    from emrg.server.upgrade import prune_upgrade_backups

    backups = tmp_path / "upgrade-backup"
    for ver in ("0.2.99", "0.2.100", "0.2.101"):
        _snapshot(backups, ver)
    # mtimes deliberately inverted: 0.2.99 touched last.
    import os

    os.utime(backups / "0.2.99", (time.time() + 60, time.time() + 60))
    _install_versions(monkeypatch, tmp_path, "0.2.102", "0.2.101")

    assert prune_upgrade_backups(backups) == ["0.2.99", "0.2.100"]
    assert [p.name for p in backups.iterdir()] == ["0.2.101"]


def test_prune_tolerates_a_v_prefix_in_the_names(monkeypatch, tmp_path):
    """The prompt writes the tag; the name may or may not keep its 'v'."""
    from emrg.server.upgrade import prune_upgrade_backups

    backups = tmp_path / "upgrade-backup"
    _snapshot(backups, "v0.2.94")
    _snapshot(backups, "0.2.95")
    _install_versions(monkeypatch, tmp_path, "v0.2.96", "v0.2.95")

    assert prune_upgrade_backups(backups) == ["v0.2.94"]
    assert [p.name for p in backups.iterdir()] == ["0.2.95"]


def test_prune_survives_a_snapshot_it_cannot_remove(monkeypatch, tmp_path):
    """One undeletable snapshot must not hide the others or abort the sweep."""
    from emrg.server.upgrade import prune_upgrade_backups

    backups = tmp_path / "upgrade-backup"
    _snapshot(backups, "0.2.90")
    _snapshot(backups, "0.2.91")
    _snapshot(backups, "0.2.95")
    _install_versions(monkeypatch, tmp_path, "0.2.96", "0.2.95")

    import shutil as _shutil

    real_rmtree = _shutil.rmtree

    class _FlakyShutil:
        """Only the upgrade module's view — the real shutil is untouched."""

        @staticmethod
        def rmtree(path, *a, **kw):
            if Path(path).name == "0.2.90":
                raise OSError("device busy")
            return real_rmtree(path, *a, **kw)

    monkeypatch.setattr("emrg.server.upgrade.shutil", _FlakyShutil)
    removed = prune_upgrade_backups(backups)

    assert removed == ["0.2.91"], "the snapshot that could not be removed is not reported as removed"
    assert (backups / "0.2.90").is_dir(), "a failed removal leaves the snapshot in place"
    assert not (backups / "0.2.91").exists()


def test_tick_prunes_the_snapshots(monkeypatch, tmp_path):
    """The sweep is wired into tick() — nothing else ever visits the directory."""
    import emrg.server.upgrade as up

    backups = tmp_path / "upgrade-backup"
    for ver in ("0.2.90", "0.2.91", "0.2.95"):
        _snapshot(backups, ver)
    _install_versions(monkeypatch, tmp_path, "0.2.96", "0.2.95")
    monkeypatch.setattr(up, "BACKUP_DIR", backups)

    class _EmptyClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            class _Resp:
                status_code = 200

                def json(self):
                    return []

            return _Resp()

    monkeypatch.setattr(up.httpx, "AsyncClient", _EmptyClient)
    mgr = UpgradeManager(UpdateConfig(), lambda **kw: asyncio.sleep(0))
    asyncio.run(mgr.tick())

    assert [p.name for p in backups.iterdir()] == ["0.2.95"]


def test_tick_prunes_nothing_while_enabled_is_false(monkeypatch, tmp_path):
    """enabled=false disables the upgrade trigger, and the sweep sits behind
    that check — a host who turned upgrades off must not have bases deleted."""
    import emrg.server.upgrade as up

    backups = tmp_path / "upgrade-backup"
    for ver in ("0.2.90", "0.2.91", "0.2.95"):
        _snapshot(backups, ver)
    _install_versions(monkeypatch, tmp_path, "0.2.96", "0.2.95")
    monkeypatch.setattr(up, "BACKUP_DIR", backups)

    mgr = UpgradeManager(UpdateConfig(enabled=False), lambda **kw: asyncio.sleep(0))
    asyncio.run(mgr.tick())

    assert sorted(p.name for p in backups.iterdir()) == ["0.2.90", "0.2.91", "0.2.95"]
