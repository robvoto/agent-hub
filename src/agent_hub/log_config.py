"""Central logging configuration for Agent Hub."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Literal

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

LEVEL_NAME_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

DEFAULT_LOG_FILE = "agent-hub.log"
DEFAULT_LOG_DIR = Path(os.getenv("HUB_LOG_DIR", Path(__file__).resolve().parents[2] / "logs"))
MAX_LOG_BYTES = 5_000_000
BACKUP_COUNT = 5


def parse_log_level(level: str | int | None = None) -> int:
    if level is None:
        return logging.INFO
    if isinstance(level, int):
        return level
    return LEVEL_NAME_MAP.get(str(level).upper(), logging.INFO)


def get_log_file_path(log_file: str | None = None) -> Path:
    path = Path(log_file) if log_file else DEFAULT_LOG_DIR / DEFAULT_LOG_FILE
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def configure_logging(level: str | int | None = None, log_file: str | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        get_log_file_path(log_file),
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    root.setLevel(parse_log_level(level))

    # Keep third-party transport logs quiet; only our app logs should be visible.
    for transport in ("httpx", "httpcore", "urllib3", "asyncio"):
        logger = logging.getLogger(transport)
        logger.setLevel(logging.WARNING)
        logger.propagate = False

    if root.level <= logging.DEBUG:
        logging.getLogger("langchain").setLevel(logging.DEBUG)
        logging.getLogger("langgraph").setLevel(logging.DEBUG)
