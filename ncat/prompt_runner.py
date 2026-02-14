"""Prompt lifecycle manager — handles ACP prompt calls with timeout and cancellation.

Owns the per-chat active task tracking, timeout notification timers,
and ACP agent interaction via AgentManager.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ncat.acp_client import AgentManager
from ncat.converter import ParsedMessage, build_context_header
from ncat.permission import PermissionBroker

logger = logging.getLogger("ncat.prompt_runner")

# Type alias for the reply callback provided by the transport layer.
# Signature: async reply_fn(event: dict, text: str) -> None
ReplyFn = Callable[[dict, str], Awaitable[None]]

# --- Notification message templates ---
_MSG_THINKING = "消息已收到，AI 正在思考中，请稍候..."
_MSG_THINKING_LONG = "AI 思考时间较长，你可以发送 /stop 中断当前思考。"


class PromptRunner:
    """
    Manages the full AI request lifecycle: prompt building, ACP agent
    interaction, timeout notifications, and cancellation support.

    Exposes is_busy() and cancel() for use by the message handler and command
    executor respectively.
    """

    def __init__(
        self,
        agent_manager: AgentManager,
        reply_fn: ReplyFn,
        permission_broker: PermissionBroker,
        thinking_notify_seconds: float = 10,
        thinking_long_notify_seconds: float = 30,
    ) -> None:
        # ACP agent manager for sending prompts and cancellation
        self._agent_manager = agent_manager
        # Callback to send a text reply back to the QQ message source
        self._reply_fn = reply_fn
        # Permission broker (cancel pending requests on /stop)
        self._permission_broker = permission_broker
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

        Also cancels any pending permission request for this chat (the
        PermissionBroker future will be cancelled, causing the ACP handler
        to return a DeniedOutcome).

        Returns True if a task was found and cancellation was requested,
        False if no active task exists for this chat.
        """
        task = self._active_tasks.get(chat_id)
        if task is not None and not task.done():
            # Cancel any pending permission request first (resolves immediately)
            self._permission_broker.cancel_pending(chat_id)
            task.cancel()
            # Also send ACP cancel notification to the agent
            cancel_task = asyncio.create_task(self._agent_manager.cancel(chat_id))
            cancel_task.add_done_callback(lambda t: t.result() if not t.cancelled() else None)
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
            # Build context-enriched prompt text
            prompt_text = build_context_header(parsed)

            # Send prompt to the ACP agent and wait for complete response
            response_text = await self._agent_manager.send_prompt(chat_key, prompt_text)

            # Cancel timers immediately after AI responds to avoid late notifications
            for timer in timers:
                timer.cancel()

            # Send reply based on response
            if response_text:
                logger.info(
                    "Sending AI reply to %s (%d chars)",
                    chat_key,
                    len(response_text),
                )
                await self._reply_fn(event, response_text)
            else:
                logger.warning("Agent returned empty content for %s", chat_key)
                await self._reply_fn(event, "AI 未返回有效回复")

        except asyncio.CancelledError:
            # Cancelled by /stop — notification already sent by the command executor
            logger.info("AI request cancelled for %s (user /stop)", chat_key)
            raise

        except RuntimeError as e:
            # Agent not running (e.g. crashed)
            logger.error("Agent error for %s: %s", chat_key, e)
            await self._reply_fn(
                event, f"Agent 异常：{e}\n当前会话已关闭，下次对话将自动开启新会话。"
            )
            # Close the session for this chat on agent crash
            await self._agent_manager.close_session(chat_key)

        except Exception as e:
            logger.error("AI processing error for %s: %s", chat_key, e, exc_info=True)
            await self._reply_fn(
                event, f"Agent 异常：{e}\n当前会话已关闭，下次对话将自动开启新会话。"
            )
            # Close session on any unexpected error
            await self._agent_manager.close_session(chat_key)

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
