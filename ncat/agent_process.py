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
import shutil
import sys
from typing import Any

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

logger = logging.getLogger("ncat.agent_process")

# Union of all ACP content block types accepted by conn.prompt().
PromptBlock = (
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
    logger.debug(
        "ACP %s [%s] id=%s method=%s: %s",
        arrow, direction, msg_id, method, raw,
    )


class AgentProcess:
    """Manages the ACP agent subprocess and connection lifecycle.

    Responsibilities:
    - Resolve and start the agent executable (with Windows .cmd support)
    - Establish ACP connection over stdin/stdout
    - Initialize ACP protocol handshake
    - Track agent capabilities (e.g. image support)
    - Stop the subprocess and close the connection
    """

    def __init__(self, command: str, args: list[str], cwd: str) -> None:
        # Agent executable and arguments
        self._command = command
        self._args = args
        # Working directory for the agent process
        self._cwd = cwd

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

    async def start(self, client: Client) -> None:
        """Start the agent subprocess and initialize the ACP connection.

        Args:
            client: ACP Client implementation that handles protocol callbacks
                    (e.g. session_update, request_permission).
        """
        logger.info(
            "Starting agent: %s %s (cwd: %s)",
            self._command,
            " ".join(self._args),
            self._cwd,
        )

        # Resolve the executable path. On Windows, create_subprocess_exec cannot
        # run .cmd/.bat scripts directly (WinError 193), so we resolve the full
        # path via shutil.which() and let the shell handle it.
        resolved = shutil.which(self._command)
        if resolved is None:
            raise FileNotFoundError(
                f"Agent command not found: {self._command}"
            )

        # On Windows, .cmd/.bat wrappers (e.g. npm-installed tools) must be
        # executed through the shell. On Linux this is not needed.
        use_shell = (
            sys.platform == "win32"
            and resolved.lower().endswith((".cmd", ".bat"))
        )
        if use_shell:
            shell_args = [resolved, *self._args]
            logger.debug(
                "Using shell execution for .cmd wrapper: %s", resolved
            )
            self._process = await asyncio.create_subprocess_exec(
                "cmd",
                "/c",
                *shell_args,
                stdin=aio_subprocess.PIPE,
                stdout=aio_subprocess.PIPE,
                cwd=self._cwd,
            )
        else:
            # Direct exec (Linux, or native .exe on Windows)
            self._process = await asyncio.create_subprocess_exec(
                resolved,
                *self._args,
                stdin=aio_subprocess.PIPE,
                stdout=aio_subprocess.PIPE,
                cwd=self._cwd,
            )

        if self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError(
                "Agent process does not expose stdio pipes"
            )

        # Establish ACP connection over stdin/stdout.
        # Register a stream observer to log all JSON-RPC messages for debugging.
        self._conn = connect_to_agent(
            client,
            self._process.stdin,
            self._process.stdout,
            observers=[_acp_stream_observer],
        )

        # Initialize the ACP protocol.
        #
        # We bypass the SDK's conn.initialize() because its serialize_params()
        # uses exclude_defaults=True, which silently drops fields whose values
        # equal pydantic defaults (e.g. clientCapabilities, fs, terminal).
        # Instead we construct the params dict directly so every field is
        # explicitly present on the wire, matching the ACP spec examples.
        init_params = {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {
                "fs": {
                    "readTextFile": False,
                    "writeTextFile": False,
                },
                "terminal": False,
            },
            "clientInfo": {
                "name": "ncat",
                "title": "NapCat ACP Client",
                "version": "0.2.0",
            },
        }
        raw_response = await self._conn._conn.send_request(
            "initialize", init_params
        )
        init_result = InitializeResponse.model_validate(raw_response)
        prompt_caps = getattr(
            init_result.agent_capabilities, "prompt_capabilities", None
        )
        self._supports_image = bool(getattr(prompt_caps, "image", False))

        logger.info(
            "ACP initialized: agent=%s protocol_version=%s",
            init_result.agent_info,
            init_result.protocol_version,
        )
        logger.info(
            "ACP prompt capabilities: image=%s", self._supports_image
        )

    async def stop(self) -> None:
        """Stop the agent subprocess and close the ACP connection."""
        # Close the ACP connection
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None

        # Terminate the agent subprocess
        if self._process is not None and self._process.returncode is None:
            logger.info(
                "Terminating agent subprocess (pid=%s)", self._process.pid
            )
            self._process.terminate()
            with contextlib.suppress(ProcessLookupError):
                await self._process.wait()
            self._process = None

        logger.info("Agent stopped")
