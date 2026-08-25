"""Entry point for EMRG daemon: python -m emrg.server"""
import asyncio
import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from emrg.server.daemon import DaemonExit, run_server
from emrg.server.logcontext import session_label


class _TaskColumnFormatter(logging.Formatter):
    """Formatter with dedicated [task] + [session] columns.

    Rant 2026-08-19T10:18:44 ([task]): scheduler TaskHandler logs carry a
    ``task`` extra (per-task LoggerAdapter in scheduler.py); this formatter
    renders it as a short, eye-scanable ``[task-name]`` column.
    Rant 2026-08-24T10:48:50 ([session]): daemon session-core logs carry the
    current session label via the ``emrg.server.logcontext.session_label``
    ContextVar (set around each message's tool loop); rendered as a
    ``[session]`` column (``session_id`` or ``session_id:name``).

    Records without a task/session context fall back to ``-`` so the columns
    never break formatting. Log consumers that grep ``TaskHandler[...]`` keep
    working — the in-message prefix is retained.
    """

    def format(self, record: logging.LogRecord) -> str:
        task = getattr(record, "task", "-")
        record.task = task if task else "-"
        sess = session_label.get()
        record.session = sess if sess else "-"
        return super().format(record)


_fmt = _TaskColumnFormatter(
    "%(asctime)s [%(levelname)s] [%(task)s] [%(session)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _configure_logging() -> None:
    """Write daemon logs to ~/.emrg/emrgd.log so they survive stderr=DEVNULL."""
    log_dir = Path.home() / ".emrg"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "emrgd.log"
    file_handler = RotatingFileHandler(
        str(log_file), maxBytes=10 * 1024 * 1024, backupCount=3,
        encoding="utf-8",  # rant 2026-08-07T14:00Z: default locale code page (GBK on zh-CN Windows) mojibakes CJK log lines
    )
    file_handler.setFormatter(_fmt)
    handlers = [file_handler]
    # StreamHandler only for interactive runs: daemon_manager spawns emrgd
    # with stderr=DEVNULL, so the stream handler is pure waste there — and
    # since _redirect_std_streams() later points sys.stderr at the crash log,
    # a stream handler would duplicate the whole log into it (rant
    # 2026-08-25T09:25:32 wants an *independent* crash log).
    if sys.stderr.isatty():
        stream_handler = logging.StreamHandler()  # visible when run directly
        stream_handler.setFormatter(_fmt)
        handlers.append(stream_handler)

    logging.basicConfig(
        level=logging.DEBUG,
        handlers=handlers,
    )
    # Suppress noisy httpcore/httpx DEBUG logs (rant #24)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Suppress websockets frame-level DEBUG (rant 2026-08-06T10:21:26):
    # 帧级收发日志占 emrgd.log ~90% 纯噪音。设 INFO 保留连接建立/关闭事件，
    # 仅滤掉 DEBUG 帧收发（比 WARNING 更细——不丢连接生命周期信息）。
    logging.getLogger("websockets").setLevel(logging.INFO)


def _redirect_std_streams() -> None:
    """Redirect sys.stdout/sys.stderr to ~/.emrg/emrgd-crash.log (rant
    2026-08-25T09:25:32 — daemon silent death).

    daemon_manager spawns emrgd with stdout/stderr=DEVNULL, so anything that
    bypasses logging — asyncio's default exception handler output, C-level
    abort/assert traces, faulthandler dumps — vanished silently. This gives
    the daemon a second, independent sink for such output, and enables
    faulthandler so even a fatal-signal death (SIGSEGV etc., where no Python
    code runs) leaves a stack dump behind.
    """
    crash_log = Path.home() / ".emrg" / "emrgd-crash.log"
    try:
        stream = open(str(crash_log), "a", encoding="utf-8", buffering=1)
    except OSError:
        return  # best-effort: keep the original DEVNULL sinks
    sys.stdout = stream
    sys.stderr = stream
    try:
        import faulthandler

        faulthandler.enable(file=stream)
    except Exception:
        pass  # best-effort: faulthandler may reject the stream (no fileno)


def main() -> None:
    from emrg.config import load_config

    _configure_logging()
    _redirect_std_streams()
    config = load_config()
    try:
        result = asyncio.run(run_server(config.llm))
    except KeyboardInterrupt:
        # SIGINT delivered at the event-loop poll point escapes the main
        # coroutine — record it here so no stop path is ever silent.
        result = DaemonExit("sigint", 130, None)
        logging.getLogger("emrg.server").info(
            "daemon terminated by SIGINT outside the event loop"
        )
    except SystemExit:
        # POSIX SIGTERM handler (daemon._sigterm_handler) → SystemExit that
        # escaped the loop.
        result = DaemonExit("sigterm", 143, None)
        logging.getLogger("emrg.server").info(
            "daemon terminated by SIGTERM outside the event loop"
        )
    except BaseException:
        result = DaemonExit("crash", 1, traceback.format_exc())
        logging.getLogger("emrg.server").critical(
            "daemon crashed outside the event loop", exc_info=True
        )
    result.write_record()
    sys.exit(result.exit_code)


if __name__ == "__main__":
    main()
