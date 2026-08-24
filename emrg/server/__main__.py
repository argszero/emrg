"""Entry point for EMRG daemon: python -m emrg.server"""
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

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
    stream_handler = logging.StreamHandler()  # also to stderr (visible when run directly)
    stream_handler.setFormatter(_fmt)

    logging.basicConfig(
        level=logging.DEBUG,
        handlers=[file_handler, stream_handler],
    )
    # Suppress noisy httpcore/httpx DEBUG logs (rant #24)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Suppress websockets frame-level DEBUG (rant 2026-08-06T10:21:26):
    # 帧级收发日志占 emrgd.log ~90% 纯噪音。设 INFO 保留连接建立/关闭事件，
    # 仅滤掉 DEBUG 帧收发（比 WARNING 更细——不丢连接生命周期信息）。
    logging.getLogger("websockets").setLevel(logging.INFO)


def main() -> None:
    from emrg.config import load_config
    from emrg.server.daemon import run_server

    _configure_logging()
    config = load_config()
    asyncio.run(run_server(config.llm))


if __name__ == "__main__":
    main()
