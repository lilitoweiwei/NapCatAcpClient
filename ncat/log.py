"""Logging initialization and structured event helpers for ncat."""

import json
import logging
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from ncat.config import LoggingConfig

_STANDARD = set(logging.makeLogRecord({}).__dict__.keys()) | {"asctime", "message", "args"}


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_normalize(v) for v in value]
    return str(value)


def _extra(event: str, **fields: Any) -> dict[str, Any]:
    payload = {"event": event}
    for key, value in fields.items():
        if value is None:
            continue
        if key in _STANDARD:
            key = f"field_{key}"
        payload[key] = value
    return payload


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": getattr(record, "service", "ncat"),
            "event": getattr(record, "event", "log"),
            "msg": record.getMessage(),
            "workspace": getattr(record, "workspace", os.getenv("SUZU_WORKSPACE", "unknown")),
            "module": record.name,
            "pid": record.process,
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD or key.startswith("_"):
                continue
            payload[key] = _normalize(value)

        if record.exc_info:
            payload.setdefault("err", str(record.exc_info[1]))
            payload.setdefault("trace", self.formatException(record.exc_info))

        return json.dumps(payload, ensure_ascii=False)


def debug_event(logger: logging.Logger, event: str, msg: str, **fields: Any) -> None:
    logger.debug(msg, extra=_extra(event, **fields))


def info_event(logger: logging.Logger, event: str, msg: str, **fields: Any) -> None:
    logger.info(msg, extra=_extra(event, **fields))


def warning_event(logger: logging.Logger, event: str, msg: str, **fields: Any) -> None:
    logger.warning(msg, extra=_extra(event, **fields))


def error_event(
    logger: logging.Logger,
    event: str,
    msg: str,
    *,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    logger.error(msg, extra=_extra(event, **fields), exc_info=exc_info)


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
    logger.handlers.clear()
    logger.propagate = False

    # Console stays human-friendly.
    console_formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler — uses configured level (default INFO)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, config.level.upper(), logging.INFO))
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # File handler — always DEBUG and written as JSONL for agent-friendly queries
    log_file = log_dir / "ncat.log"
    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=config.keep_days,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(file_handler)
