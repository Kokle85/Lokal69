"""Logging setup (loguru): console + rotating file."""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(level: str = "INFO") -> None:
    logger.remove()
    logger.add(sys.stderr, level=level)
    Path("logs").mkdir(exist_ok=True)
    logger.add("logs/accumulation_bot.log", level="DEBUG",
               rotation="10 MB", retention="14 days")
