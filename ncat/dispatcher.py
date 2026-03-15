"""Message dispatcher — thin orchestrator that routes messages.

Routes incoming QQ message events to either the unified command system
or the PromptRunner (for AI requests). Handles filtering and busy rejection.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

# Import bg_command to register /bg * commands (must be after command_registry is created)
# This module has side effects: registers commands in command_registry
from ncat import bg_command  # noqa: F401
from ncat.agent_manager import AgentManager
from ncat.bsp_client import BspClient
from ncat.command import command_registry, get_help_text
from ncat.converter import onebot_to_internal
from ncat.file_ingress import best_effort_download_private_file
from ncat.log import debug_event, error_event, info_event, warning_event
from ncat.models import ContentPart
from ncat.pending_inputs import PendingInputStore
from ncat.prompt_runner import PromptRunner

logger = logging.getLogger("ncat.dispatcher")

# Type alias for the reply callback provided by the transport layer.
# Signature: async reply_fn(event: dict, text: str) -> None
ReplyFn = Callable[[dict, str], Awaitable[None]]
# Type alias for mixed content replies (text + images).
# Signature: async reply_content_fn(event: dict, parts: list[ContentPart]) -> None
ReplyContentFn = Callable[[dict, list[ContentPart]], Awaitable[None]]

# Busy rejection message (dispatching-level concern)
_MSG_BUSY = "AI 正在思考中，请等待或使用 /stop 中断。"
_MSG_ATTACHMENT_WAIT = "已收到文件/图片，请继续发送说明。"
_MSG_ATTACHMENT_WAIT_FILE = "已收到文件。（文件已保存到 {path}）请继续发送说明。"
_MSG_ATTACHMENT_FAIL = "文件已收到，但保存失败。请补充说明或稍后重试。"


class MessageDispatcher:
    """
    Thin dispatcher: parse → filter → route to command system or PromptRunner.

    Decoupled from WebSocket transport: sends replies via the reply_fn callback.
    """

    def __init__(
        self,
        agent_manager: AgentManager,
        reply_fn: ReplyFn,
        reply_content_fn: ReplyContentFn | None = None,
        thinking_notify_seconds: float = 10,
        thinking_long_notify_seconds: float = 30,
        image_download_timeout: float = 15.0,
        file_ingress_enabled: bool = True,
        file_inbox_dirname: str = ".qqfiles",
        file_download_timeout: float = 30.0,
        pending_ttl_seconds: float = 1800.0,
        max_file_size_mb: int | None = None,
        max_inline_image_mb: int = 2,
        get_file_fn: Callable[[str], Awaitable[dict | None]] | None = None,
        bsp_client: BspClient | None = None,
    ) -> None:
        # Callback to send a text reply back to the QQ message source
        self._reply_fn = reply_fn
        # Agent manager
        self._agent_manager = agent_manager
        # BSP client for background session management
        self._bsp_client = bsp_client
        self._file_ingress_enabled = file_ingress_enabled
        self._file_inbox_dirname = file_inbox_dirname
        self._file_download_timeout = file_download_timeout
        self._max_file_size_mb = max_file_size_mb
        self._get_file_fn = get_file_fn
        self._pending_inputs = PendingInputStore(ttl_seconds=pending_ttl_seconds)

        async def _reply_content_fallback(event: dict, parts: list[ContentPart]) -> None:
            # Fallback: deliver text-only if the transport doesn't support images.
            text = "".join(p.text for p in parts if p.type == "text")
            await reply_fn(event, text or "AI 未返回有效回复")

        # Prompt lifecycle manager (owns active task tracking)
        self._ai = PromptRunner(
            agent_manager=agent_manager,
            reply_fn=reply_fn,
            reply_content_fn=reply_content_fn or _reply_content_fallback,
            thinking_notify_seconds=thinking_notify_seconds,
            thinking_long_notify_seconds=thinking_long_notify_seconds,
            image_download_timeout=image_download_timeout,
            max_inline_image_mb=max_inline_image_mb,
        )

        # Configure command registry with dependencies
        command_registry.set_dependency("agent_manager", agent_manager)
        command_registry.set_dependency("bsp_client", bsp_client)
        command_registry.set_dependency("cancel_fn", self._ai.cancel)
        command_registry.set_dependency("busy_fn", self._ai.is_busy)
        command_registry.set_dependency("pending_input_store", self._pending_inputs)

        info_event(
            logger,
            "command_system_ready",
            "Command system initialized",
            command_count=command_registry.get_command_count(),
        )

    async def handle_message(self, event: dict, bot_id: int | None = None) -> None:
        """
        Handle an incoming QQ message event.

        This is the main entry point called by napcat_server.py.
        The bot_id parameter is kept for backward compatibility but not used.

        Args:
            event: Raw QQ message event dict
            bot_id: Bot QQ ID (unused, kept for API compatibility)
        """
        await self.dispatch(event, bot_id)

    async def dispatch(self, event: dict, bot_id: int | None = None) -> None:
        """
        Dispatch an incoming QQ message event.

        Flow:
        1. Convert OneBot event to internal ParsedMessage
        2. Handle /send specially (strip prefix, forward to AI)
        3. Try to handle as a command
        4. Reject if AI is busy
        5. Dispatch to AI prompt runner

        Args:
            event: Raw QQ message event dict
        """
        try:
            # Step 1: Convert OneBot event to internal message format
            if bot_id is None:
                warning_event(
                    logger,
                    "message_ignored",
                    "Received message before bot_id was available",
                )
                return

            parsed = onebot_to_internal(event, bot_id)
            if not parsed:
                return

            self._pending_inputs.cleanup_expired()

            if parsed.message_type == "group" and not parsed.is_at_bot:
                return

            chat_id = parsed.chat_id
            debug_event(
                logger,
                "message_received",
                "Received message",
                chat_id=chat_id,
                message_type=parsed.message_type,
                msg_preview=parsed.text[:50] + "..." if len(parsed.text) > 50 else parsed.text,
            )

            # Step 2: Handle /send command specially (strip prefix, forward to AI)
            send_forwarded = False
            if parsed.text.startswith("/send "):
                body = parsed.text[6:].strip()
                if not body:
                    # /send with no payload - show usage hint
                    await self._reply_fn(event, get_help_text())
                    return
                parsed.text = body
                send_forwarded = True

            # Step 3: Try to handle as a command (skip if /send forwarded)
            if not send_forwarded:
                try:
                    matched = await command_registry.execute(
                        parsed.text,
                        chat_id=chat_id,
                        event=event,
                        reply_fn=self._reply_fn,
                    )
                    if matched:
                        return
                    if parsed.text.startswith("/"):
                        await self._reply_fn(event, get_help_text())
                        return
                except Exception:
                    # Error already logged by command handler
                    return

            # Step 4: Reject if AI is already processing for this chat
            if self._ai.is_busy(chat_id):
                info_event(
                    logger,
                    "message_rejected_busy",
                    "Busy rejection while AI is already processing",
                    chat_id=chat_id,
                )
                await self._reply_fn(event, _MSG_BUSY)
                return

            if await self._handle_attachment_only_message(parsed, event):
                return

            self._merge_pending_inputs(parsed)

            if not parsed.has_text:
                return

            # Step 5: Dispatch to AI prompt runner
            # (connection is established on demand in send_prompt)
            debug_event(
                logger,
                "message_dispatched",
                "Dispatching message to AI",
                chat_id=chat_id,
                agent_running=self._agent_manager.is_running(chat_id),
            )
            await self._ai.process(parsed, event)

        except asyncio.CancelledError:
            # Let cancellation propagate cleanly (don't send error message for /stop)
            raise
        except Exception as e:
            error_event(
                logger,
                "message_handle_fail",
                "Error handling message",
                chat_id=event.get("user_id") or event.get("group_id"),
                err=str(e),
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await self._reply_fn(event, "处理消息时发生内部错误")

    async def _handle_attachment_only_message(self, parsed, event: dict) -> bool:
        """Buffer private attachment-only messages until the next text arrives."""
        if parsed.message_type != "private":
            return False
        if parsed.has_text:
            return False
        if not parsed.images and not parsed.files:
            return False

        chat_id = parsed.chat_id
        saved_files = []
        file_failed = False
        if self._file_ingress_enabled:
            workspace_cwd = self._agent_manager.get_workspace_cwd(chat_id)
            for attachment in parsed.files:
                saved = await best_effort_download_private_file(
                    attachment=attachment,
                    workspace_cwd=workspace_cwd,
                    inbox_dirname=self._file_inbox_dirname,
                    timeout_seconds=self._file_download_timeout,
                    max_file_size_mb=self._max_file_size_mb,
                    get_file=self._get_file_data,
                )
                if saved is None:
                    file_failed = True
                    continue
                saved_files.append(saved)
        elif parsed.files:
            file_failed = True

        if saved_files:
            self._pending_inputs.add_files(chat_id, saved_files)
        if parsed.images:
            self._pending_inputs.add_images(chat_id, parsed.images)

        if file_failed and not saved_files and not parsed.images:
            await self._reply_fn(event, _MSG_ATTACHMENT_FAIL)
            return True

        if saved_files and not parsed.images:
            await self._reply_fn(
                event,
                _MSG_ATTACHMENT_WAIT_FILE.format(path=saved_files[0].saved_path),
            )
            return True

        await self._reply_fn(event, _MSG_ATTACHMENT_WAIT)
        return True

    def _merge_pending_inputs(self, parsed) -> None:
        """Move any buffered attachments into the current text-bearing prompt."""
        if not parsed.has_text:
            return
        pending = self._pending_inputs.pop_all(parsed.chat_id)
        if pending is None:
            return
        parsed.pending_files = pending.files
        if pending.images:
            pending_prefix = "\n".join("[图片]" for _ in pending.images)
            if parsed.text:
                parsed.text = f"{pending_prefix}\n{parsed.text}"
            else:
                parsed.text = pending_prefix
            parsed.images = [*pending.images, *parsed.images]

    async def _get_file_data(self, file_id: str) -> dict | None:
        """Fetch file metadata via NapCat if the transport exposed an API callback."""
        if self._get_file_fn is None:
            return None
        response = await self._get_file_fn(file_id)
        if not isinstance(response, dict):
            return None
        data = response.get("data")
        return data if isinstance(data, dict) else response

    def clear_pending_inputs(self, chat_id: str | None = None) -> None:
        """Clear buffered attachments for one chat or for all chats."""
        if chat_id is None:
            self._pending_inputs.clear_all()
        else:
            self._pending_inputs.clear(chat_id)
