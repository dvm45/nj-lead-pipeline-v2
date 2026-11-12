"""
Logging configuration for the pipeline.

Sets up two handlers:
  1. Console — INFO-level, clean format (no timestamps) for terminal output
  2. Per-run log file — DEBUG-level with timestamps, written to logs/ directory

Call setup_logging() once at CLI startup.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path


def setup_logging(log_dir: Path | None = None) -> Path | None:
    """
    Configure the root logger with console and file handlers.

    Returns the path to the log file, or None if file logging was skipped.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Remove any existing handlers (avoids duplicates on re-init)
    root.handlers.clear()

    # Console handler: clean, readable output
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console)

    # Quiet down noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    logging.getLogger("google").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)

    # File handler: detailed with timestamps
    if log_dir is None:
        log_dir = Path.cwd() / "logs"

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        log_path = log_dir / f"njlead_{timestamp}.log"
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
        )
        root.addHandler(file_handler)
        return log_path
    except OSError:
        # Can't write log file (e.g. read-only directory) — continue without
        return None
