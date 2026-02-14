"""AI request lifecycle manager — handles OpenCode calls with timeout and cancellation.

Owns the per-chat active task tracking, timeout notification timers,
session management, prompt building, and OpenCode invocation.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ncat.converter import ParsedMessage
from ncat.opencode import SubprocessOpenCodeBackend
from ncat.prompt import PromptBuilder
from ncat.session import SessionManager

logger = logging.getLogger("ncat.ai_processor")

# Type alias for the reply callback provided by the transport layer.
# Signature: async reply_fn(event: dict, text: str) -> None
ReplyFn = Callable[[dict, str], Awaitable[None]]

# --- Notification message templates ---
_MSG_THINKING = "消息已收到，AI 正在思考中，请稍候..."
_MSG_THINKING_LONG = "AI 思考时间较长，你可以发送 /stop 中断当前思考。"


class AiProcessor:
    """
    Manages the full AI request lifecycle: session management, prompt building,
    OpenCode invocation, timeout notifications, and cancellation support.

    Exposes is_busy() and cancel() for use by the message handler and command
    executor respectively.
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

    def is_busy(self, chat_id: str) -> bool:
        """Check if there is an active AI task for the given chat."""
        return chat_id in self._active_tasks

    def cancel(self, chat_id: str) -> bool:
        """
        Cancel the active AI task for the given chat.

        Returns True if a task was found and cancellation was requested,
        False if no active task exists for this chat.
        """
        task = self._active_tasks.get(chat_id)
        if task is not None and not task.done():
            task.cancel()
            logger.info("AI task cancelled for %s", chat_id)
            return True
        return False

    async def process(self, parsed: ParsedMessage, event: dict) -> None:
        """
        Process an AI request with timeout notifications and cancellation support.

        Registers the current task in _active_tasks so cancel() can stop it.
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
                    self._send_after_delay(
                        event, self._thinking_notify_seconds, _MSG_THINKING
                    )
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
                await self._reply_fn(
                    event, "AI 正在忙，你的请求已排队，请稍候..."
                )

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
            # Cancelled by /stop — notification already sent by the command executor
            logger.info("AI request cancelled for %s (user /stop)", chat_key)
            raise

        finally:
            # Ensure all notification timers are cancelled (idempotent)
            for timer in timers:
                timer.cancel()
            # Remove from active tasks if we are still the registered task
            if self._active_tasks.get(chat_key) is current_task:
                del self._active_tasks[chat_key]

    async def _send_after_delay(
        self, event: dict, delay: float, message: str
    ) -> None:
        """Send a notification message after a delay (used for timeout notifications)."""
        await asyncio.sleep(delay)
        logger.info("Sending timeout notification: %s", message[:50])
        await self._reply_fn(event, message)
