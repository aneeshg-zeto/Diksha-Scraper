"""Centralised logging setup using Rich for pretty console output."""

import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

console = Console()

_LOG_FILE = Path("scraper.log")


def get_logger(name: str = "diksha") -> logging.Logger:
    """Return a configured logger.  Call once per module with __name__."""
    logger = logging.getLogger(name)

    if logger.handlers:
        # Already configured — return as-is to avoid duplicate handlers.
        return logger

    logger.setLevel(logging.DEBUG)

    # ── Rich console handler (INFO and above) ──────────────────────────────
    rich_handler = RichHandler(
        console=console,
        rich_tracebacks=True,
        show_path=False,
        markup=True,
    )
    rich_handler.setLevel(logging.INFO)
    logger.addHandler(rich_handler)

    # ── File handler (DEBUG and above) ─────────────────────────────────────
    file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(file_handler)

    return logger
