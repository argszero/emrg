"""Entry point for EMRG daemon: python -m emrg.server"""
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Write daemon logs to ~/.emrg/emrgd.log so they survive stderr=DEVNULL
_log_dir = Path.home() / ".emrg"
_log_dir.mkdir(parents=True, exist_ok=True)
_log_file = _log_dir / "emrgd.log"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        RotatingFileHandler(
            str(_log_file), maxBytes=10 * 1024 * 1024, backupCount=3
        ),
        logging.StreamHandler(),  # also to stderr (visible when run directly)
    ],
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
