"""ACP agent subprocess — owns process lifecycle and ACP connection.

This module contains `AgentProcess`, which starts/stops the external ACP agent
subprocess, establishes the ACP connection over stdio, and initializes the
ACP protocol handshake.

Session management and prompt orchestration live in
`ncat.agent_manager.AgentManager`.
"""

import asyncio
import asyncio.subprocess as aio_subprocess
import contextlib
import json
import logging
import os
import secrets
import shutil
import sys
from typing import Any, cast

from acp import PROTOCOL_VERSION, Client, connect_to_agent
from acp.core import ClientSideConnection
from acp.schema import (
    AudioContentBlock,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    InitializeResponse,
    ResourceContentBlock,
    TextContentBlock,
)

from ncat.log import debug_event, info_event

logger = logging.getLogger("ncat.agent_process")

# Union of all ACP content block types accepted by conn.prompt().
type PromptBlock = (
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceContentBlock
    | EmbeddedResourceContentBlock
)


# --- ACP stream observer for debug logging ---

# Maximum length of the JSON dump to log per message (avoids flooding logs
# with large prompt/response payloads).
_LOG_MAX_LEN = 2000


def _acp_stream_observer(event: Any) -> None:
    """Log every JSON-RPC message exchanged over the ACP connection.

    Registered as a StreamObserver on the underlying Connection. Logs at
    DEBUG level so it only appears when debug logging is enabled.
    """
    direction = event.direction.value  # "incoming" or "outgoing"
    msg = event.message
    method = msg.get("method", "")
    msg_id = msg.get("id", "")

    # Compact JSON for the log line, truncated to avoid giant payloads
    raw = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
    if len(raw) > _LOG_MAX_LEN:
        raw = raw[:_LOG_MAX_LEN] + f"... ({len(raw)} chars total)"

    # Tag with ← (incoming) or → (outgoing) for quick scanning
    arrow = "←" if direction == "incoming" else "→"
    debug_event(
        logger,
        "acp_stream",
        "ACP stream message observed",
        direction=direction,
        arrow=arrow,
        request_id=msg_id,
        method=method,
        raw=raw,
    )


class AgentProcess:
    """Manages the ACP agent subprocess and connection lifecycle.

    Responsibilities:
    - Resolve and start the agent executable (with Windows .cmd/.bat support)
    - Establish ACP connection over stdin/stdout
    - Initialize ACP protocol handshake
    - Track agent capabilities (e.g. image support)
    - Stop the subprocess and close the connection
    """

    def __init__(
        self,
        command: str,
        args: list[str],
        cwd: str,
        env: dict[str, str] | None = None,
        log_extra_context_env_var: str | None = None,
    ) -> None:
        # Agent executable and arguments
        self._command = command
        self._args = args
        # Working directory for the agent process
        self._cwd = cwd
        # Extra environment variables (merged with system env at start time)
        self._extra_env = env or {}
        self._log_extra_context_env_var = log_extra_context_env_var

        # Agent subprocess handle
        self._process: aio_subprocess.Process | None = None
        # ACP connection to the agent
        self._conn: ClientSideConnection | None = None

        # Agent prompt capabilities (populated on initialize)
        self._supports_image: bool = False

    @property
    def conn(self) -> ClientSideConnection | None:
        """The ACP connection (None if not started)."""
        return self._conn

    @property
    def cwd(self) -> str:
        """The working directory for the agent process."""
        return self._cwd

    def set_cwd(self, cwd: str) -> None:
        """Update the working directory used for the next subprocess start."""
        self._cwd = cwd

    @property
    def is_running(self) -> bool:
        """Check if the agent process is alive."""
        return (
            self._process is not None
            and self._process.returncode is None
            and self._conn is not None
        )

    @property
    def supports_image(self) -> bool:
        """Whether the connected agent supports image blocks in prompts."""
        return self._supports_image

    @property
    def log_extra_context_env_var(self) -> str | None:
        """Configured env var name used to pass wrapper log context."""
        return self._log_extra_context_env_var

    def build_log_extra_context(
        self,
        *,
        chat_id: str,
        workspace_name: str,
        spawn_id: str | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        """Build extra structured log context for wrapper-side logging."""
        if not self._log_extra_context_env_var:
            return None, {}

        payload = {
            "workspace": os.environ.get("SUZU_WORKSPACE", workspace_name),
            "workspace_name": workspace_name,
            "chat_id": chat_id,
            "spawn_id": spawn_id or f"spawn_{secrets.token_hex(8)}",
            "agent_cwd": self._cwd,
            "agent_command": self._command,
        }
        return self._log_extra_context_env_var, payload

    async def start_once(
        self,
        client: Client,
        timeout: float,
        *,
        chat_id: str,
        workspace_name: str,
        spawn_id: str | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        """One-shot: start agent subprocess and complete ACP initialize with timeout.

        Cleans up any existing process/connection first. On timeout or any
        exception, cleans up and re-raises for the caller to retry later.
        """
        await self.stop()

        info_event(
            logger,
            "agent_spawn_start",
            "Starting agent subprocess",
            command=self._command,
            cmd_args=self._args,
            cwd=self._cwd,
            chat_id=chat_id,
        )

        proc_env: dict[str, str] | None = None
        if self._extra_env:
            proc_env = {**os.environ, **self._extra_env}
            info_event(
                logger,
                "agent_env_override",
                "Agent extra environment variables configured",
                env_keys=sorted(self._extra_env.keys()),
            )

        extra_context_env_var, extra_context = self.build_log_extra_context(
            chat_id=chat_id,
            workspace_name=workspace_name,
            spawn_id=spawn_id,
        )
        if extra_context_env_var and extra_context:
            if proc_env is None:
                proc_env = dict(os.environ)
            proc_env[extra_context_env_var] = json.dumps(extra_context, ensure_ascii=False)
            info_event(
                logger,
                "agent_log_context_env",
                "Configured agent wrapper log context env",
                chat_id=chat_id,
                env_var=extra_context_env_var,
                context_keys=sorted(extra_context.keys()),
                spawn_id=extra_context.get("spawn_id"),
            )

        resolved = shutil.which(self._command)
        if resolved is None:
            raise FileNotFoundError(f"Agent command not found: {self._command}")

        use_shell = sys.platform == "win32" and resolved.lower().endswith((".cmd", ".bat"))
        if use_shell:
            shell_args = [resolved, *self._args]
            debug_event(
                logger,
                "agent_spawn_shell",
                "Using shell execution for .cmd wrapper",
                command=resolved,
            )
            self._process = await asyncio.create_subprocess_exec(
                "cmd",
                "/c",
                *shell_args,
                stdin=aio_subprocess.PIPE,
                stdout=aio_subprocess.PIPE,
                cwd=self._cwd,
                env=proc_env,
            )
        else:
            self._process = await asyncio.create_subprocess_exec(
                resolved,
                *self._args,
                stdin=aio_subprocess.PIPE,
                stdout=aio_subprocess.PIPE,
                cwd=self._cwd,
                env=proc_env,
            )

        if self._process.stdin is None or self._process.stdout is None:
            await self.stop()
            raise RuntimeError("Agent process does not expose stdio pipes")

        self._conn = connect_to_agent(
            client,
            self._process.stdin,
            self._process.stdout,
            observers=[_acp_stream_observer],
        )
        conn = cast(Any, self._conn)

        init_params = {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {
                "name": "ncat",
                "title": "NapCat ACP Client",
                "version": "0.2.0",
            },
        }
        debug_event(logger, "acp_initialize_payload", "ACP initialize payload prepared", params=init_params)
        info_event(logger, "acp_initialize_start", "Initializing ACP connection")
        try:
            raw_response = await asyncio.wait_for(
                conn._conn.send_request("initialize", init_params),
                timeout=timeout,
            )
        except (TimeoutError, Exception):
            await self.stop()
            raise
        init_result = InitializeResponse.model_validate(raw_response)
        prompt_caps = getattr(init_result.agent_capabilities, "prompt_capabilities", None)
        self._supports_image = bool(getattr(prompt_caps, "image", False))
        info_event(
            logger,
            "acp_initialize_ok",
            "ACP initialized",
            agent_info=str(init_result.agent_info),
            protocol_version=init_result.protocol_version,
        )
        info_event(
            logger,
            "acp_capabilities",
            "ACP prompt capabilities detected",
            supports_image=self._supports_image,
        )
        return extra_context_env_var, extra_context

    async def wait(self) -> None:
        """Wait for the agent subprocess to exit (e.g. after connection is lost)."""
        if self._process is not None and self._process.returncode is None:
            await self._process.wait()

    async def stop(self) -> None:
        """Stop the agent subprocess and close the ACP connection."""
        # Close the ACP connection
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None

        # Terminate the agent subprocess
        if self._process is not None and self._process.returncode is None:
            info_event(
                logger,
                "agent_spawn_stop",
                "Terminating agent subprocess",
                pid=self._process.pid,
            )
            self._process.terminate()
            with contextlib.suppress(ProcessLookupError):
                await self._process.wait()
            self._process = None

        info_event(logger, "agent_spawn_stopped", "Agent stopped")
