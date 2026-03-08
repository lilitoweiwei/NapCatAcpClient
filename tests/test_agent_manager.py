"""Tests for workspace handling in AgentManager."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from ncat.agent_manager import AgentManager
from ncat.agent_process import AgentProcess


class DummyAcpConnection:
    """Minimal ACP connection stub for session creation tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict]]] = []

    async def new_session(self, cwd: str, mcp_servers: list[dict]) -> SimpleNamespace:
        self.calls.append((cwd, mcp_servers))
        return SimpleNamespace(session_id="sess-1")


def _manager(tmp_path: Path) -> AgentManager:
    return AgentManager(
        command="claude",
        args=[],
        workspace_root=str(tmp_path),
        default_workspace="default",
    )


def test_set_next_session_cwd_uses_default_workspace(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    manager.set_next_session_cwd("private:1", None)

    assert manager._next_session_cwd["private:1"] == str(tmp_path / "default")


def test_set_next_session_cwd_rejects_workspace_escape(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    with pytest.raises(ValueError):
        manager.set_next_session_cwd("private:1", "../escape")

    with pytest.raises(ValueError):
        manager.set_next_session_cwd("private:1", "/tmp/escape")


@pytest.mark.asyncio
async def test_create_session_uses_absolute_workspace_path(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.set_next_session_cwd("private:1", "project-a")
    conn = manager._get_or_create_connection("private:1")
    acp_conn = DummyAcpConnection()
    conn.agent_process._conn = acp_conn

    session_id = await manager.get_or_create_session("private:1")

    workspace = tmp_path / "project-a"
    assert session_id == "sess-1"
    assert workspace.is_dir()
    assert conn.workspace_cwd == str(workspace)
    assert acp_conn.calls == [(str(workspace), [])]


@pytest.mark.asyncio
async def test_ensure_connection_creates_workspace_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    manager.set_next_session_cwd("private:1", "nested/project")
    started_in: list[Path] = []

    async def fake_start_once(self: AgentProcess, client, timeout: float) -> None:
        cwd = Path(self.cwd)
        started_in.append(cwd)
        assert cwd.is_dir()

    monkeypatch.setattr(AgentProcess, "start_once", fake_start_once)

    await manager.ensure_connection("private:1")

    assert started_in == [tmp_path / "nested/project"]
