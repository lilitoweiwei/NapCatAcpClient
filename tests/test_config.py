"""Tests for ncat configuration loading."""

from pathlib import Path

import pytest

from ncat.config import load_config


def _write_config(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_load_config_uses_default_acp_stdio_read_limit_mb(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path, "")

    config = load_config(config_path)

    assert config.agent.acp_stdio_read_limit_mb == 128


def test_load_config_reads_custom_acp_stdio_read_limit_mb(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        "[agent]\nacp_stdio_read_limit_mb = 256\n",
    )

    config = load_config(config_path)

    assert config.agent.acp_stdio_read_limit_mb == 256


def test_load_config_rejects_non_positive_acp_stdio_read_limit_mb(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        "[agent]\nacp_stdio_read_limit_mb = 0\n",
    )

    with pytest.raises(ValueError, match="acp_stdio_read_limit_mb"):
        load_config(config_path)


def test_load_config_rejects_legacy_workspace_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        "[agent]\nworkspace_root = \"/workspace\"\ndefault_workspace = \"default\"\n",
    )

    with pytest.raises(ValueError, match="workspace_root"):
        load_config(config_path)
