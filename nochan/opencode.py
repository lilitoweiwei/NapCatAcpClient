"""OpenCode CLI backend wrapper with subprocess execution and concurrency control."""

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger("nochan.opencode")


@dataclass
class OpenCodeResponse:
    """Result from an OpenCode CLI invocation."""

    # OpenCode session ID (format: ses_XXX); empty string if not obtained
    session_id: str
    # Concatenated AI response text from all "text" events
    content: str
    # True if the process exited cleanly with no error events
    success: bool
    # Human-readable error description; None on success
    error: str | None


def parse_jsonl_events(lines: list[str]) -> OpenCodeResponse:
    """
    Parse JSONL event lines from `opencode run --format json` output.

    Extracts session ID, text content, and error information.
    """
    session_id = ""
    text_parts: list[str] = []
    errors: list[str] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Non-JSON line from opencode: %s", line[:200])
            continue

        event_type = event.get("type", "")

        # Log every JSONL event at DEBUG for full traceability
        logger.debug("OpenCode JSONL event: type=%s data=%s", event_type, str(event)[:300])

        # Extract session ID from any event (first one wins)
        if not session_id and "sessionID" in event:
            session_id = event["sessionID"]

        if event_type == "text":
            # Collect AI response text (content is already visible in the JSONL event log above)
            part_text = event.get("part", {}).get("text", "")
            if part_text:
                text_parts.append(part_text)

        elif event_type == "tool_use":
            # Log tool usage — these are important for understanding AI behavior
            part = event.get("part", {})
            tool_name = part.get("tool", "?")
            state = part.get("state", {})
            title = state.get("title", "")
            status = state.get("status", "?")
            output = state.get("output", "")
            logger.info(
                "OpenCode tool: %s [%s] %s",
                tool_name,
                status,
                title,
            )
            if output:
                logger.debug("OpenCode tool output: %s", output[:500])

        elif event_type == "step_start":
            logger.debug("OpenCode step started (session=%s)", session_id)

        elif event_type == "step_finish":
            # Log completion info
            part = event.get("part", {})
            reason = part.get("reason", "")
            cost = part.get("cost")
            tokens = part.get("tokens", {})
            if reason == "stop":
                logger.info(
                    "OpenCode finished: cost=%s, tokens_in=%s, tokens_out=%s",
                    cost,
                    tokens.get("input"),
                    tokens.get("output"),
                )
            else:
                logger.debug("OpenCode step_finish: reason=%s", reason)

        elif event_type == "error":
            # Capture error messages
            error_data = event.get("error", {})
            error_msg = error_data.get("data", {}).get(
                "message", str(error_data.get("name", "Unknown error"))
            )
            errors.append(error_msg)
            logger.error("OpenCode error: %s", error_msg)

        else:
            logger.debug("OpenCode unknown event type: %s", event_type)

    # Build response
    content = "".join(text_parts)

    if errors:
        logger.info(
            "OpenCode parse result: FAILED session=%s content_len=%d errors=%s",
            session_id,
            len(content),
            "; ".join(errors),
        )
        return OpenCodeResponse(
            session_id=session_id,
            content=content,
            success=False,
            error="; ".join(errors),
        )

    logger.info(
        "OpenCode parse result: OK session=%s content_len=%d",
        session_id,
        len(content),
    )

    return OpenCodeResponse(
        session_id=session_id,
        content=content,
        success=True,
        error=None,
    )


class SubprocessOpenCodeBackend:
    """OpenCode backend that runs `opencode run` as a subprocess."""

    def __init__(self, command: str, work_dir: str, max_concurrent: int) -> None:
        # Path or name of the opencode executable
        self._command = command
        # Working directory passed as cwd to the subprocess
        self._work_dir = work_dir
        # Semaphore enforcing the concurrent process limit
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        # Number of tasks currently waiting for or holding a semaphore slot
        self._active_count = 0

    def is_queue_full(self) -> bool:
        """Check if all slots are occupied (new requests will have to wait)."""
        return self._active_count >= self._max_concurrent

    async def send_message(self, session_id: str | None, message: str) -> OpenCodeResponse:
        """
        Send a message to OpenCode via CLI subprocess.

        Args:
            session_id: OpenCode session ID to continue, or None for new session
            message: The user message / prompt to send
        """
        self._active_count += 1
        try:
            async with self._semaphore:
                return await self._run(session_id, message)
        finally:
            self._active_count -= 1

    async def _run(self, session_id: str | None, message: str) -> OpenCodeResponse:
        """Execute opencode run and parse the JSONL output."""
        # Build command
        cmd = [self._command, "run", "--format", "json"]
        if session_id:
            cmd.extend(["-s", session_id])
        cmd.append(message)

        logger.info(
            "Running opencode (session=%s): %s",
            session_id or "new",
            message[:100],
        )

        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._work_dir,
            )

            # Read all stdout lines
            assert process.stdout is not None
            stdout_data = await process.stdout.read()
            stderr_data = await process.stderr.read() if process.stderr else b""

            await process.wait()

            # Log stderr if any
            stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
            if stderr_text:
                logger.warning("OpenCode stderr: %s", stderr_text[:500])

            # Check exit code
            if process.returncode != 0:
                logger.error("OpenCode exited with code %d", process.returncode)
                return OpenCodeResponse(
                    session_id=session_id or "",
                    content="",
                    success=False,
                    error=f"OpenCode process exited with code {process.returncode}",
                )

            # Parse JSONL output
            stdout_text = stdout_data.decode("utf-8", errors="replace")
            lines = stdout_text.splitlines()
            logger.debug("OpenCode stdout: %d lines, %d bytes", len(lines), len(stdout_text))
            return parse_jsonl_events(lines)

        except asyncio.CancelledError:
            # Kill the subprocess when the calling task is cancelled (/stop)
            if process is not None and process.returncode is None:
                logger.info(
                    "Killing opencode subprocess (pid=%s) due to cancellation",
                    process.pid,
                )
                process.kill()
                with contextlib.suppress(ProcessLookupError):
                    await process.wait()
            raise

        except FileNotFoundError:
            logger.error("OpenCode command not found: %s", self._command)
            return OpenCodeResponse(
                session_id=session_id or "",
                content="",
                success=False,
                error=f"OpenCode command not found: {self._command}",
            )
        except Exception as e:
            logger.error("OpenCode execution failed: %s", e)
            return OpenCodeResponse(
                session_id=session_id or "",
                content="",
                success=False,
                error=str(e),
            )
