"""Tests for configuration loading."""

from pathlib import Path

import pytest

from ncat.config import NcatConfig, load_config


def test_load_valid_config(tmp_config: Path) -> None:
    """Test loading a valid TOML config file."""
    config = load_config(tmp_config)
    assert isinstance(config, NcatConfig)
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 0
    assert config.agent.command == "echo"
    assert config.agent.args == []
    assert config.logging.level == "DEBUG"
    assert config.logging.keep_days == 7


def test_load_missing_file() -> None:
    """Test that missing config file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_config("nonexistent.toml")


def test_load_partial_config(tmp_path: Path) -> None:
    """Test that missing sections fall back to defaults."""
    config_file = tmp_path / "partial.toml"
    config_file.write_text("[server]\nport = 9999\n")

    config = load_config(config_file)
    # Specified value should be loaded
    assert config.server.port == 9999
    # Missing sections should use defaults
    assert config.agent.command == "claude"
    assert config.logging.level == "INFO"


def test_load_empty_config(tmp_path: Path) -> None:
    """Test that an empty config file uses all defaults."""
    config_file = tmp_path / "empty.toml"
    config_file.write_text("")

    config = load_config(config_file)
    assert config.server.host == "0.0.0.0"
    assert config.server.port == 8080
    assert config.agent.command == "claude"
    assert config.ux.thinking_notify_seconds == 10


def test_load_agent_config_with_args(tmp_path: Path) -> None:
    """Test loading agent config with args list."""
    config_file = tmp_path / "agent.toml"
    config_file.write_text(
        '[agent]\ncommand = "gemini"\nargs = ["--experimental-acp"]\ncwd = "/tmp/workspace"\n'
    )

    config = load_config(config_file)
    assert config.agent.command == "gemini"
    assert config.agent.args == ["--experimental-acp"]
    assert config.agent.cwd == "/tmp/workspace"
