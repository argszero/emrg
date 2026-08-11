"""EMRG client — TUI interface (python-tui inlined).
Keeps interactive_demo.py's input handling, renders chat in viewport.
"""

from __future__ import annotations

import asyncio, json, logging, os, platform, signal, subprocess, sys, threading, time
try:
    import fcntl  # POSIX-only（TUI 非阻塞 stdin）；Windows 无此模块
except ImportError:  # pragma: no cover - Windows
    fcntl = None
from datetime import datetime
from pathlib import Path, PurePath
from emrg._win import win32_no_window_kwargs
from emrg.client import daemon_manager
from emrg.client.python_tui import ChatRow, Diff, InputParser, StatusLine, Terminal, ToolCard
from emrg.client.python_tui.widgets.markdown import StreamingMarkdown
from emrg.client.widgets import (
    InputWidget, RewindSelector, SessionSelector, ProjectSelector,
    TaskSelector, ModelSelector, CommandDropdown, ChatHistory, SelectorState,
    _COMMAND_HELP,
)
from websockets.exceptions import ConnectionClosed
from emrg.protocol import TaskResponse, ToolEnd, ToolStart
from emrg.session import generate_session_id
from emrg.skills.loader import load_skills

logger = logging.getLogger(__name__)


# ── Clipboard image support (platform-adaptive) ─────────────

def _detect_clipboard_image() -> tuple[bool, str | None]:
    """Check system clipboard for image data.
    Returns (has_image, label_or_None).
    """
    system = platform.system()
    try:
        if system == "Darwin":
            result = subprocess.run(
                ['osascript', '-e', 'clipboard info'],
                capture_output=True, text=True, timeout=3,
                **win32_no_window_kwargs(),
            )
            out = result.stdout
            has_image = any(tag in out for tag in (
                '«class PNGf»', '«class TIFF»', '«class jpg»',
                '«class GIFf»', '«class BMP »', 'picture',
            ))
            if not has_image:
                return False, None
            # Try to get filename from file reference
            label = None
            try:
                r2 = subprocess.run(
                    ['osascript', '-e',
                     'try\n  set f to (the clipboard as «class furl»)\n'
                     '  return POSIX path of f\nend try'],
                    capture_output=True, text=True, timeout=2,
                    **win32_no_window_kwargs(),
            )
                if r2.stdout.strip():
                    label = Path(r2.stdout.strip()).name
            except Exception:
                pass
            return True, label

        elif system == "Linux":
            result = subprocess.run(
                ['xclip', '-selection', 'clipboard', '-t', 'TARGETS', '-o'],
                capture_output=True, text=True, timeout=3,
                **win32_no_window_kwargs(),
            )
            out = result.stdout
            if 'image/png' not in out:
                return False, None
            # Try to get filename from URI list
            label = None
            if 'text/uri-list' in out:
                try:
                    r2 = subprocess.run(
                        ['xclip', '-selection', 'clipboard', '-t',
                         'text/uri-list', '-o'],
                        capture_output=True, text=True, timeout=2,
                        **win32_no_window_kwargs(),
            )
                    uri = r2.stdout.strip()
                    if uri:
                        label = Path(uri.replace('file://', '')).name
                except Exception:
                    pass
            return True, label

        elif system == "Windows":
            ps_cmd = (
                'Add-Type -AssemblyName System.Windows.Forms; '
                '$img = [System.Windows.Forms.Clipboard]::GetImage(); '
                'if ($img -ne $null) { Write-Output "IMAGE" } else { Write-Output "TEXT" }'
            )
            result = subprocess.run(
                ['powershell', '-Command', ps_cmd],
                capture_output=True, text=True, timeout=5,
                **win32_no_window_kwargs(),
            )
            if 'IMAGE' not in result.stdout:
                return False, None
            # Try to get filename from FileDropList
            label = None
            try:
                r2 = subprocess.run(
                    ['powershell', '-Command',
                     'Add-Type -AssemblyName System.Windows.Forms; '
                     '$files = [System.Windows.Forms.Clipboard]::GetFileDropList(); '
                     'if ($files -ne $null -and $files.Count -gt 0) '
                     '{ Write-Output $files[0] }'],
                    capture_output=True, text=True, timeout=3,
                    **win32_no_window_kwargs(),
            )
                if r2.stdout.strip():
                    label = Path(r2.stdout.strip()).name
            except Exception:
                pass
            return True, label

    except Exception:
        return False, None
    return False, None


def _extract_clipboard_image(target_path: str) -> bool:
    """Extract clipboard image as PNG to target_path. Returns True on success."""
    system = platform.system()
    try:
        if system == "Darwin":
            # Use osascript to write clipboard as PNG
            escaped = target_path.replace('"', '\\"')
            applescript = (
                f'set f to open for access (POSIX file "{escaped}")'
                ' with write permission\n'
                'set eof f to 0\n'
                'try\n'
                '  write (the clipboard as «class PNGf») to f\n'
                'end try\n'
                'close access f'
            )
            subprocess.run(
                ['osascript', '-e', applescript],
                capture_output=True, timeout=5,
                **win32_no_window_kwargs(),
            )
            path = Path(target_path)
            return path.exists() and path.stat().st_size > 0

        elif system == "Linux":
            with open(target_path, 'wb') as f:
                subprocess.run(
                    ['xclip', '-selection', 'clipboard', '-t',
                     'image/png', '-o'],
                    stdout=f, timeout=5,
                    **win32_no_window_kwargs(),
            )
            path = Path(target_path)
            return path.exists() and path.stat().st_size > 0

        elif system == "Windows":
            ps_cmd = (
                'Add-Type -AssemblyName System.Windows.Forms; '
                'Add-Type -AssemblyName System.Drawing; '
                '$img = [System.Windows.Forms.Clipboard]::GetImage(); '
                f'$img.Save("{target_path}", '
                '[System.Drawing.Imaging.ImageFormat]::Png)'
            )
            subprocess.run(
                ['powershell', '-Command', ps_cmd],
                capture_output=True, timeout=5,
                **win32_no_window_kwargs(),
            )
            path = Path(target_path)
            return path.exists() and path.stat().st_size > 0

    except Exception as e:
        logger.debug("clipboard image extract failed: %s", e)
    return False


async def interactive(init_auto_evolve: bool = False):
    if not sys.stdin.isatty():
        print("This client requires a real terminal (TTY).", file=sys.stderr); return

    try: conn = await daemon_manager.ensure_connected()
    except Exception as e:
        print(f"Failed to connect to emrgd: {e}", file=sys.stderr); return
    logger.info("connected to emrgd")

    # Session setup
    cwd = os.getcwd()
    session_id = generate_session_id(Path(cwd))
    project_name = Path(cwd).name

    # Send init_auto_evolve if requested (before ping, so daemon
    # processes it before any user interaction starts)
    if init_auto_evolve:
        await conn.send_command("init_auto_evolve", cwd=cwd)
        # Read the response to consume it (daemon replies {"ok": ...} synchronously)
        try:
            await conn.recv(timeout=5)
        except asyncio.TimeoutError:
            pass

    await conn.send_command("ping")
    term = Terminal(); stdin_fd = sys.stdin.fileno()
    stdin_queue: asyncio.Queue = asyncio.Queue()

    def _status_left(title: str, sid: str, model: str = "") -> str:
        """Format left status: session title + short ID + current model."""
        parts = []
        if title:
            parts.append(f"{title} ({sid[:8]})")
        else:
            parts.append(sid)
        if model:
            parts.append(f"[{model}]")
        return " ".join(parts)
    busy = False; server_id = ""; need_new_assistant = False; session_title = ""
    current_model = ""  # model name tracked independently of server_id (rant 2026-08-11T20:02:43)

    status = StatusLine(left=_status_left(session_title, session_id, current_model), center="connecting...")
    inp = InputWidget(); chat = ChatHistory()
    term.mount(status=status, composer=inp, chat=chat)

    loop = asyncio.get_event_loop()
    history, paste_mode, stream_buffer = [], False, ""
    _pending_images: list[dict] = []  # images accumulated during current input
    history_index: int = -1  # -1 = editing, 0..len-1 = navigating history
    history_saved_input: str = ""  # saved input when navigating history
    msg_count = 0
    _welcomed = False  # show welcome message once on first connect
    _request_start: float = 0.0  # timestamp when current request started
    _elapsed_task: asyncio.Task | None = None  # background timer task
    # P1 queue-injection client side (daemon #655): messages sent while the
    # session is busy are queued daemon-side (task_queued). Track them here so
    # `queued_requeue` can re-send with the same request id (without re-adding
    # chat rows) and `queued_cancelled` clears on abort/disconnect.
    _queued_sends: list[dict] = []  # {"id", "prompt", "images"}

    def _short_path(p: str) -> str:
        home = os.path.expanduser("~")
        if p.startswith(home):
            p = "~" + p[len(home):]
        if len(p) > 30:
            p = "…" + p[-29:]
        return p

    def _update_left_extra() -> None:
        if msg_count > 0:
            status.update(left_extra=f"· {msg_count} msgs · {_short_path(cwd)}")
        else:
            status.update(left_extra="Enter=send  Esc=quit  /help")
    _update_left_extra()

    _status_base: str = ""  # base center text without timer, for elapsed timer overlay
    _last_center: str = ""  # last center text set via status.update, for timer overlay

    async def _run_elapsed_timer() -> None:
        """Background task: update status line elapsed time and terminal title every second while busy."""
        nonlocal _request_start
        while busy:
            try:
                elapsed = int(time.time() - _request_start)
                mins, secs = divmod(elapsed, 60)
                timer = f"[{mins}:{secs:02d}]"
                status.elapsed = timer
                term.set_title(f"{timer} {session_title or session_id} @ {project_name}")
                term.render()
            except Exception as e:
                logger.error("elapsed timer error: %s", e)
            await asyncio.sleep(1)

    tool_args: dict[str, dict] = {}  # track tool arguments by tool_call_id for diff rendering
    _tool_start_times: dict[str, float] = {}  # track tool start time by tool_call_id for timing logs

    # Selector state — each selector has an active flag, widget ref, and pending flag.
    session_sel = SelectorState()
    delete_sel = SelectorState()
    project_sel = SelectorState()
    model_sel = SelectorState()
    rewind_sel = SelectorState()
    task_sel = SelectorState()
    _rant_project: str | None = None  # Set after project selection, used on next Enter
    _skills_confirm: tuple | None = None  # (skill_name, install_cmd) — next Enter answers the prompt

    # Command autocomplete state (shows dropdown when user types /)
    _autocomplete_active = False
    _autocomplete_widget: CommandDropdown | None = None

    # Render throttling: limit renders during streaming to ~60fps
    _last_render_time = 0.0
    _RENDER_MIN_INTERVAL = 0.016  # ~60fps

    def _render_throttled():
        nonlocal _last_render_time
        now = time.monotonic()
        if now - _last_render_time >= _RENDER_MIN_INTERVAL:
            _last_render_time = now
            term.render()

    async def read_server():
        nonlocal stream_buffer, status, history, chat, busy, server_id, need_new_assistant, session_id, session_title, msg_count, tool_args, _welcomed
        nonlocal current_model
        nonlocal _last_center, _elapsed_task, conn
        nonlocal _request_start

        async def _reconnect():
            """Attempt reconnection — blocks until successful."""
            nonlocal conn, busy, _elapsed_task
            # stop elapsed timer
            if _elapsed_task is not None:
                _elapsed_task.cancel(); _elapsed_task = None
            busy = False  # pending request is lost
            _queued_sends.clear()  # daemon drops the queue on disconnect (queued_cancelled)
            chat.add("system", "⏸ server connection lost — reconnecting...")
            status.update(center="reconnecting...")
            term.render()
            # close stale connection
            try: await conn.close()
            except Exception: pass
            # Rant 2026-08-09T13:16:36 ⑤: spawn 节流命中后提示宿主手动启动
            # （否则每 1s 静默重试 spawn 一台新 daemon，Windows 上即弹窗风暴）。
            _throttle_warned = False
            while True:
                try:
                    await asyncio.sleep(1)
                    conn = await daemon_manager.ensure_connected()
                    await conn.send_command("ping")
                    logger.info("reconnected to emrgd")
                    chat.add("system", "✓ server reconnected")
                    status.update(center=server_id or "emrg")
                    term.render()
                    return
                except RuntimeError as e:
                    if "failed to start after" in str(e) and not _throttle_warned:
                        _throttle_warned = True
                        chat.add("system", f"⚠ {e}")
                        status.update(center="daemon down — run 'emrg server'")
                        term.render()
                    continue
                except Exception:
                    continue

        while True:
            try:
                data = await conn.recv(0.1)
            except asyncio.TimeoutError:
                continue
            except ConnectionClosed:
                await _reconnect()
                continue
            if data is None:
                continue
            try:
                if "uptime_seconds" in data:
                    ident = data.get("identity", {}); hid = ident.get("instance_id", "?")[:8]
                    host = ident.get("host_name", "?")
                    model = data.get("model", "")
                    if model:
                        current_model = model
                    server_id = f"{hid} @ {host}"
                    if not _welcomed:
                        _welcomed = True
                        import emrg
                        ver = getattr(emrg, "__version__", "dev")
                        chat.add("system", f"EMRG {ver}  |  {server_id}\nType /help for shortcuts, or just start chatting.")
                        # Auto update-check prompt (rant 2026-08-10T07:12:12):
                        # one-time, non-blocking — query daemon's cached latest
                        # release, show a status line, mark prompted (idempotent).
                        try:
                            await conn.send_command("update_check")
                        except Exception:
                            pass  # never block chat on update check
                    status.update(left=_status_left(session_title, session_id, current_model), center=server_id)
                    term.set_title(f"{session_title or session_id} @ {project_name}")
                    term.render(); continue

                # P1 queue-injection client side (daemon #655): messages sent
                # while the session is busy are queued daemon-side and injected
                # at the next round boundary. The TUI tracks them so a
                # queued_requeue re-sends with the same request id.
                if data.get("type") == "task_queued":
                    # Daemon queued our task (session busy). The user row is
                    # already shown; confirm the queue position.
                    pos = data.get("position", 0)
                    chat.add("system", f"⏳ Queued (position {pos}) — will run after the current turn.")
                    chat.dirty = True; term.render()
                    continue

                if data.get("type") == "steer_committed":
                    # Injected into the running turn — no longer needs requeue.
                    rid = data.get("request_id", "")
                    if rid:
                        _queued_sends[:] = [q for q in _queued_sends if q.get("id") != rid]
                    continue

                if data.get("type") == "queued_requeue":
                    # Turn ended with queued messages never injected — re-send
                    # them through the normal path (the daemon lock is released
                    # now). Rows were already added at the original submit, so
                    # do NOT re-add them or double-count msg_count.
                    ids = set(data.get("request_ids", []) or [])
                    to_resend = [q for q in _queued_sends if q.get("id") in ids]
                    _queued_sends.clear()
                    if to_resend:
                        was_busy = busy
                        busy = True; need_new_assistant = True; stream_buffer = ""
                        _request_start = time.time()
                        if _elapsed_task is None:
                            _elapsed_task = asyncio.create_task(_run_elapsed_timer())
                        for i, q in enumerate(to_resend):
                            rid = await conn.send_task(
                                session_id=session_id, cwd=cwd, prompt=q["prompt"],
                                images=q.get("images"), id=q["id"],
                            )
                            # Track every re-sent message the daemon will queue:
                            # re-send #1 starts a new turn (busy=True above), so
                            # re-sends #2+ arrive while busy and get queued
                            # daemon-side (task_queued) — untracked they would be
                            # silently lost at the next queued_requeue. Also
                            # track all re-sends when a turn was already running
                            # (multi-client). steer_committed removes ids that
                            # get injected mid-turn, so the loop converges.
                            if was_busy or i > 0:
                                _queued_sends.append({"id": rid, "prompt": q["prompt"], "images": q.get("images")})
                        chat.add("system", f"→ Re-sending {len(to_resend)} queued message(s).")
                        chat.dirty = True; term.render()
                    continue

                if data.get("type") == "queued_cancelled":
                    if _queued_sends:
                        _queued_sends.clear()
                        chat.add("system", "⏹ Queued message(s) cancelled.")
                        chat.dirty = True; term.render()
                    continue

                # Tool lifecycle: create a ToolCard on start, update on end.
                if data.get("type") == "tool_start":
                    ts = ToolStart.from_dict(data)
                    tool_args[ts.tool_call_id] = ts.arguments  # track for diff rendering
                    _tool_start_times[ts.tool_call_id] = time.time()
                    card = ToolCard(
                        name=ts.tool_name,
                        command=_format_args(ts.arguments, ts.tool_name),
                        status="running",
                        expanded=False,
                    )
                    chat.add(card)
                    _last_center = f"running {ts.tool_name}..."
                    status.update(center=_last_center)
                    _render_throttled()
                    continue

                if data.get("type") == "tool_end":
                    te = ToolEnd.from_dict(data)
                    elapsed = time.time() - _tool_start_times.pop(te.tool_call_id, time.time())
                    logger.info("tool %s %s in %.2fs", te.tool_name,
                                "FAILED" if te.error else "done", elapsed)
                    # Show diff for successful edit operations
                    if te.tool_name == "edit" and not te.error and te.tool_call_id in tool_args:
                        args = tool_args.pop(te.tool_call_id)
                        old_str = args.get("old_string", "")
                        new_str = args.get("new_string", "")
                        if old_str or new_str:
                            diff_widget = Diff(
                                old=old_str,
                                new=new_str,
                                old_label="old",
                                new_label="new",
                                mode="unified",
                            )
                            chat.add(diff_widget)
                    # Show summary for successful write operations
                    elif te.tool_name == "write" and not te.error and te.tool_call_id in tool_args:
                        args = tool_args.pop(te.tool_call_id)
                        fp = args.get("file_path", "?")
                        short_fp = f"…/{PurePath(fp).name}" if len(fp) > 50 else fp
                        content_len = len(args.get("content", ""))
                        chat.add("system", f"✓ Wrote {content_len} bytes to {short_fp}")
                    elif te.tool_call_id in tool_args:
                        tool_args.pop(te.tool_call_id)  # cleanup non-edit tools
                    card = chat.last_tool_card()
                    if card and card.name == te.tool_name:
                        card.update(
                            "failed" if te.error else "done",
                            output=te.content,
                        )
                    else:
                        # Fallback: no matching start card
                        prefix = "✗ " if te.error else "✓ "
                        chat.add("tool", f"{prefix}{te.tool_name} result")
                    chat.dirty = True  # ToolCard updated inside; force ChatHistory re-render
                    need_new_assistant = True
                    _last_center = server_id or "emrg"
                    status.update(center=_last_center)
                    # Title managed by _run_elapsed_timer during busy — don't overwrite
                    term.render()
                    continue

                resp = TaskResponse.from_dict(data)
                if resp.delta and resp.content:
                    if need_new_assistant:
                        md = StreamingMarkdown()
                        md.feed(resp.content)
                        chat.add(md)
                        stream_buffer = resp.content
                        need_new_assistant = False
                    else:
                        stream_buffer += resp.content
                        md = chat.last_markdown()
                        if md:
                            md.feed(resp.content)
                            chat.dirty = True
                        else:
                            # Fallback: no StreamingMarkdown found, use plain text
                            chat.update_last(stream_buffer)
                    logger.debug("ROWS after delta: %d [%s]", len(chat.rows),
                        ', '.join(f'{r.role}={r.content[:30]}' for r in chat.rows if isinstance(r, ChatRow)))
                    _last_center = "streaming..."
                    status.update(center=_last_center); _render_throttled()
                if resp.done:
                    logger.info("response complete, %d chars", len(stream_buffer) if stream_buffer else 0)
                    logger.debug("DONE: stream_buffer=%r", stream_buffer[:80])
                    busy = False
                    # Cancel elapsed timer
                    if _elapsed_task:
                        _elapsed_task.cancel()
                        _elapsed_task = None
                    status.elapsed = ""
                    if stream_buffer:
                        # Final flush: if streaming markdown, it already has content;
                        # if plain text fallback, update the ChatRow
                        md = chat.last_markdown()
                        if not md:
                            chat.update_last(stream_buffer)
                        stream_buffer = ""
                    # Show hints from server (e.g. max tool rounds exceeded)
                    if resp.content and ("Exceeded" in resp.content or "exceeded" in resp.content.lower()):
                        chat.add("system", f"⚠ {resp.content}  Try '继续' to resume.")
                    _last_center = server_id or "emrg"
                    status.update(center=_last_center)
                    term.set_title(f"{session_title or session_id} @ {project_name}")
                    msg_count += 1; _update_left_extra()
                    term.render()
                if "error" in data:
                    err = data["error"]; logger.error("server error: %s", err)
                    chat.add("system", f"Error: {err}"); term.render()

                # Clear result
                if data.get("type") == "clear_result":
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Clear failed: {err}")
                    else:
                        # Clear the TUI chat display
                        chat.rows.clear()
                        chat.dirty = True
                        chat.add("system", "Session cleared — starting fresh.")
                        msg_count = 0
                        _update_left_extra()
                    status.update(center=server_id or "emrg")
                    term.render()
                    continue

                # Session deleted
                if data.get("type") == "session_deleted":
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Delete failed: {err}")
                    else:
                        deleted_sid = data.get("session_id", "")
                        chat.add("system", f"🗑 Session {deleted_sid} deleted.")
                        if deleted_sid == session_id:
                            # Deleted the current session — create a new one
                            new_sid = generate_session_id(Path(cwd))
                            session_id = new_sid
                            session_title = ""
                            chat.rows.clear()
                            chat.dirty = True
                            chat.add("system", f"Created new session {new_sid} — continue chatting.")
                            status.update(left=_status_left("", new_sid, current_model), center=server_id or "emrg")
                            term.set_title(f"{new_sid} @ {project_name}")
                            msg_count = 0
                            _update_left_extra()
                    status.update(center=server_id or "emrg")
                    term.render()
                    continue

                # Rewind result
                if data.get("type") == "rewind_result":
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Rewind failed: {err}")
                    else:
                        # Clear the TUI chat display and reload history
                        chat.rows.clear()
                        chat.dirty = True
                        removed = data.get("removed_count", 0)
                        chat.add("system", f"↶ Session rewound — {removed} messages removed.")
                        msg_count = 0
                        # Reload session state from server
                        await conn.send_command("ping")
                        _update_left_extra()
                    status.update(center=server_id or "emrg")
                    term.render()
                    continue

                # Compact result
                if data.get("type") == "compact_result":
                    # Skip progress notifications (auto-compact "compacting..." messages)
                    if data.get("auto") and data.get("messages_compacted", 1) == 0:
                        continue
                    compacted = data.get("messages_compacted", 0)
                    summary = data.get("summary", "")
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Compact failed: {err}")
                    else:
                        chat.add("system",
                            f"Compact complete — {compacted} messages compressed into summary.\n"
                            f"Summary: {summary[:200]}..."
                        )
                    busy = False
                    msg_count = max(0, msg_count - compacted)
                    _update_left_extra()
                    status.elapsed = ""
                    status.update(center=server_id or "emrg"); term.render()
                    continue

                # Sessions list
                if data.get("type") == "sessions_list":
                    nonlocal session_sel, delete_sel
                    sessions = data.get("sessions", [])
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Error: {err}")
                        session_sel.pending = False
                        delete_sel.pending = False
                    elif delete_sel.pending and sessions:
                        # /delete interactive mode — current session on top with (current) tag
                        delete_sel.pending = False
                        reordered = []
                        if sessions:
                            # Move current session to top
                            for s in sessions:
                                if s.get("session_id") == session_id:
                                    reordered.append(s)
                                    break
                            for s in sessions:
                                if s.get("session_id") != session_id:
                                    reordered.append(s)
                        delete_sel.widget = SessionSelector(reordered, current_session_id=session_id)
                        delete_sel.active = True
                        chat.add(delete_sel.widget)
                        status.update(center="select session to delete: ↑↓ Enter Esc  (j/k vim) — no confirmation")
                    elif session_sel.pending and sessions:
                        # Enter interactive selection mode
                        session_sel.pending = False
                        session_sel.widget = SessionSelector(sessions)
                        session_sel.active = True
                        chat.add(session_sel.widget)
                        status.update(center="select session: ↑↓ Enter Esc  (j/k vim)")
                    else:
                        session_sel.pending = False
                        if sessions:
                            # /sessions also enters interactive selection mode
                            session_sel.widget = SessionSelector(sessions)
                            session_sel.active = True
                            chat.add(session_sel.widget)
                            status.update(center="select session: ↑↓ Enter Esc  (j/k vim)")
                        else:
                            chat.add("system", "No saved sessions yet. Start chatting to create one.")
                    term.render()
                    continue

                # History list (for /rewind)
                if data.get("type") == "history_list":
                    nonlocal rewind_sel
                    messages = data.get("messages", [])
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Error: {err}")
                        rewind_sel.pending = False
                    elif messages:
                        rewind_sel.pending = False
                        rewind_sel.widget = RewindSelector(messages)
                        rewind_sel.active = True
                        chat.add(rewind_sel.widget)
                        status.update(center="select message to rewind to: ↑↓ Enter Esc  (j/k vim)")
                    else:
                        rewind_sel.pending = False
                        chat.add("system", "No user messages in this session to rewind to.")
                    term.render()
                    continue

                # Projects list
                if data.get("type") == "projects_list":
                    nonlocal project_sel
                    projects = data.get("projects", [])
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Error: {err}")
                        project_sel.pending = False
                    elif project_sel.pending and projects:
                        project_sel.pending = False
                        project_sel.widget = ProjectSelector(projects)
                        project_sel.active = True
                        chat.add(project_sel.widget)
                        status.update(center="select project: ↑↓ Enter Esc  (j/k vim)")
                    else:
                        project_sel.pending = False
                        if projects:
                            project_sel.widget = ProjectSelector(projects)
                            project_sel.active = True
                            chat.add(project_sel.widget)
                            status.update(center="select project: ↑↓ Enter Esc  (j/k vim)")
                        else:
                            chat.add("system", "No projects configured. Use emrg in a git repo to auto-register.")
                    term.render()
                    continue

                # Models list response (for /model interactive picker)
                if data.get("type") == "models_list":
                    nonlocal model_sel
                    models = data.get("models", [])
                    current = data.get("current", "")
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Error: {err}")
                        model_sel.pending = False
                    elif models:
                        model_sel.pending = False
                        model_sel.widget = ModelSelector(models, current)
                        model_sel.active = True
                        chat.add(model_sel.widget)
                        status.update(center="select model: ↑↓ Enter Esc  (j/k vim)")
                    else:
                        model_sel.pending = False
                        chat.add("system", "No models configured. Add [[llm.models]] to ~/.emrg/config.toml.")
                    term.render()
                    continue

                # Model set response
                if data.get("type") == "model_set":
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Model switch failed: {err}")
                    else:
                        model_name = data.get("model", "")
                        ctx_win = data.get("context_window", 0)
                        previous = data.get("previous", "")
                        chat.add("system",
                                 f"Model switched: {previous} → {model_name}"
                                 f" (context: {ctx_win:,})")
                        # Track model independently and refresh the left section
                        current_model = model_name
                        status.update(left=_status_left(session_title, session_id, current_model), center=server_id)
                    term.render()
                    continue

                # Tasks list response (for /trigger interactive mode)
                if data.get("type") == "tasks_list":
                    nonlocal task_sel
                    tasks = data.get("tasks", [])
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Error: {err}")
                    elif tasks:
                        task_sel.widget = TaskSelector(tasks)
                        task_sel.active = True
                        task_sel.pending = False
                        chat.add(task_sel.widget)
                        status.update(center="select a task to trigger (↑↓/j/k, Enter, Esc)")
                    else:
                        chat.add("system", "No scheduled tasks found.")
                    term.render()
                    continue

                # Trigger result response
                if data.get("type") == "trigger_result":
                    err = data.get("error", "")
                    name = data.get("name", "?")
                    result = data.get("result", "")
                    detail = data.get("detail", "")
                    if err:
                        chat.add("system", f"Trigger failed: {err}")
                    elif result == "running":
                        chat.add("system", f"Task '{name}' is already running — {detail}")
                    elif result == "triggered":
                        chat.add("system", f"Task '{name}' triggered: {detail}")
                    else:
                        chat.add("system", f"Task '{name}': {result} — {detail}")
                    term.render()
                    continue

                # Skills available result (installable-skills catalog)
                if data.get("type") == "skills_available_result":
                    skills = data.get("skills", [])
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Error: {err}")
                    elif not skills:
                        chat.add("system", "No catalog skills found. Check ~/.emrg/skills/skill-catalog.md")
                    else:
                        lines = ["**Available Skills (catalog):**", ""]
                        for s in skills:
                            mark = "✅ installed" if s.get("installed") else "not installed"
                            if s.get("managed"):
                                mark += " · managed"
                            lines.append(f"- **{s.get('name', '?')}** — {s.get('description', '')} ({mark})")
                        lines.append("")
                        lines.append("Install: `/skills install <name>` · Refresh: `/skills update`")
                        chat.add("system", "\n".join(lines))
                    status.update(center=server_id or "emrg")
                    term.render()
                    continue

                # Skills install result
                if data.get("type") == "skills_install_result":
                    nonlocal _skills_confirm
                    name = data.get("name", "")
                    if data.get("confirm_required"):
                        cmd = data.get("install_command", "")
                        _skills_confirm = (name, cmd)
                        chat.add("system",
                                 f"⚠️  Skill `{name}` needs its CLI installed first:\n"
                                 f"`{cmd}`\n\n"
                                 f"Type `yes` to confirm, or anything else to cancel.")
                    elif data.get("error"):
                        chat.add("system", f"Install failed for `{name}`: {data['error']}")
                    elif data.get("ok"):
                        chat.add("system",
                                 f"✅ Skill `{name}` installed"
                                 + (f" (v{data.get('version', '?')})" if data.get("version") else "")
                                 + ". It will appear in the next session's Available Skills.")
                    status.update(center=server_id or "emrg")
                    term.render()
                    continue

                # Skills update result
                if data.get("type") == "skills_update_result":
                    err = data.get("error", "")
                    checked = data.get("checked", 0)
                    updated = data.get("updated", [])
                    skipped = data.get("skipped", [])
                    errors = data.get("errors", [])
                    if err:
                        chat.add("system", f"Skill update failed: {err}")
                    else:
                        lines = [f"**Skill update check:** {checked} managed skill(s)"]
                        if updated:
                            lines.append(f"Updated: {', '.join(updated)}")
                        if skipped:
                            lines.append(f"Skipped (CLI missing): {', '.join(skipped)}")
                        if errors:
                            lines.append(f"Failed: {', '.join(errors)}")
                        if not updated and not skipped and not errors:
                            lines.append("All up to date.")
                        chat.add("system", "\n".join(lines))
                    status.update(center=server_id or "emrg")
                    term.render()
                    continue

                # Resume result
                if data.get("type") == "resume_result":
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Resume failed: {err}")
                        term.render()
                        continue

                    new_sid = data.get("session_id", "")
                    meta = data.get("meta", {})

                    # Switch session
                    session_id = new_sid

                    # Clear and replay history from disk
                    chat.rows.clear()
                    chat.dirty = True

                    hist_path = Path(cwd) / ".emrg" / "sessions" / session_id / "history.jsonl"
                    record_count = 0
                    if hist_path.exists():
                        for line in hist_path.read_text(encoding="utf-8").splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                r = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            record_count += 1
                            rtype = r.get("type", "")
                            if rtype == "message":
                                role = r.get("role", "")
                                content = r.get("content", "")
                                if role == "user":
                                    chat.add("user", content)
                                elif role == "assistant":
                                    # Use StreamingMarkdown for color rendering (rant #28)
                                    md = StreamingMarkdown()
                                    md.feed(content)
                                    chat.add(md)
                                elif role == "system":
                                    chat.add("system", content)
                                elif role == "tool":
                                    chat.add("tool", content)
                            elif rtype == "summary":
                                chat.add("system",
                                    f"[Session summary from compact #"
                                    f"{r.get('compact_id', '?')}]: "
                                    f"{r.get('content', '')[:300]}")
                            elif rtype == "tool_call":
                                pass
                            elif rtype == "tool_result":
                                chat.add("tool", f"  result: {r.get('content', '')[:500]}")

                    title_extra = ""
                    if meta.get("title"):
                        title_extra = f" [{meta['title']}]"
                        session_title = meta["title"]
                    else:
                        session_title = ""
                    chat.add("system",
                        f"Resumed session {session_id}{title_extra} "
                        f"({meta.get('message_count', record_count)} messages, "
                        f"created {str(meta.get('created_at', ''))[:16].replace('T', ' ')})")
                    status.update(left=_status_left(session_title, session_id, current_model), center=server_id or "emrg")
                    term.set_title(f"{session_title or session_id} @ {project_name}")
                    # Set message count from loaded session
                    msg_count = meta.get("message_count", record_count)
                    _update_left_extra()
                    term.render()
                    continue

                # Rename result
                if data.get("type") == "rename_result":
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Rename failed: {err}")
                    else:
                        new_title = data.get("title", "")
                        session_title = new_title
                        chat.add("system", f"Session renamed to: {new_title}")
                        status.update(left=_status_left(session_title, session_id, current_model), center=server_id or "emrg")
                        term.set_title(f"{session_title} @ {project_name}")
                    term.render()
                    continue

                # Memories list
                if data.get("type") == "memories_list":
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Error: {err}")
                    else:
                        scope = data.get("scope", "project")
                        directory = data.get("directory", "")
                        memories = data.get("memories", [])
                        lines = [f"Memories ({scope}):", f"  Directory: {directory}", ""]
                        if memories:
                            for m in memories:
                                status_tag = f" [{m.get('status', '')}]" if m.get('status') != 'active' else ""
                                lines.append(
                                    f"  [{m.get('type', '?')}] {m.get('title', '?')}{status_tag}"
                                )
                                lines.append(f"    id: {m.get('id', '?')}  file: {m.get('file', '?')}")
                                created = m.get('created_at', '')[:16].replace('T', ' ')
                                event = m.get('event_at', '')[:10]
                                lines.append(f"    recorded: {created}  event: {event}")
                            lines.append("")
                            lines.append("Type /memory <id> to read a specific memory.")
                            lines.append("Type /memory session for session memories.")
                            lines.append("Type /memory project for project memories.")
                        else:
                            lines.append("  (no memories yet)")
                        chat.add("system", "\n".join(lines))
                    term.render()
                    continue

                # Memory content (read)
                if data.get("type") == "update_check":
                    # Auto update-check prompt (rant 2026-08-10T07:12:12):
                    # one-time, non-blocking status line; idempotent per version.
                    if data.get("has_update") and data.get("enabled"):
                        latest = data.get("latest_version", "")
                        prompted = data.get("prompted_version", "")
                        if latest and latest != prompted:
                            import emrg
                            ver = getattr(emrg, "__version__", "dev")
                            chat.add("system",
                                f"New version v{latest} available (current v{ver}) — "
                                f"https://github.com/argszero/emrg/releases")
                            status.update(center=server_id or "emrg")
                            try:
                                await conn.send_command(
                                    "update_check_prompted",
                                    {"version": latest},
                                )
                            except Exception:
                                pass
                    term.render()
                    continue

                if data.get("type") == "memory_content":
                    err = data.get("error", "")
                    if err:
                        chat.add("system", f"Error: {err}")
                    else:
                        mem_file = data.get("file", "?")
                        mem_path = data.get("path", "?")
                        mem_body = data.get("body", "")
                        frontmatter = data.get("frontmatter", {})
                        lines = [
                            f"Memory: {frontmatter.get('title', mem_file)}",
                            f"  id: {data.get('memory_id', '?')}",
                            f"  type: {frontmatter.get('type', '?')}",
                            f"  scope: {frontmatter.get('scope', '?')}",
                            f"  status: {frontmatter.get('status', '?')}",
                            f"  file: {mem_path}",
                            "",
                            mem_body,
                        ]
                        chat.add("system", "\n".join(lines))
                        status.update(center=server_id or "emrg")
                    term.render()
                    continue

            except json.JSONDecodeError: pass

    read_task = asyncio.create_task(read_server())

    # ── Terminal resize handler ──────────────────────────────
    # R123: Windows 无 SIGWINCH + ProactorEventLoop 不支持 add_signal_handler
    # → 轮询线程每 500ms 检测 get_terminal_size 变化；POSIX 保持 SIGWINCH。
    _resize_event = asyncio.Event()
    _win_resize_thread: threading.Thread | None = None
    _win_resize_stop = threading.Event()
    _last_size = os.get_terminal_size() if sys.platform == "win32" else None

    def _on_sigwinch() -> None:
        _resize_event.set()

    if sys.platform == "win32":
        def _poll_resize() -> None:
            nonlocal _last_size
            while not _win_resize_stop.is_set():
                try:
                    size = os.get_terminal_size()
                    if size != _last_size:
                        _last_size = size
                        loop.call_soon_threadsafe(_resize_event.set)
                except (OSError, ValueError):
                    pass
                _win_resize_stop.wait(0.5)

        _win_resize_thread = threading.Thread(
            target=_poll_resize, name="emrg-resize-poll", daemon=True)
        _win_resize_thread.start()
    else:
        sigwinch = getattr(signal, "SIGWINCH", None)
        if sigwinch is not None:
            loop.add_signal_handler(sigwinch, _on_sigwinch)

    # ── Stdin reader ────────────────────────────────────────
    # R123: Windows ProactorEventLoop 无 add_reader → daemon 线程阻塞
    # os.read + call_soon_threadsafe 填充同一 stdin_queue；POSIX 保持
    # asyncio-native add_reader + O_NONBLOCK（rant #SIGWINCH-leak）。
    _win_stdin_thread: threading.Thread | None = None
    _win_stdin_stop = threading.Event()

    def _stdin_reader() -> None:
        try:
            data = os.read(stdin_fd, 4096)
            if data:
                stdin_queue.put_nowait(data)
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            pass

    if sys.platform == "win32":
        def _win_stdin_loop() -> None:
            # ReadConsoleInputW 主路径（rant 2026-08-07T21:35:47）：
            # os.read 字节流按 OEM 代码页（中文系统 GBK）交付 IME 字符，
            # UTF-8 假设的输入链必然乱码；宽字符 API 直接给 UTF-16。
            from emrg.client.python_tui.win32 import (
                flush_console_input,
                read_console_unicode,
            )
            try:
                flush_console_input(stdin_fd)  # 丢弃切换前的字节流残余
            except (OSError, ValueError):
                pass
            while not _win_stdin_stop.is_set():
                try:
                    data = read_console_unicode(stdin_fd)
                    if data:
                        loop.call_soon_threadsafe(stdin_queue.put_nowait, data)
                    else:
                        _win_stdin_stop.wait(0.005)  # 防忙轮询
                except (OSError, ValueError):
                    break

        _win_stdin_thread = threading.Thread(
            target=_win_stdin_loop, name="emrg-stdin-reader", daemon=True)
        _win_stdin_thread.start()
    else:
        if fcntl is not None:  # POSIX-only（Windows 无 fcntl）
            _stdin_flags = fcntl.fcntl(stdin_fd, fcntl.F_GETFL)
            fcntl.fcntl(stdin_fd, fcntl.F_SETFL, _stdin_flags | os.O_NONBLOCK)
        loop.add_reader(stdin_fd, _stdin_reader)

    def _handle_selector_nav(data: bytes, widget) -> bool:
        """Handle arrow key and j/k navigation for any selector widget.
        Returns True if navigation was handled, False otherwise.
        """
        if len(data) >= 3 and data[0] == 0x1B and data[1] == 0x5B:
            c = data[2]
            if c == 0x41:  # Up
                widget.move_up()
                return True
            elif c == 0x42:  # Down
                widget.move_down()
                return True
        if data == b"j":
            widget.move_down()
            return True
        if data == b"k":
            widget.move_up()
            return True
        return False

    async def handle_key(data: bytes) -> bool:
        nonlocal inp, status, history, paste_mode, stream_buffer, conn, chat, busy, need_new_assistant, session_id, session_title, msg_count, cwd
        nonlocal current_model
        nonlocal session_sel, delete_sel, project_sel, model_sel, rewind_sel, task_sel
        nonlocal history_index, history_saved_input
        nonlocal _autocomplete_active, _autocomplete_widget
        nonlocal _request_start, _last_center, _elapsed_task, _pending_images
        nonlocal _skills_confirm
        if len(data) == 0: return True
        if data == b"\x1b[200~": paste_mode = True; return True
        if data == b"\x1b[201~":
            paste_mode = False
            # After paste, check clipboard for images
            has_image, label = _detect_clipboard_image()
            if has_image:
                images_dir = Path(cwd) / ".emrg" / "sessions" / session_id / "images"
                images_dir.mkdir(parents=True, exist_ok=True)
                counter = len(_pending_images) + 1
                tmp_path = images_dir / f"_clipboard_tmp_{counter}.png"
                if _extract_clipboard_image(str(tmp_path)):
                    # Re-read and save with dedup via session's hash convention
                    import hashlib
                    data = tmp_path.read_bytes()
                    h = hashlib.blake2b(data, digest_size=4).hexdigest()
                    safe_label = "".join(
                        c if c.isalnum() or c in "._-" else "_" for c in (label or f"Image{counter}")
                    )[:40].rstrip("._") or "image"
                    filename = f"{safe_label}_{h}.png"
                    final_path = images_dir / filename
                    # Dedup: check if already exists
                    if not final_path.exists():
                        tmp_path.rename(final_path)
                    else:
                        tmp_path.unlink()
                    placeholder = f"[📷 {label or f'Image {counter}'}]"
                    _pending_images.append({
                        "path": str(final_path),
                        "label": placeholder,
                        "position": len(inp.text),
                    })
                    inp.insert(placeholder + "\n")
                    logger.info("clipboard image saved: %s", filename)
            term.render()
            return True

        # ── ESC interrupt when busy ──────────────────────────
        # Mimics Claude Code: Esc stops the current response mid-turn,
        # keeping work done so far. Dialogs (selector/autocomplete) are
        # handled below — this only fires when LLM is actively responding.
        if data == b"\x1b" and busy:
            busy = False
            if _elapsed_task:
                _elapsed_task.cancel()
                _elapsed_task = None
            status.elapsed = ""
            # Send cancel to daemon so it stops tool/LLM processing
            await conn.send_command("cancel")
            chat.add("system", "⏸ Interrupted — response stopped. You can continue.")
            _last_center = server_id or "emrg"
            status.update(center=_last_center)
            chat.dirty = True; term.render()
            return True

        # ── Session selector mode ──────────────────────────
        if session_sel.active and session_sel.widget:
            if data == b"\x1b":  # Esc — cancel selection
                session_sel.active = False
                chat.add("system", "Session selection cancelled.")
                session_sel.widget = None
                status.update(center=server_id or "emrg")
                chat.dirty = True; term.render()
                return True
            if data == b"\r" or data == b"\n":  # Enter — confirm
                sid = session_sel.widget.selected_session_id
                session_sel.active = False
                session_sel.widget = None
                if sid:
                    await conn.send_command("resume_session", session_id=sid, cwd=cwd)
                    status.update(center=f"resuming {sid}...")
                    term.render()
                else:
                    chat.add("system", "No session selected.")
                    status.update(center=server_id or "emrg")
                    term.render()
                return True
            if _handle_selector_nav(data, session_sel.widget):
                chat.dirty = True; term.render()
                return True
            # Ignore other keys when in selector mode
            return True

        # ── Delete session selector mode ────────────────────
        if delete_sel.active and delete_sel.widget:
            if data == b"\x1b":  # Esc — cancel
                delete_sel.active = False
                chat.add("system", "Delete cancelled.")
                delete_sel.widget = None
                status.update(center=server_id or "emrg")
                chat.dirty = True; term.render()
                return True
            if data == b"\r" or data == b"\n":  # Enter — delete immediately
                sid = delete_sel.widget.selected_session_id
                delete_sel.active = False
                delete_sel.widget = None
                if sid:
                    await conn.send_command("delete_session", session_id=sid, cwd=cwd)
                    status.update(center=f"deleting {sid}...")
                    term.render()
                else:
                    chat.add("system", "No session selected.")
                    status.update(center=server_id or "emrg")
                    term.render()
                return True
            if _handle_selector_nav(data, delete_sel.widget):
                chat.dirty = True; term.render()
                return True
            return True

        # ── Project selector mode ──────────────────────────
        if project_sel.active and project_sel.widget:
            if data == b"\x1b":  # Esc — cancel selection
                project_sel.active = False
                chat.add("system", "Project selection cancelled.")
                project_sel.widget = None
                status.update(center=server_id or "emrg")
                chat.dirty = True; term.render()
                return True
            if data == b"\r" or data == b"\n":  # Enter — confirm
                pname = project_sel.widget.selected_project_name
                project_sel.active = False
                project_sel.widget = None
                if pname:
                    nonlocal _rant_project
                    _rant_project = pname
                    chat.add("system", f"Rant to project '@{pname}' — type your message and press Enter:")
                    status.update(center=f"rant to @{pname}")
                else:
                    chat.add("system", "No project selected.")
                    status.update(center=server_id or "emrg")
                chat.dirty = True; term.render()
                return True
            if _handle_selector_nav(data, project_sel.widget):
                chat.dirty = True; term.render()
                return True
            # Ignore other keys when in project selector mode
            return True

        # ── Model selector mode ──────────────────────────
        if model_sel.active and model_sel.widget:
            if data == b"\x1b":  # Esc — cancel selection
                model_sel.active = False
                chat.add("system", "Model selection cancelled.")
                model_sel.widget = None
                status.update(center=server_id or "emrg")
                chat.dirty = True; term.render()
                return True
            if data == b"\r" or data == b"\n":  # Enter — confirm
                mname = model_sel.widget.selected_model_name
                model_sel.active = False
                model_sel.widget = None
                if mname:
                    await conn.send_command("set_model", model=mname)
                    status.update(center=f"switching model to {mname}...")
                else:
                    chat.add("system", "No model selected.")
                    status.update(center=server_id or "emrg")
                chat.dirty = True; term.render()
                return True
            if _handle_selector_nav(data, model_sel.widget):
                chat.dirty = True; term.render()
                return True
            # Ignore other keys when in model selector mode
            return True

        # ── Rewind selector mode ──────────────────────────
        if rewind_sel.active and rewind_sel.widget:
            if data == b"\x1b":  # Esc — cancel selection
                rewind_sel.active = False
                chat.add("system", "Rewind cancelled.")
                rewind_sel.widget = None
                status.update(center=server_id or "emrg")
                chat.dirty = True; term.render()
                return True
            if data == b"\r" or data == b"\n":  # Enter — confirm
                idx = rewind_sel.widget.selected_record_index
                rewind_sel.active = False
                rewind_sel.widget = None
                if idx is not None:
                    await conn.send_command("rewind_session", session_id=session_id,
                                            cwd=cwd, record_index=idx)
                    status.update(center=f"rewinding to message #{idx}...")
                    term.render()
                else:
                    chat.add("system", "No message selected.")
                    status.update(center=server_id or "emrg")
                    term.render()
                return True
            if _handle_selector_nav(data, rewind_sel.widget):
                chat.dirty = True; term.render()
                return True
            # Ignore other keys when in rewind selector mode
            return True

        # ── Task selector mode ──────────────────────────
        if task_sel.active and task_sel.widget:
            if data == b"\x1b":  # Esc — cancel selection
                task_sel.active = False
                chat.add("system", "Task selection cancelled.")
                task_sel.widget = None
                status.update(center=server_id or "emrg")
                chat.dirty = True; term.render()
                return True
            if data in (b"\r", b"\n"):  # Enter — confirm
                task_name = task_sel.widget.selected_task_name
                task_sel.active = False
                task_sel.widget = None
                if task_name:
                    await conn.send_command("trigger_task", name=task_name,
                                            session_id=session_id, cwd=cwd)
                    chat.add("system", f"Triggering task: {task_name}")
                    status.update(center=f"triggering {task_name}...")
                else:
                    chat.add("system", "No task selected.")
                    status.update(center=server_id or "emrg")
                chat.dirty = True; term.render()
                return True
            if _handle_selector_nav(data, task_sel.widget):
                chat.dirty = True; term.render()
                return True
            # Ignore other keys when in task selector mode
            return True

        # ── Command autocomplete: recompute on every keystroke ──
        if not session_sel.active and not rewind_sel.active and not task_sel.active and not busy:
            text_stripped = inp.text.lstrip()
            if text_stripped.startswith("/"):
                cmd_prefix = text_stripped.split(None, 1)[0]
                # If the prefix already is a full command (with args or not),
                # don't show autocomplete — let Enter submit directly
                if cmd_prefix in _COMMAND_HELP:
                    if _autocomplete_active:
                        _autocomplete_active = False
                        if _autocomplete_widget:
                            chat.remove(_autocomplete_widget)
                        chat.dirty = True
                elif not _autocomplete_active:
                    _autocomplete_active = True
                    _autocomplete_widget = CommandDropdown(prefix=cmd_prefix)
                    _autocomplete_widget.visible = True
                    chat.add(_autocomplete_widget)
                    chat.dirty = True
                else:
                    _autocomplete_widget._recompute(cmd_prefix)
                    chat.dirty = True
            elif _autocomplete_active:
                _autocomplete_active = False
                if _autocomplete_widget:
                    chat.remove(_autocomplete_widget)
                chat.dirty = True

        # ── Command autocomplete: intercept navigation keys ──
        if _autocomplete_active and _autocomplete_widget:
            if data == b"\x1b":  # Esc — dismiss and clear input
                _autocomplete_active = False
                chat.remove(_autocomplete_widget)
                # Clear the partial command so autocomplete doesn't re-trigger
                inp.text = ""; inp.cursor = 0; inp.dirty = True
                chat.dirty = True; term.render()
                return True
            if data == b"\r" or data == b"\n":  # Enter — complete
                cmd = _autocomplete_widget.selected_command
                if cmd:
                    old_prefix = inp.text.lstrip().split(None, 1)[0]
                    rest = inp.text.lstrip()[len(old_prefix):]
                    leading = inp.text[:len(inp.text) - len(inp.text.lstrip())]
                    inp.text = leading + cmd + rest
                    inp.cursor = len(inp.text)
                    inp.dirty = True
                _autocomplete_active = False
                chat.remove(_autocomplete_widget)
                chat.dirty = True; term.render()
                return True
            if len(data) >= 3 and data[0] == 0x1B and data[1] == 0x5B:
                c = data[2]
                if c == 0x41:  # Up
                    _autocomplete_widget.move_up()
                    chat.dirty = True; term.render()
                    return True
                elif c == 0x42:  # Down
                    _autocomplete_widget.move_down()
                    chat.dirty = True; term.render()
                    return True
            # Tab in autocomplete mode: cycle selection
            if data == b"\t" or data[0] == 0x09:
                cmds = _autocomplete_widget._matching
                if cmds:
                    _autocomplete_widget.selected_index = (
                        (_autocomplete_widget.selected_index + 1) % len(cmds)
                    )
                    _autocomplete_widget._dirty = True
                    chat.dirty = True; term.render()
                return True
            # For any other key: fall through to normal processing
            # (autocomplete will recompute on the next keystroke)

        b = data[0]
        if b in (0x03, 0x04): return False
        # Ctrl+A (home), Ctrl+E (end), Ctrl+U (delete to line start),
        # Ctrl+W (delete word), Ctrl+K (kill to end of line)
        if b == 0x01: inp.move_home(); term.render(); return True
        if b == 0x05: inp.move_end(); term.render(); return True
        if b == 0x15: inp.delete_to_beginning_of_line(); term.render(); return True
        if b == 0x17: inp.delete_word_backward(); term.render(); return True
        if b == 0x0B: inp.delete_to_end_of_line(); term.render(); return True
        if b >= 0x80:
            try:
                for c in data.decode("utf-8"): inp.insert(c)
            except UnicodeDecodeError: pass
            if not paste_mode: term.render()
            return True

        # Tab: command completion (when / prefix) or tool card toggle
        if b == 0x09:
            text = inp.text.lstrip()
            if text.startswith("/"):
                cmd_prefix = text.split(None, 1)[0]
                # If autocomplete already handles this (partial command), skip
                if cmd_prefix not in _COMMAND_HELP:
                    # Partial command — let autocomplete intercept handle it
                    pass
                # else: full command typed, Tab does nothing (Enter to submit)
            else:
                # Tool card toggle
                tool_cards = [r for r in chat.rows if isinstance(r, ToolCard)]
                if tool_cards:
                    changed = False
                    for tc in tool_cards:
                        if tc.output and not tc.expanded:
                            tc.toggle()
                            changed = True
                            break
                    if not changed and tool_cards:
                        tool_cards[-1].toggle()
                    chat.dirty = True; term.render()
            return True

        if b == 0x1B and len(data) >= 3:
            if data[1] == 0x5B:
                c = data[2]
                if c == 0x41:  # Up
                    avail = max(1, term.viewport.viewport_width - 2)
                    if inp._cursor_vrow(avail) == 0:
                        # Cursor on first visual row → navigate command history
                        if history:
                            if history_index == -1:
                                history_saved_input = inp.text
                                history_index = len(history) - 1
                            elif history_index > 0:
                                history_index -= 1
                            inp.text = history[history_index]
                            inp.cursor = len(inp.text)
                            inp.dirty = True
                    else:
                        inp.move_up(avail)
                elif c == 0x42:  # Down
                    avail = max(1, term.viewport.viewport_width - 2)
                    rows = inp._visual_rows(avail)
                    if inp._cursor_vrow(avail) >= len(rows) - 1:
                        # Cursor on last visual row → navigate command history forward
                        if history_index >= 0:
                            if history_index < len(history) - 1:
                                history_index += 1
                                inp.text = history[history_index]
                            else:
                                history_index = -1
                                inp.text = history_saved_input
                            inp.cursor = len(inp.text)
                            inp.dirty = True
                    else:
                        inp.move_down(avail)
                elif c == 0x43: inp.move_right()
                elif c == 0x44: inp.move_left()
                elif c == 0x48: inp.move_home()
                elif c == 0x46: inp.move_end()
                elif c == 0x33 and len(data) >= 4 and data[3] == 0x7E: inp.delete_forward()
            term.render(); return True
        if b in (0x7F, 0x08):
            inp.backspace()
            if not paste_mode: term.render()
            return True
        if b == 0x0D:
            if paste_mode:
                if not inp.text.endswith("\n"): inp.insert("\n")
                term.render(); return True
            logger.debug("ENTER: text=%r busy=%s len=%d", inp.text, busy, len(inp.text))
            text = inp.text.strip()
            if text:
                if text.lower() in ("quit", "exit"): return False

                # If a rant project was selected, use this message as the rant
                if _rant_project:
                    await conn.send_command("rant", message=text, project=_rant_project,
                                            timestamp=datetime.now().isoformat())

                    chat.add("system", f"Rant recorded (@{_rant_project}). The evolution system will review it.")
                    _rant_project = None
                    status.update(center=server_id or "emrg")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Pending /skills install confirmation — next line is the answer
                if _skills_confirm is not None:
                    name, cmd = _skills_confirm
                    _skills_confirm = None
                    if text.lower() in ("y", "yes"):
                        await conn.send_command("skills_install", name=name, confirmed=True)
                        chat.add("system", f"Confirmed — installing `{name}` (CLI: `{cmd}`)…")
                    else:
                        chat.add("system", "Install cancelled.")
                    status.update(center=server_id or "emrg")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /memory command
                if text.lower().startswith("/memory"):
                    parts = text.split(None, 1)
                    sub = parts[1].strip() if len(parts) > 1 else ""
                    scope = "project"
                    mem_id = ""
                    if sub:
                        # /memory session → list session memories
                        # /memory <id> → read specific memory
                        if sub.lower() == "session":
                            scope = "session"
                        elif sub.lower() == "project":
                            scope = "project"
                        else:
                            mem_id = sub
                            # Could be a read request — send as read
                            await conn.send_command("read_memory", scope=scope,
                                                    memory_id=mem_id, session_id=session_id,
                                                    cwd=cwd)
        
                            status.update(center="reading memory...")
                            inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                            return True

                    # List memories
                    await conn.send_command("list_memories", scope=scope,
                                            session_id=session_id, cwd=cwd)

                    status.update(center=f"listing {scope} memories...")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /compact command
                if text.lower() == "/compact":
                    await conn.send_command("compact", session_id=session_id, cwd=cwd)

                    status.update(center="compacting...")
                    chat.add("system", "Compact requested — summarizing conversation...")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /rename command
                if text.lower().startswith("/rename"):
                    parts = text.split(None, 1)
                    title = parts[1].strip() if len(parts) > 1 else ""
                    await conn.send_command("rename_session", session_id=session_id,
                                            cwd=cwd, title=title)

                    if title:
                        status.update(center=f"renaming to {title}...")
                        chat.add("system", f"Renaming session to: {title}")
                    else:
                        status.update(center="generating title...")
                        chat.add("system", "Auto-generating title...")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /sessions command
                if text.lower() == "/sessions":
                    await conn.send_command("list_sessions", cwd=cwd)

                    status.update(center="listing sessions...")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /delete command
                if text.lower().startswith("/delete"):
                    parts = text.split(None, 1)
                    if len(parts) < 2:
                        # No argument: fetch sessions and enter interactive delete mode
                        delete_sel.pending = True
                        await conn.send_command("list_sessions", cwd=cwd)
                        status.update(center="loading sessions for delete...")
                    else:
                        target_sid = parts[1].strip()
                        # Direct delete by session ID
                        await conn.send_command("delete_session", session_id=target_sid, cwd=cwd)
                        status.update(center=f"deleting {target_sid}...")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /skills command (list / available / install / update)
                if text.lower().startswith("/skills"):
                    parts = text.split(None, 1)
                    sub = parts[1].strip() if len(parts) > 1 else ""
                    sub_l = sub.lower()
                    if sub_l == "available":
                        # Installable-skills catalog (rant 2026-08-08T10:14:29)
                        await conn.send_command("skills_available")
                        status.update(center="checking available skills…")
                    elif sub_l.startswith("install "):
                        name = sub[8:].strip()
                        if not name:
                            chat.add("system", "Usage: /skills install <name>")
                        else:
                            await conn.send_command("skills_install", name=name, confirmed=False)
                            status.update(center=f"installing {name}…")
                    elif sub_l == "update":
                        await conn.send_command("skills_update")
                        status.update(center="checking skill updates…")
                    else:
                        skills = load_skills()
                        if skills:
                            lines = ["**Loaded Skills:**", ""]
                            for s in skills:
                                lines.append(f"- **{s.name}** ({s.source}) — {s.description}")
                            chat.add("system", "\n".join(lines))
                        else:
                            chat.add("system", "No skills loaded. Add .md files to ~/.emrg/skills/ or .emrg/skills/")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /image token — detect independent /image token anywhere in input
                if "/image" in inp.text:
                    def _is_image_token(s, i):
                        """Check if /image at position i is an independent token."""
                        before_ok = i == 0 or s[i-1].isspace()
                        after_ok = i + 6 == len(s) or s[i+6].isspace()
                        return before_ok and after_ok
                    pos = inp.text.find("/image")
                    while pos != -1:
                        if _is_image_token(inp.text, pos):
                            has_image, label = _detect_clipboard_image()
                            if has_image:
                                images_dir = Path(cwd) / ".emrg" / "sessions" / session_id / "images"
                                images_dir.mkdir(parents=True, exist_ok=True)
                                counter = len(_pending_images) + 1
                                tmp_path = images_dir / f"_clipboard_tmp_{counter}.png"
                                if _extract_clipboard_image(str(tmp_path)):
                                    import hashlib
                                    data = tmp_path.read_bytes()
                                    h = hashlib.blake2b(data, digest_size=4).hexdigest()
                                    safe_label = "".join(
                                        c if c.isalnum() or c in "._-" else "_" for c in (label or f"Image{counter}")
                                    )[:40].rstrip("._") or "image"
                                    filename = f"{safe_label}_{h}.png"
                                    final_path = images_dir / filename
                                    if not final_path.exists():
                                        tmp_path.rename(final_path)
                                    else:
                                        tmp_path.unlink()
                                    placeholder = f"[📷 {label or f'Image {counter}'}]"
                                    inp.text = inp.text[:pos] + placeholder + inp.text[pos+6:]
                                    # Adjust cursor if it was after the replaced token
                                    if inp.cursor > pos:
                                        inp.cursor += len(placeholder) - 6
                                    _pending_images.append({
                                        "path": str(final_path),
                                        "label": placeholder,
                                        "position": pos,
                                    })
                                    inp.dirty = True
                                    logger.info("/image token: clipboard image saved: %s at pos %d", filename, pos)
                                else:
                                    # Extraction failed — remove the token
                                    inp.text = inp.text[:pos] + inp.text[pos+6:]
                                    if inp.cursor > pos:
                                        inp.cursor -= 6
                                    inp.dirty = True
                                    chat.add("system", "无法从剪贴板提取图片。")
                            else:
                                # No image in clipboard — remove the token
                                inp.text = inp.text[:pos] + inp.text[pos+6:]
                                if inp.cursor > pos:
                                    inp.cursor -= 6
                                inp.dirty = True
                                chat.add("system", "剪贴板中没有图片。请先复制图片到剪贴板（CMD+C 或截图）。")
                            term.render()
                            return True
                        pos = inp.text.find("/image", pos + 1)

                # Handle /version command
                if text.lower() == "/version":
                    import emrg
                    ver = getattr(emrg, "__version__", "dev")
                    chat.add("system", f"EMRG {ver}  |  {server_id}\nSession: {session_title or session_id}\nCWD: {cwd}")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /help command
                if text.lower() == "/help":
                    help_text = """Keyboard Shortcuts
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Editing
  Type text           Insert at cursor
  Enter               Send message
  Esc                 Interrupt response (when busy) / close dialog
  Ctrl+C / Ctrl+D     Quit
  Backspace           Delete left
  Del (Fn+Delete)     Delete right
  Ctrl+U              Delete to line start
  Ctrl+W              Delete word left
  Ctrl+K              Delete to end of line
  ← →                 Move cursor
  Ctrl+A / Ctrl+E     Jump to line start/end
  ↑ ↓                 Navigate history / move between lines
  Home / End          Jump to line start/end
  Opt+Enter           Insert newline (not send)

Navigation
  Scroll/mouse wheel   Browse history (terminal native)
  Tab                  Complete command (/ prefix) or toggle tool card
  /                    Type / to show command menu (↑↓ to select, type to filter)
  j / k                Vim-style up/down in session picker

Commands
  /help               Show this help
  /skills             List loaded skills (user + project)
  /version            Show EMRG version and instance info
  /image              Insert clipboard image into input field
  /compact            Compress conversation history
  /clear              Clear current session and start fresh
  /memory [session|project|<id>]  Browse memories
  /sessions           Interactive session picker (↑↓/j/k to select)
  /resume [id]        Switch to session (no args = interactive picker, ↑↓/j/k)
  /rename [title]     Rename current session
  /delete [id]        Delete session (no args = interactive picker, ↑↓/j/k)
  /rant <msg>         Send feedback to evolution system
  /rant @<project> <msg>  Rant to a specific project
  /rant               Interactive project picker, then type message
  /model [name]        Switch LLM model (no args = interactive picker)
  /trigger             List scheduled tasks (type name to trigger)
  /trigger <name>      Manually trigger a scheduled task now
  quit / exit         Exit EMRG

Streaming
  ● assistant         Markdown + syntax-highlighted
  ◇ tool              Green tool prefix
  ○ system            Dim system messages
  > user              Cyan user messages"""
                    chat.add("system", help_text)
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /clear command
                if text.lower() == "/clear":
                    await conn.send_command("clear_session", session_id=session_id, cwd=cwd)

                    status.update(center="clearing session...")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /rewind command
                if text.lower() == "/rewind":
                    await conn.send_command("list_history", session_id=session_id, cwd=cwd)

                    rewind_sel.pending = True
                    status.update(center="loading session history...")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /rant command
                if text.lower().startswith("/rant"):
                    parts = text.split(None, 2)
                    message = parts[1].strip() if len(parts) > 1 else ""
                    project = None
                    if message.startswith("@"):
                        # /rant @<project> <message> — project-targeted rant
                        sub = text.split(None, 2)
                        project = sub[1][1:]  # strip @
                        message = sub[2].strip() if len(sub) > 2 else ""
                    if not message:
                        if project:
                            chat.add("system", "Usage: /rant @<project> <message>")
                            inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                            return True
                        # /rant without args → interactive project selector
                        project_sel.pending = True
                        await conn.send_command("list_projects")

                        status.update(center="loading projects...")
                        inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                        return True
                    payload = {
                        "message": message,
                        "timestamp": datetime.now().isoformat(),
                    }
                    if project:
                        payload["project"] = project
                    await conn.send_command("rant", **payload)

                    target = f" (@{project})" if project else ""
                    chat.add("system", f"Rant recorded{target}. The evolution system will review it.")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /model command
                if text.lower().startswith("/model"):
                    parts = text.split(None, 1)
                    model_arg = parts[1].strip() if len(parts) > 1 else ""
                    if model_arg:
                        # /model <name> → direct switch
                        await conn.send_command("set_model", model=model_arg)
    
                        status.update(center=f"switching model to {model_arg}...")
                    else:
                        # /model without args → interactive picker
                        model_sel.pending = True
                        await conn.send_command("list_models")
    
                        status.update(center="loading models...")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /trigger command
                if text.lower().startswith("/trigger"):
                    parts = text.split(None, 1)
                    task_name = parts[1].strip() if len(parts) > 1 else ""
                    if task_name:
                        # /trigger <name> → direct trigger
                        await conn.send_command("trigger_task", name=task_name)
                        status.update(center=f"triggering task '{task_name}'...")
                    else:
                        # /trigger without args → list tasks
                        await conn.send_command("list_tasks")
                        status.update(center="loading tasks...")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # Handle /resume command
                if text.lower().startswith("/resume"):
                    parts = text.split(None, 1)
                    if len(parts) < 2:
                        # No argument: enter interactive session selection
                        session_sel.pending = True
                        await conn.send_command("list_sessions", cwd=cwd)
    
                        status.update(center="loading sessions...")
                    else:
                        target_sid = parts[1].strip()
                        # Deactivate selector if active
                        session_sel.active = False
                        session_sel.widget = None
                        session_sel.pending = False
                        await conn.send_command("resume_session", session_id=target_sid, cwd=cwd)
    
                        status.update(center=f"resuming {target_sid}...")
                    inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render()
                    return True

                # P1 queue-injection (daemon #655): sending while busy no longer
                # blocks — the daemon queues the task (task_queued) and injects
                # it at the next round boundary, or re-sends via queued_requeue
                # when the turn ends. Track the send for requeue.
                was_busy = busy

                busy = True; need_new_assistant = True  # rant #32: force new StreamingMarkdown per response
                _request_start = time.time()
                # Cancel any stale timer and start a new one
                if _elapsed_task:
                    _elapsed_task.cancel()
                _elapsed_task = asyncio.create_task(_run_elapsed_timer())
                logger.debug("SUBMIT: text=%r", text)
                chat.add("user", inp.text)
                history.append(text); stream_buffer = ""
                history_index = -1  # reset history navigation on submit
                chat.add("assistant", "")
                msg_count += 1; _update_left_extra()
                logger.debug("ROWS after asst: %d [%s]", len(chat.rows),
                    ', '.join(f'{r.role}={r.content[:20]}' for r in chat.rows if isinstance(r, ChatRow)))
                _last_center = "thinking..."
                status.update(center=_last_center)
                term.render()
                # Drop images whose placeholder was deleted from the input text
                if _pending_images:
                    _pending_images[:] = [img for img in _pending_images if img.get("label") in inp.text]
                images = _pending_images or None
                _pending_images = []
                rid = await conn.send_task(session_id=session_id, cwd=cwd, prompt=text,
                                           images=images)
                if was_busy:
                    _queued_sends.append({"id": rid, "prompt": text, "images": images})
                logger.info("task sent, prompt_len=%d chars", len(text))
            inp.text = ""; inp.cursor = 0; inp.dirty = True; term.render(); return True
        if b == 0x1B and len(data) >= 2 and data[1] in (0x0D, 0x0A):
            inp.insert("\n")
            if not paste_mode: term.render()
            return True
        if b == 0x1B: return True
        if 0x20 <= b <= 0x7E:
            inp.insert(chr(b))
            if not paste_mode: term.render()
            return True
        # Multi-byte UTF-8 (CJK, emoji, etc.) — decode the full sequence
        if b >= 0x80:
            try:
                char = data.decode("utf-8")
                inp.insert(char)
            except UnicodeDecodeError:
                pass
            if not paste_mode: term.render()
            return True
        if b == 0x0A:
            if paste_mode:
                if not inp.text.endswith("\n"): inp.insert("\n")
            else: inp.insert("\n")
            if not paste_mode: term.render()
            return True
        return True

    parser = InputParser()
    try:
        while True:
            # Race stdin queue get against SIGWINCH resize event
            # (add_reader + Queue — no thread pool, no leaked os.read threads)
            stdin_ft = asyncio.ensure_future(stdin_queue.get())
            resize_ft = asyncio.ensure_future(_resize_event.wait())
            done, pending = await asyncio.wait(
                [stdin_ft, resize_ft], return_when=asyncio.FIRST_COMPLETED)

            # Cancel whichever future didn't fire (both are safe to cancel)
            for ft in pending:
                ft.cancel()
                try: await ft
                except (asyncio.CancelledError, Exception): pass

            # Process resize immediately (real-time, no keypress needed)
            if _resize_event.is_set():
                _resize_event.clear()
                try: term.handle_resize()
                except Exception:
                    logger.debug("resize handler failed", exc_info=True)

            # Skip data processing if stdin didn't fire
            if stdin_ft not in done:
                continue

            data = stdin_ft.result()
            if not data: break

            for seq in parser.feed(data):
                if not await handle_key(seq): return
            while parser.has_pending():
                try:
                    more = await asyncio.wait_for(stdin_queue.get(), timeout=0.05)
                    for seq in parser.feed(more):
                        if not await handle_key(seq): return
                except asyncio.TimeoutError:
                    # Flush standalone Escape (Claude Code style: 50ms timer for lone ESC)
                    if parser._buf == bytearray(b'\x1b'):
                        parser._buf.clear()
                        if not await handle_key(b'\x1b'): return
                    break
    except Exception: logger.exception("TUI main loop crashed")
    finally:
        # R123: Windows 无 add_reader（线程 reader）→ remove_reader 需保护；
        # 停掉轮询/读线程（daemon=True 兜底，显式 stop 更干净）。
        if sys.platform == "win32":
            _win_resize_stop.set()
            _win_stdin_stop.set()
        else:
            try:
                loop.remove_reader(stdin_fd)
            except (NotImplementedError, ValueError):
                pass
        logger.info("disconnecting from emrgd")
        read_task.cancel()
        try: await read_task
        except (asyncio.CancelledError, Exception): pass
        try:
            await conn.close()
        except Exception:
            pass
        term.shutdown(); sys.stdout.write("\n"); sys.stdout.flush()


def _format_args(args: dict, tool_name: str = "") -> str:
    """Format tool arguments for compact, human-readable display in the ToolCard header.

    Instead of raw JSON, shows the most relevant argument for each tool type.
    """
    if not args:
        return ""

    # Tool-specific human-readable formats
    if tool_name == "bash":
        cmd = args.get("command", "")
        workdir = args.get("workdir")
        if cmd:
            # Show first non-empty line, truncate long commands
            first_line = ""
            for raw_line in cmd.split("\n"):
                stripped = raw_line.strip()
                if stripped:
                    first_line = stripped
                    break
            if not first_line:
                first_line = cmd.split("\n")[0].strip()
            # Prefix with workdir if set
            prefix = f"[{workdir}] " if workdir else ""
            remaining = 70 - len(prefix)
            # Guard: always show at least 8 chars of the command
            if remaining < 8:
                remaining = 8
                # Truncate the workdir to fit — keep last 20 chars
                if workdir:
                    short_dir = workdir if len(workdir) <= 20 else "…" + workdir[-19:]
                    prefix = f"[{short_dir}] "
                    remaining = 70 - len(prefix)
                    if remaining < 8:
                        remaining = 8
            if len(first_line) > remaining:
                first_line = first_line[:remaining - 3] + "..."
            return prefix + first_line
    elif tool_name in ("read", "write", "edit"):
        fp = args.get("file_path", "")
        if fp:
            name = PurePath(fp).name
            # Compact path display
            short = "…/" + name if len(fp) > 50 else fp
            # Add context: size for write, range for read
            if tool_name == "write":
                content_len = len(args.get("content", ""))
                if content_len >= 1024:
                    short += f" ({content_len // 1024}KB)"
                elif content_len > 0:
                    short += f" ({content_len}B)"
            elif tool_name == "read":
                sl = args.get("start_line") or args.get("offset")
                ll = args.get("line_limit") or args.get("limit")
                if sl and ll:
                    short += f" [L{sl}:L{int(sl) + int(ll)}]"
                elif sl:
                    short += f" [from L{sl}]"
            return short

    # Fallback: JSON dump (truncated)
    arg_str = json.dumps(args, ensure_ascii=False)
    if len(arg_str) > 60:
        arg_str = arg_str[:57] + "..."
    return arg_str


def run_client(init_auto_evolve: bool = False): asyncio.run(interactive(init_auto_evolve=init_auto_evolve))
