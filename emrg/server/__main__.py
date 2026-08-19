"""Entry point for EMRG daemon: python -m emrg.server"""
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Write daemon logs to ~/.emrg/emrgd.log so they survive stderr=DEVNULL
_log_dir = Path.home() / ".emrg"
_log_dir.mkdir(parents=True, exist_ok=True)
_log_file = _log_dir / "emrgd.log"


class _TaskColumnFormatter(logging.Formatter):
    """Formatter with a dedicated [task] column (rant 2026-08-19T10:18:44).

    Scheduler TaskHandler logs carry a ``task`` extra (per-task LoggerAdapter
    in scheduler.py); this formatter renders it as a short, eye-scanable
    ``[task-name]`` column. Records without a task context (daemon core,
    TaskScheduler-level lines) fall back to ``-`` so the column never breaks
    formatting. Log consumers that grep ``TaskHandler[...]`` keep working —
    the in-message prefix is retained.
    """

    def format(self, record: logging.LogRecord) -> str:
        task = getattr(record, "task", "-")
        record.task = task if task else "-"
        return super().format(record)


_fmt = _TaskColumnFormatter(
    "%(asctime)s [%(levelname)s] [%(task)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_file_handler = RotatingFileHandler(
    str(_log_file), maxBytes=10 * 1024 * 1024, backupCount=3,
    encoding="utf-8",  # rant 2026-08-07T14:00Z: default locale code page (GBK on zh-CN Windows) mojibakes CJK log lines
)
_file_handler.setFormatter(_fmt)
_stream_handler = logging.StreamHandler()  # also to stderr (visible when run directly)
_stream_handler.setFormatter(_fmt)

logging.basicConfig(
    level=logging.DEBUG,
    handlers=[_file_handler, _stream_handler],
)
# Suppress noisy httpcore/httpx DEBUG logs (rant #24)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
# Suppress websockets frame-level DEBUG (rant 2026-08-06T10:21:26):
# 帧级收发日志占 emrgd.log ~90% 纯噪音。设 INFO 保留连接建立/关闭事件，
# 仅滤掉 DEBUG 帧收发（比 WARNING 更细——不丢连接生命周期信息）。
logging.getLogger("websockets").setLevel(logging.INFO)
from emrg.config import load_config
from emrg.server.daemon import run_server

config = load_config()
asyncio.run(run_server(config.llm))
