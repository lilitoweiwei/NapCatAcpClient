"""Logging initialization for ncat."""

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from ncat.config import LoggingConfig


def _cleanup_old_logs(log_dir: Path, max_total_bytes: int) -> None:
    """
    Delete oldest log files when total size exceeds the limit.

    Scans all ncat.log* files, sorts by modification time (oldest first),
    and removes files until total size is within the budget.
    """
    log_files = sorted(log_dir.glob("ncat.log*"), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in log_files)

    while total > max_total_bytes and len(log_files) > 1:
        oldest = log_files.pop(0)
        total -= oldest.stat().st_size
        oldest.unlink()


def setup_logging(config: LoggingConfig) -> None:
    """
    Initialize the logging system with console and file handlers.

    Console handler uses the configured level (e.g. INFO) for real-time viewing.
    File handler always captures DEBUG level for comprehensive diagnostics.
    On startup, cleans up old log files if total size exceeds max_total_mb.
    """
    # Ensure log directory exists
    log_dir = Path(config.dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # Clean up old logs if total exceeds budget
    max_total_bytes = config.max_total_mb * 1024 * 1024
    _cleanup_old_logs(log_dir, max_total_bytes)

    # Configure root ncat logger (set to DEBUG so file handler can capture everything)
    logger = logging.getLogger("ncat")
    logger.setLevel(logging.DEBUG)

    # Log format: [2026-02-13 10:30:00] [INFO] [module] message
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler — uses configured level (default INFO)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, config.level.upper(), logging.INFO))
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler — always DEBUG for full diagnostics
    log_file = log_dir / "ncat.log"
    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=config.keep_days,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
