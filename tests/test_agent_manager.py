"""Tests for AgentManager session and workspace lifecycle."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ncat.agent_manager import AgentManager
from ncat.agent_process import AgentProcess
from ncat.models import ContentPart


class DummyProcess:
    """Minimal subprocess stub compatible with AgentProcess.stop()."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.pid = 12345

    def terminate(self) -> None:
        self.returncode = 0

    async def wait(self) -> int:
        self.returncode = 0
        return 0


class DummyAcpConnection:
    """Minimal ACP connection stub for session creation tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict]]] = []
        self.prompt_calls: list[tuple[str, list]] = []
        self.cancel_calls: list[str] = []
        self.session_counter = 0

    async def new_session(self, cwd: str, mcp_servers: list[dict]) -> SimpleNamespace:
        self.calls.append((cwd, mcp_servers))
        self.session_counter += 1
        return SimpleNamespace(session_id=f"sess-{self.session_counter}")

    async def prompt(self, session_id: str, prompt: list) -> SimpleNamespace:
        self.prompt_calls.append((session_id, prompt))
        return SimpleNamespace(stop_reason="end_turn")

    async def cancel(self, session_id: str) -> None:
        self.cancel_calls.append(session_id)


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
    assert conn.active_session_id == "sess-1"


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


@pytest.mark.asyncio
async def test_session_reused_across_successive_prompts_same_chat(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    conn = manager._get_or_create_connection("private:1")
    acp_conn = DummyAcpConnection()
    conn.agent_process._conn = acp_conn
    conn.agent_process._process = cast(Any, DummyProcess())

    first = await manager.send_prompt("private:1", [])
    second = await manager.send_prompt("private:1", [])

    assert first == []
    assert second == []
    assert acp_conn.calls == [(str(tmp_path / "default"), [])]
    assert acp_conn.prompt_calls == [("sess-1", []), ("sess-1", [])]
    assert conn.active_session_id == "sess-1"


@pytest.mark.asyncio
async def test_successful_prompt_keeps_active_session(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    conn = manager._get_or_create_connection("private:1")
    acp_conn = DummyAcpConnection()
    conn.agent_process._conn = acp_conn
    conn.agent_process._process = cast(Any, DummyProcess())

    await manager.send_prompt("private:1", [])

    assert conn.active_session_id == "sess-1"
    assert conn.active_turn_session_id is None
    assert conn.turn_accumulator == []
    assert conn.active_prompt is False


@pytest.mark.asyncio
async def test_close_session_forces_next_prompt_to_create_new_session(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    conn = manager._get_or_create_connection("private:1")
    acp_conn = DummyAcpConnection()
    conn.agent_process._conn = acp_conn
    conn.agent_process._process = cast(Any, DummyProcess())

    await manager.send_prompt("private:1", [])
    await manager.close_session("private:1")
    await manager.send_prompt("private:1", [])

    assert [session_id for session_id, _ in acp_conn.prompt_calls] == ["sess-1", "sess-2"]


@pytest.mark.asyncio
async def test_cancel_uses_active_turn_session_id(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    conn = manager._get_or_create_connection("private:1")
    acp_conn = DummyAcpConnection()
    conn.agent_process._conn = acp_conn
    conn.agent_process._process = cast(Any, DummyProcess())

    ready = asyncio.Event()
    release = asyncio.Event()

    async def delayed_prompt(session_id: str, prompt: list) -> SimpleNamespace:
        acp_conn.prompt_calls.append((session_id, prompt))
        ready.set()
        await release.wait()
        return SimpleNamespace(stop_reason="cancelled")

    acp_conn.prompt = delayed_prompt

    task = asyncio.create_task(manager.send_prompt("private:1", []))
    await ready.wait()

    cancelled = await manager.cancel("private:1")
    release.set()
    await task

    assert cancelled is True
    assert acp_conn.cancel_calls == ["sess-1"]


@pytest.mark.asyncio
async def test_disconnect_clears_session_and_turn_state(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    conn = manager._get_or_create_connection("private:1")
    acp_conn = DummyAcpConnection()
    conn.agent_process._conn = acp_conn
    conn.agent_process._process = cast(Any, DummyProcess())
    conn.active_session_id = "sess-1"
    conn.active_turn_session_id = "sess-1"
    conn.turn_accumulator.append(ContentPart(type="text", text="partial"))
    conn.active_prompt = True

    await manager.disconnect("private:1")

    assert "private:1" not in manager._connections


@pytest.mark.asyncio
async def test_ensure_connection_does_not_create_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    new_session_called = False

    async def fake_start_once(self: AgentProcess, client, timeout: float) -> None:
        self._conn = DummyAcpConnection()
        self._process = cast(Any, DummyProcess())

    async def fake_create_session(chat_id: str) -> str:
        nonlocal new_session_called
        new_session_called = True
        return "sess-1"

    monkeypatch.setattr(AgentProcess, "start_once", fake_start_once)
    monkeypatch.setattr(manager, "_create_session", fake_create_session)

    await manager.ensure_connection("private:1")

    assert new_session_called is False
    assert manager._connections["private:1"].active_session_id is None
