"""Tests for AgentManager session and workspace lifecycle."""

import asyncio
import signal
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
        self.stdin = DummyStream()
        self.terminate_calls = 0
        self.kill_calls = 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class DummyStream:
    def __init__(self) -> None:
        self.closed = False
        self.wait_closed_calls = 0

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.wait_closed_calls += 1


class HangingProcess(DummyProcess):
    def __init__(self) -> None:
        super().__init__()
        self.wait_calls = 0

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            await asyncio.sleep(60)
        return await super().wait()


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


class DelayedTrailingUpdateAcpConnection(DummyAcpConnection):
    """ACP stub that emits late chunks after prompt() resolves."""

    def __init__(self, manager: AgentManager, chat_id: str) -> None:
        super().__init__()
        self._manager = manager
        self._chat_id = chat_id
        self._tasks: list[asyncio.Task[None]] = []

    async def prompt(self, session_id: str, prompt: list) -> SimpleNamespace:
        self.prompt_calls.append((session_id, prompt))
        self._manager.accumulate_part(
            self._chat_id,
            session_id,
            ContentPart(type="text", text="foo"),
        )

        async def _late_chunks() -> None:
            await asyncio.sleep(0.05)
            self._manager.accumulate_part(
                self._chat_id,
                session_id,
                ContentPart(type="text", text="bar"),
            )
            await asyncio.sleep(0.05)
            self._manager.accumulate_part(
                self._chat_id,
                session_id,
                ContentPart(type="text", text="baz"),
            )

        self._tasks.append(asyncio.create_task(_late_chunks()))
        return SimpleNamespace(stop_reason="end_turn")


def _manager(tmp_path: Path) -> AgentManager:
    return AgentManager(
        command="claude",
        args=[],
        workspace_root=str(tmp_path),
        default_workspace="default",
        log_extra_context_env_var="SUZU_WRAPPER_EXTRA_LOG",
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

    async def fake_start_once(
        self: AgentProcess,
        client,
        timeout: float,
        *,
        chat_id: str,
        workspace_name: str,
        spawn_id: str | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        cwd = Path(self.cwd)
        started_in.append(cwd)
        assert cwd.is_dir()
        return self._log_extra_context_env_var, {
            "chat_id": chat_id,
            "workspace_name": workspace_name,
            "spawn_id": spawn_id or "spawn_test",
        }

    monkeypatch.setattr(AgentProcess, "start_once", fake_start_once)

    await manager.ensure_connection("private:1")

    assert started_in == [tmp_path / "nested/project"]
    conn = manager._connections["private:1"]
    assert conn.spawn_id is not None
    assert conn.extra_log_context["chat_id"] == "private:1"
    assert conn.extra_log_context["workspace_name"] == "nested/project"


def test_build_log_extra_context_requires_json_env_name(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    conn = manager._get_or_create_connection("private:1")

    env_var, payload = conn.agent_process.build_log_extra_context(
        chat_id="private:1",
        workspace_name="default",
        spawn_id="spawn_fixed",
    )

    assert env_var == "SUZU_WRAPPER_EXTRA_LOG"
    assert payload["chat_id"] == "private:1"
    assert payload["workspace_name"] == "default"
    assert payload["spawn_id"] == "spawn_fixed"


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
async def test_send_prompt_waits_for_trailing_updates_after_prompt_return(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    conn = manager._get_or_create_connection("private:1")
    acp_conn = DelayedTrailingUpdateAcpConnection(manager, "private:1")
    conn.agent_process._conn = acp_conn
    conn.agent_process._process = cast(Any, DummyProcess())

    parts = await manager.send_prompt("private:1", [])

    assert "".join(part.text for part in parts if part.type == "text") == "foobarbaz"


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
async def test_agent_stop_kills_process_group_after_grace_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    conn = manager._get_or_create_connection("private:1")
    acp_conn = DummyAcpConnection()
    proc = HangingProcess()
    conn.agent_process._conn = acp_conn
    conn.agent_process._process = cast(Any, proc)
    conn.agent_process._process_group_id = 4321

    killpg_calls: list[tuple[int, signal.Signals]] = []

    def fake_killpg(pgid: int, sig: signal.Signals) -> None:
        killpg_calls.append((pgid, sig))
        if sig == signal.SIGKILL:
            proc.returncode = -9

    monkeypatch.setattr("ncat.agent_process.os.killpg", fake_killpg)

    await conn.agent_process.stop()

    assert proc.stdin.closed is True
    assert killpg_calls == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]
    assert conn.agent_process._process is None
    assert conn.agent_process._process_group_id is None


@pytest.mark.asyncio
async def test_agent_stop_resets_process_state_after_graceful_exit(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    conn = manager._get_or_create_connection("private:1")
    acp_conn = DummyAcpConnection()
    proc = DummyProcess()
    conn.agent_process._conn = acp_conn
    conn.agent_process._process = cast(Any, proc)
    conn.agent_process._process_group_id = 4321

    await conn.agent_process.stop()

    assert proc.stdin.closed is True
    assert proc.terminate_calls == 0
    assert proc.kill_calls == 0
    assert conn.agent_process._process is None
    assert conn.agent_process._process_group_id is None


@pytest.mark.asyncio
async def test_ensure_connection_does_not_create_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    new_session_called = False

    async def fake_start_once(
        self: AgentProcess,
        client,
        timeout: float,
        *,
        chat_id: str,
        workspace_name: str,
        spawn_id: str | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        self._conn = DummyAcpConnection()
        self._process = cast(Any, DummyProcess())
        return self._log_extra_context_env_var, {
            "chat_id": chat_id,
            "workspace_name": workspace_name,
            "spawn_id": spawn_id or "spawn_test",
        }

    async def fake_create_session(chat_id: str) -> str:
        nonlocal new_session_called
        new_session_called = True
        return "sess-1"

    monkeypatch.setattr(AgentProcess, "start_once", fake_start_once)
    monkeypatch.setattr(manager, "_create_session", fake_create_session)

    await manager.ensure_connection("private:1")

    assert new_session_called is False
    assert manager._connections["private:1"].active_session_id is None
