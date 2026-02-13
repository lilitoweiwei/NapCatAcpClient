"""Configuration loading from TOML file."""

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ServerConfig:
    """WebSocket server configuration."""

    # Bind address; use "0.0.0.0" (or None internally) to listen on all interfaces
    host: str = "0.0.0.0"
    # WebSocket port that NapCatQQ connects to
    port: int = 8080


@dataclass
class OpenCodeConfig:
    """OpenCode CLI backend configuration."""

    # Path or name of the opencode executable
    command: str = "opencode"
    # Working directory for opencode subprocess (~ is expanded at runtime)
    work_dir: str = "~/.nochan/workspace"
    # Max number of concurrent opencode processes (limits resource usage)
    max_concurrent: int = 1


@dataclass
class DatabaseConfig:
    """SQLite database configuration."""

    # File path for the SQLite database (parent dirs created automatically)
    path: str = "data/nochan.db"


@dataclass
class LoggingConfig:
    """Logging configuration."""

    # Console log level (file handler always captures DEBUG)
    level: str = "INFO"
    # Directory for log files
    dir: str = "data/logs"
    # Number of days to keep rotated log files
    keep_days: int = 30
    # Total log size cap in MB; oldest files are deleted when exceeded
    max_total_mb: int = 100


@dataclass
class PromptConfig:
    """System prompt file configuration."""

    # Directory for prompt files, relative to opencode work_dir
    dir: str = "prompts"
    # Filename for the session-level system prompt (prepended to first message only)
    session_init_file: str = "session_init.md"
    # Filename for the per-message prefix prompt (prepended to every message)
    message_prefix_file: str = "message_prefix.md"


@dataclass
class UxConfig:
    """User experience configuration for timeout notifications and interaction."""

    # Seconds before sending first "AI is thinking" notification (0 to disable)
    thinking_notify_seconds: float = 10
    # Seconds before sending "AI thinking too long, use /stop" notification (0 to disable)
    thinking_long_notify_seconds: float = 30


@dataclass
class NochanConfig:
    """Top-level nochan configuration, aggregating all sub-configs."""

    server: ServerConfig = field(default_factory=ServerConfig)
    opencode: OpenCodeConfig = field(default_factory=OpenCodeConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    ux: UxConfig = field(default_factory=UxConfig)


def load_config(path: str | Path = "config.toml") -> NochanConfig:
    """
    Load configuration from a TOML file.

    Falls back to defaults for any missing fields.
    Raises FileNotFoundError if the file does not exist.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    # Build config from raw dict, using defaults for missing fields
    server = ServerConfig(**raw.get("server", {}))
    opencode = OpenCodeConfig(**raw.get("opencode", {}))
    database = DatabaseConfig(**raw.get("database", {}))
    logging_cfg = LoggingConfig(**raw.get("logging", {}))
    prompt = PromptConfig(**raw.get("prompt", {}))
    ux = UxConfig(**raw.get("ux", {}))

    return NochanConfig(
        server=server,
        opencode=opencode,
        database=database,
        logging=logging_cfg,
        prompt=prompt,
        ux=ux,
    )


def get_config_path() -> str:
    """Get config file path from command-line args or default."""
    # Simple arg parsing: main.py [config_path]
    if len(sys.argv) > 1:
        return sys.argv[1]
    return "config.toml"
