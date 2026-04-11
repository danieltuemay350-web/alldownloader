from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from config import Settings


def setup_logging(settings: Settings) -> None:
    log_file = settings.log_dir / "bot.log"
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))
    root.handlers.clear()
    root.addHandler(console_handler)
    root.addHandler(file_handler)
