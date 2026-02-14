"""Configuration loading from TOML file for ncat."""

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
class AgentConfig:
    """ACP agent subprocess configuration."""

    # Path or name of the agent executable (e.g. "claude", "gemini")
    command: str = "claude"
    # Arguments to pass to the agent (e.g. ["--experimental-acp"])
    args: list[str] = field(default_factory=list)
    # Working directory for the agent process
    cwd: str = "~/.ncat/workspace"


@dataclass
class UxConfig:
    """User experience configuration for timeout notifications and interaction."""

    # Seconds before sending first "AI is thinking" notification (0 to disable)
    thinking_notify_seconds: float = 10
    # Seconds before sending "AI thinking too long, use /stop" notification (0 to disable)
    thinking_long_notify_seconds: float = 30
    # Seconds before an unanswered permission request is auto-cancelled (0 to wait forever)
    permission_timeout: float = 300
    # Max characters of raw_input to display in permission request messages (0 for unlimited)
    permission_raw_input_max_len: int = 500


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
class NcatConfig:
    """Top-level ncat configuration, aggregating all sub-configs."""

    server: ServerConfig = field(default_factory=ServerConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    ux: UxConfig = field(default_factory=UxConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def load_config(path: str | Path = "config.toml") -> NcatConfig:
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
    agent_raw = raw.get("agent", {})
    agent = AgentConfig(**agent_raw)
    ux = UxConfig(**raw.get("ux", {}))
    logging_cfg = LoggingConfig(**raw.get("logging", {}))

    return NcatConfig(
        server=server,
        agent=agent,
        ux=ux,
        logging=logging_cfg,
    )


def get_config_path() -> str:
    """Get config file path from command-line args or default."""
    # Simple arg parsing: main.py [config_path]
    if len(sys.argv) > 1:
        return sys.argv[1]
    return "config.toml"
