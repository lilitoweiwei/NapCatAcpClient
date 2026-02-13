"""Message processing pipeline — business logic for handling QQ messages."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from nochan.command import HELP_TEXT, parse_command
from nochan.converter import ParsedMessage, onebot_to_internal
from nochan.opencode import SubprocessOpenCodeBackend
from nochan.prompt import PromptBuilder
from nochan.session import SessionManager

logger = logging.getLogger("nochan.handler")

# Type alias for the reply callback provided by the transport layer.
# Signature: async reply_fn(event: dict, text: str) -> None
ReplyFn = Callable[[dict, str], Awaitable[None]]

# --- Notification message templates ---
_MSG_THINKING = "消息已收到，AI 正在思考中，请稍候..."
_MSG_THINKING_LONG = "AI 思考时间较长，你可以发送 /stop 中断当前思考。"
_MSG_STOPPED = "已中断当前 AI 思考。"
_MSG_NO_ACTIVE = "当前没有进行中的 AI 思考。"
_MSG_BUSY = "AI 正在思考中，请等待或使用 /stop 中断。"


class MessageHandler:
    """
    Processes incoming QQ message events through the full nochan pipeline:
    parse → filter → command/AI → reply.

    Decoupled from WebSocket transport: sends replies via the reply_fn callback.
    """

    def __init__(
        self,
        session_manager: SessionManager,
        opencode_backend: SubprocessOpenCodeBackend,
        prompt_builder: PromptBuilder,
        reply_fn: ReplyFn,
        thinking_notify_seconds: float = 10,
        thinking_long_notify_seconds: float = 30,
    ) -> None:
        # Session manager for chat-to-opencode session mapping
        self._session_manager = session_manager
        # OpenCode backend for sending prompts and receiving AI responses
        self._opencode_backend = opencode_backend
        # Prompt builder for constructing context-enriched prompts
        self._prompt_builder = prompt_builder
        # Callback to send a text reply back to the QQ message source
        self._reply_fn = reply_fn
        # Seconds before sending first "AI is thinking" notification (0 = disabled)
        self._thinking_notify_seconds = thinking_notify_seconds
        # Seconds before sending "thinking too long, use /stop" notification (0 = disabled)
        self._thinking_long_notify_seconds = thinking_long_notify_seconds
        # Active AI processing tasks per chat, keyed by chat_id
        self._active_tasks: dict[str, asyncio.Task[None]] = {}

    async def handle_message(self, event: dict, bot_id: int) -> None:
        """
        Process an incoming message event through the full pipeline.

        Args:
            event: Raw OneBot 11 message event dict
            bot_id: The bot's own QQ ID (for @bot detection)
        """
        try:
            # Step 1: Parse the message event
            parsed = onebot_to_internal(event, bot_id)

            # Step 2: Group messages require @bot
            if parsed.message_type == "group" and not parsed.is_at_bot:
                logger.debug(
                    "Ignored group message (no @bot): group=%s user=%s text=%s",
                    parsed.chat_id,
                    parsed.sender_name,
                    parsed.text[:100],
                )
                return

            logger.info(
                "Processing message from %s (%s): %s",
                parsed.sender_name,
                parsed.chat_id,
                parsed.text[:100],
            )

            # Step 3: Check for commands (lightweight, non-blocking)
            command = parse_command(parsed.text)
            if command is not None:
                logger.info("Command received: /%s from %s", command, parsed.chat_id)
                await self._handle_command(command, parsed, event)
                return

            # Step 4: Reject if AI is already processing for this chat
            if parsed.chat_id in self._active_tasks:
                logger.info("Busy rejection for %s (AI already processing)", parsed.chat_id)
                await self._reply_fn(event, _MSG_BUSY)
                return

            # Step 5: Process the AI request with timeout notifications
            await self._process_ai_request(parsed, event)

        except asyncio.CancelledError:
            # Let cancellation propagate cleanly (don't send error message for /stop)
            raise
        except Exception as e:
            logger.error("Error handling message: %s", e, exc_info=True)
            with contextlib.suppress(Exception):
                await self._reply_fn(event, "处理消息时发生内部错误")

    async def _handle_command(self, command: str, parsed: ParsedMessage, event: dict) -> None:
        """Handle a user command (/new, /stop, /help, etc.)."""
        if command == "new":
            # Archive current session and create a new one
            await self._session_manager.archive_active_session(parsed.chat_id)
            await self._session_manager.create_session(parsed.chat_id)
            await self._reply_fn(event, "已创建新会话，AI 上下文已清空。")
            logger.info("New session created for %s", parsed.chat_id)

        elif command == "stop":
            # Cancel the active AI task for this chat
            task = self._active_tasks.get(parsed.chat_id)
            if task is not None and not task.done():
                task.cancel()
                await self._reply_fn(event, _MSG_STOPPED)
                logger.info("AI task cancelled for %s", parsed.chat_id)
            else:
                await self._reply_fn(event, _MSG_NO_ACTIVE)

        elif command == "help" or command == "unknown":
            await self._reply_fn(event, HELP_TEXT)

    async def _process_ai_request(self, parsed: ParsedMessage, event: dict) -> None:
        """
        Process an AI request with timeout notifications and cancellation support.

        Registers the current task in _active_tasks so /stop can cancel it.
        Starts background timer tasks for "thinking" notifications.
        """
        chat_key = parsed.chat_id
        current_task = asyncio.current_task()
        assert current_task is not None

        # Register as the active AI task for this chat
        self._active_tasks[chat_key] = current_task

        # Start timeout notification timer tasks
        timers: list[asyncio.Task[None]] = []
        if self._thinking_notify_seconds > 0:
            timers.append(
                asyncio.create_task(
                    self._send_after_delay(event, self._thinking_notify_seconds, _MSG_THINKING)
                )
            )
        if self._thinking_long_notify_seconds > 0:
            timers.append(
                asyncio.create_task(
                    self._send_after_delay(
                        event, self._thinking_long_notify_seconds, _MSG_THINKING_LONG
                    )
                )
            )

        try:
            # Get or create session
            session = await self._session_manager.get_active_session(chat_key)
            if session is None:
                session = await self._session_manager.create_session(chat_key)

            # Build prompt with context (include session_init on first message)
            is_new_session = session.opencode_session_id is None
            prompt = self._prompt_builder.build(parsed, is_new_session)

            # Check queue and send queuing notice if needed
            if self._opencode_backend.is_queue_full():
                await self._reply_fn(event, "AI 正在忙，你的请求已排队，请稍候...")

            # Call OpenCode (this is the long-running part, cancellable by /stop)
            response = await self._opencode_backend.send_message(
                session.opencode_session_id, prompt
            )

            # Cancel timers immediately after AI responds to avoid late notifications
            for timer in timers:
                timer.cancel()

            # Update session with OpenCode session ID if new
            if session.opencode_session_id is None and response.session_id:
                await self._session_manager.update_opencode_session_id(
                    session.id, response.session_id
                )

            # Send reply based on response status
            if response.success and response.content:
                logger.info(
                    "Sending AI reply to %s (%d chars)",
                    chat_key,
                    len(response.content),
                )
                await self._reply_fn(event, response.content)
            elif response.success and not response.content:
                logger.warning("OpenCode returned empty content for %s", chat_key)
                await self._reply_fn(event, "AI 未返回有效回复")
            else:
                logger.error(
                    "OpenCode failed for %s: %s",
                    chat_key,
                    response.error,
                )
                await self._reply_fn(event, "AI 处理出错，请稍后重试")

        except asyncio.CancelledError:
            # Cancelled by /stop — notification already sent by the /stop handler
            logger.info("AI request cancelled for %s (user /stop)", chat_key)
            raise

        finally:
            # Ensure all notification timers are cancelled (idempotent)
            for timer in timers:
                timer.cancel()
            # Remove from active tasks if we are still the registered task
            if self._active_tasks.get(chat_key) is current_task:
                del self._active_tasks[chat_key]

    async def _send_after_delay(self, event: dict, delay: float, message: str) -> None:
        """Send a notification message after a delay (used for timeout notifications)."""
        await asyncio.sleep(delay)
        logger.info("Sending timeout notification: %s", message[:50])
        await self._reply_fn(event, message)
