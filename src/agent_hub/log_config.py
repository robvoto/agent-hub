"""Central logging configuration for Agent Hub.

Two log files, mirroring the job-hunter-agent pattern:
- agent-hub.log: human-readable workflow narrative (console + file)
- agent-hub-debug.log: full technical trace, for debugging (file only)
"""

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

HUMAN_LOGGER_NAME = "agent_hub.human"

DEFAULT_LOG_DIR = Path(os.getenv("HUB_LOG_DIR", Path(__file__).resolve().parents[2] / "logs"))
DEFAULT_HUMAN_LOG_FILE = "agent-hub.log"
DEFAULT_DEBUG_LOG_FILE = "agent-hub-debug.log"
MAX_LOG_BYTES = 5_000_000
BACKUP_COUNT = 5


def parse_log_level(level: str | int | None = None) -> int:
    if level is None:
        return logging.INFO
    if isinstance(level, int):
        return level
    return LEVEL_NAME_MAP.get(str(level).upper(), logging.INFO)


def get_log_file_path(log_file: str | None = None) -> Path:
    """Path to the full technical debug log."""
    path = Path(log_file) if log_file else DEFAULT_LOG_DIR / DEFAULT_DEBUG_LOG_FILE
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_human_log_file_path() -> Path:
    """Path to the human-readable workflow log."""
    path = DEFAULT_LOG_DIR / DEFAULT_HUMAN_LOG_FILE
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_human_logger() -> logging.Logger:
    """Logger for clean workflow narrative: what was asked, what ran, what was returned."""
    return logging.getLogger(HUMAN_LOGGER_NAME)


def configure_logging(level: str | int | None = None, log_file: str | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()

    debug_formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    human_formatter = logging.Formatter(
        fmt="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    debug_file_handler = logging.handlers.RotatingFileHandler(
        get_log_file_path(log_file),
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    debug_file_handler.setFormatter(debug_formatter)
    root.addHandler(debug_file_handler)
    root.setLevel(parse_log_level(level))

    human_logger = logging.getLogger(HUMAN_LOGGER_NAME)
    human_logger.handlers.clear()
    human_logger.setLevel(logging.INFO)
    # Human-logger records still propagate to root, so the debug log captures
    # everything (human + technical); only console/agent-hub.log stay curated.

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(human_formatter)
    human_logger.addHandler(console_handler)

    human_file_handler = logging.handlers.RotatingFileHandler(
        get_human_log_file_path(),
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    human_file_handler.setFormatter(human_formatter)
    human_logger.addHandler(human_file_handler)

    human_logger.info(
        "This is the human-readable Agent Hub log (asked / ran / responded). "
        "Full technical/debug log: %s",
        debug_file_handler.baseFilename,
    )

    # Keep third-party transport/SDK logs quiet; only our app logs should be
    # visible, even in debug mode — the openai SDK dumps full request/response
    # JSON (including tool schemas) at DEBUG, which drowns out the human-readable
    # workflow trace this app emits.
    for transport in ("httpx", "httpcore", "urllib3", "asyncio", "openai"):
        logger = logging.getLogger(transport)
        logger.setLevel(logging.WARNING)
        logger.propagate = False

    if root.level <= logging.DEBUG:
        logging.getLogger("langchain").setLevel(logging.DEBUG)
        logging.getLogger("langgraph").setLevel(logging.DEBUG)
