"""
core/logging.py
Logger factory. All modules call get_logger(__name__) — never configure
logging directly outside this file.

Reads LOG_LEVEL and LOG_FORMAT from core.config.settings so every
logger in the process shares the same format and level without
circular imports (config does not import from here).
"""
from __future__ import annotations

import logging
import sys


# Module-level sentinel so the root handler is only attached once
# even if get_logger() is called before settings is fully loaded.
_root_configured: bool = False


def _configure_root() -> None:
    global _root_configured
    if _root_configured:
        return

    try:
        from core.config import settings
        level_str  = settings.LOG_LEVEL
        fmt_str    = settings.LOG_FORMAT
    except Exception:
        # settings not yet available (e.g. during import of config itself)
        level_str  = "INFO"
        fmt_str    = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

    root = logging.getLogger()

    # Remove any handlers the framework may have added before us
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(fmt_str, datefmt="%Y-%m-%d %H:%M:%S")
        )
        root.addHandler(handler)

    root.setLevel(getattr(logging, level_str, logging.INFO))
    _root_configured = True


def get_logger(name: str) -> logging.Logger:
    """
    Return a named logger, ensuring the root handler is configured first.

    Usage:
        from core.logging import get_logger
        logger = get_logger(__name__)
    """
    _configure_root()
    return logging.getLogger(name)
