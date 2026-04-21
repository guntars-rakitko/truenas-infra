"""structlog configuration.

Single place to wire up logging. JSON to `logs/truenas-infra-<ts>.log`,
human-coloured to stderr for interactive use.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

import structlog


def configure(level: str = "INFO", *, log_dir: Path | None = None) -> structlog.BoundLogger:
    """Configure structlog + stdlib logging. Returns a ready-to-use logger."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    handlers: list[logging.Handler] = []

    # Console handler — human readable, coloured.
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(numeric_level)
    handlers.append(console)

    # File handler — JSON lines, always DEBUG regardless of console level.
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        file_path = log_dir / f"truenas-infra-{ts}.log"
        file_handler = logging.FileHandler(file_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        handlers.append(file_handler)

    logging.basicConfig(
        format="%(message)s",
        level=numeric_level,
        handlers=handlers,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    return structlog.get_logger("truenas_infra")
