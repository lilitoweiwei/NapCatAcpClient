"""Permission broker — forwards ACP permission requests to QQ users.

Bridges the ACP agent's synchronous permission requests with the
asynchronous QQ user interaction. Manages "always" decision caching
per session and pending permission request futures.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from acp.schema import (
    AllowedOutcome,
    DeniedOutcome,
    PermissionOption,
    RequestPermissionResponse,
    ToolCallUpdate,
)

logger = logging.getLogger("ncat.permission")

# Type alias for the reply callback provided by the transport layer.
# Signature: async reply_fn(event: dict, text: str) -> None
ReplyFn = Callable[[dict, str], Awaitable[None]]


@dataclass
class PendingPermission:
    """Tracks a permission request that is waiting for the QQ user's reply."""

    # Future resolved when the user responds (value is the selected PermissionOption)
    future: asyncio.Future[PermissionOption]
    # The available options for this request
    options: list[PermissionOption]
    # The original event dict (needed for sending timeout/error messages)
    event: dict


class PermissionBroker:
    """Forwards ACP permission requests to QQ users and manages "always" caching.

    Lifecycle:
    - handle()       : called by NcatAcpClient when the agent requests permission
    - try_resolve()  : called by MessageDispatcher when the user replies
    - has_pending()  : called by MessageDispatcher to check interception
    - cancel_pending(): called by PromptRunner on /stop or prompt cancellation
    - clear_session() : called when an ACP session is closed
    """

    def __init__(
        self,
        reply_fn: ReplyFn,
        timeout: float = 300,
        raw_input_max_len: int = 500,
    ) -> None:
        # Callback to send a text reply back to the QQ message source
        self._reply_fn = reply_fn
        # Permission timeout in seconds (0 = wait forever)
        self._timeout = timeout
        # Max chars of raw_input to display (0 = unlimited)
        self._raw_input_max_len = raw_input_max_len

        # "Always" decision cache: session_id -> {tool_kind_key -> option}
        # tool_kind_key is ToolCallUpdate.kind or None (None is a valid key)
        self._always: dict[str, dict[str | None, PermissionOption]] = {}
        # Currently pending permission request per chat: chat_id -> PendingPermission
        self._pending: dict[str, PendingPermission] = {}

    # --- Public API ---

    async def handle(
        self,
        session_id: str,
        chat_id: str,
        event: dict,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
    ) -> RequestPermissionResponse:
        """Handle a permission request from the ACP agent.

        Checks the "always" cache first. On cache miss, sends a formatted
        message to the QQ user and waits for their reply (with optional timeout).

        Returns the RequestPermissionResponse to send back to the agent.
        """
        tool_kind = tool_call.kind

        # Check "always" cache — return immediately if a previous decision applies
        cached = self._always.get(session_id, {}).get(tool_kind)
        if cached is not None:
            logger.info(
                "Permission auto-resolved (always cache) for session %s kind=%s: %s",
                session_id,
                tool_kind,
                cached.name,
            )
            return RequestPermissionResponse(
                outcome=AllowedOutcome(outcome="selected", option_id=cached.option_id)
            )

        # Format and send the permission request message to the QQ user
        msg = self._format_permission_message(tool_call, options)
        await self._reply_fn(event, msg)

        # Create a Future for the user's reply and register it as pending
        loop = asyncio.get_running_loop()
        future: asyncio.Future[PermissionOption] = loop.create_future()
        self._pending[chat_id] = PendingPermission(future=future, options=options, event=event)

        try:
            # Wait for the user's reply with optional timeout
            if self._timeout > 0:
                selected = await asyncio.wait_for(future, timeout=self._timeout)
            else:
                selected = await future
        except TimeoutError:
            # Timeout — auto-cancel and notify the user
            self._pending.pop(chat_id, None)
            logger.info("Permission request timed out for chat %s", chat_id)
            await self._reply_fn(event, f"权限请求已超时（{self._timeout:.0f}秒），已自动取消。")
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        except asyncio.CancelledError:
            # Cancelled (e.g. by /stop) — clean up and propagate
            self._pending.pop(chat_id, None)
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        finally:
            # Ensure pending is cleaned up even on unexpected errors
            self._pending.pop(chat_id, None)

        # If the selected option is an "always" kind, cache the decision
        if selected.kind in ("allow_always", "reject_always"):
            session_cache = self._always.setdefault(session_id, {})
            session_cache[tool_kind] = selected
            logger.info(
                "Cached 'always' decision for session %s kind=%s: %s",
                session_id,
                tool_kind,
                selected.name,
            )

        logger.info(
            "Permission resolved for chat %s: %s (%s)",
            chat_id,
            selected.name,
            selected.kind,
        )
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=selected.option_id)
        )

    def has_pending(self, chat_id: str) -> bool:
        """Check whether a permission request is pending for this chat."""
        return chat_id in self._pending

    def try_resolve(self, chat_id: str, text: str) -> bool:
        """Try to resolve a pending permission request with the user's reply.

        Expects the text to be a 1-based option number (e.g. "1", "2").
        Returns True if the reply was accepted and the permission was resolved,
        False if the input was invalid (caller should show a hint).
        """
        pending = self._pending.get(chat_id)
        if pending is None:
            return False

        # Parse the user's reply as a 1-based option index
        text = text.strip()
        try:
            index = int(text)
        except ValueError:
            return False

        if index < 1 or index > len(pending.options):
            return False

        selected = pending.options[index - 1]
        # Resolve the future (handle() will pick it up and continue)
        if not pending.future.done():
            pending.future.set_result(selected)
        return True

    def cancel_pending(self, chat_id: str) -> None:
        """Cancel a pending permission request (e.g. on /stop).

        The future is cancelled, causing handle() to return a DeniedOutcome.
        """
        pending = self._pending.pop(chat_id, None)
        if pending is not None and not pending.future.done():
            pending.future.cancel()
            logger.info("Pending permission cancelled for chat %s", chat_id)

    def clear_session(self, session_id: str) -> None:
        """Clear the 'always' decision cache for a session.

        Called when an ACP session is closed (e.g. /new, disconnect).
        """
        removed = self._always.pop(session_id, None)
        if removed:
            logger.info(
                "Cleared %d 'always' decisions for session %s",
                len(removed),
                session_id,
            )

    # --- Message formatting ---

    def _format_permission_message(
        self,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
    ) -> str:
        """Format a permission request into a human-readable QQ message."""
        lines: list[str] = []

        # Header: operation description
        kind_label = f"[{tool_call.kind}] " if tool_call.kind else ""
        title = tool_call.title or "(unknown operation)"
        lines.append(f"Agent 请求执行操作：\n{kind_label}{title}")

        # Show raw_input if available
        raw_input = tool_call.raw_input
        if raw_input is not None:
            raw_str = (
                json.dumps(raw_input, ensure_ascii=False, indent=2)
                if not isinstance(raw_input, str)
                else raw_input
            )
            # Truncate if exceeding the configured limit
            if self._raw_input_max_len > 0 and len(raw_str) > self._raw_input_max_len:
                raw_str = raw_str[: self._raw_input_max_len] + "...(已截断)"
            lines.append(f"参数:\n{raw_str}")

        # Timeout hint
        if self._timeout > 0:
            lines.append(f"\n请回复编号选择（{self._timeout:.0f}秒后自动取消）：")
        else:
            lines.append("\n请回复编号选择：")

        # List the available options
        for i, opt in enumerate(options, 1):
            # Translate the kind into a Chinese hint for clarity
            kind_hint = _PERMISSION_KIND_HINTS.get(opt.kind, "")
            hint_suffix = f" ({kind_hint})" if kind_hint else ""
            lines.append(f"{i}. {opt.name}{hint_suffix}")

        return "\n".join(lines)


# Chinese descriptions for permission option kinds, shown as hints to the user
_PERMISSION_KIND_HINTS: dict[str, str] = {
    "allow_once": "允许一次",
    "allow_always": "本会话始终允许同类操作",
    "reject_once": "拒绝一次",
    "reject_always": "本会话始终拒绝同类操作",
}
