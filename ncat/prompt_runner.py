"""Prompt lifecycle manager — handles ACP prompt calls with timeout and cancellation.

Owns the per-chat active task tracking, timeout notification timers,
and ACP agent interaction via AgentManager.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ncat.agent_manager import AgentErrorWithPartialContent, AgentManager
from ncat.image_utils import download_image
from ncat.models import ContentPart, ParsedMessage
from ncat.permission import PermissionBroker
from ncat.prompt_builder import build_prompt_blocks

logger = logging.getLogger("ncat.prompt_runner")

# Type alias for the reply callback provided by the transport layer.
# Signature: async reply_fn(event: dict, text: str) -> None
ReplyFn = Callable[[dict, str], Awaitable[None]]
# Type alias for mixed content replies (text + images).
# Signature: async reply_content_fn(event: dict, parts: list[ContentPart]) -> None
ReplyContentFn = Callable[[dict, list[ContentPart]], Awaitable[None]]

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
        reply_content_fn: ReplyContentFn | None = None,
        thinking_notify_seconds: float = 10,
        thinking_long_notify_seconds: float = 30,
        image_download_timeout: float = 15.0,
    ) -> None:
        # ACP agent manager for sending prompts and cancellation
        self._agent_manager = agent_manager
        # Callback to send a text reply back to the QQ message source
        self._reply_fn = reply_fn

        # Callback to send a mixed (text+image) reply back to the QQ message source
        async def _reply_content_fallback(event: dict, parts: list[ContentPart]) -> None:
            text = "".join(p.text for p in parts if p.type == "text")
            await self._reply_fn(event, text or "AI 未返回有效回复")

        self._reply_content_fn = reply_content_fn or _reply_content_fallback
        # Permission broker (cancel pending requests on /stop)
        self._permission_broker = permission_broker
        # Seconds before sending first "AI is thinking" notification (0 = disabled)
        self._thinking_notify_seconds = thinking_notify_seconds
        # Seconds before sending "thinking too long, use /stop" notification (0 = disabled)
        self._thinking_long_notify_seconds = thinking_long_notify_seconds
        # Timeout for downloading images from NapCat URLs (used when agent supports images)
        self._image_download_timeout = image_download_timeout
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
            supports_image = getattr(self._agent_manager, "supports_image", False)
            downloaded_images: list[tuple[str, str] | None] = []
            if supports_image:
                # Download images before sending the prompt so we can attach ACP image blocks.
                for att in parsed.images:
                    if att.url:
                        downloaded_images.append(
                            await download_image(
                                att.url,
                                timeout_seconds=self._image_download_timeout,
                            )
                        )
                    else:
                        downloaded_images.append(None)
            else:
                # Agent does not support images: use pure text fallback.
                downloaded_images = [None] * len(parsed.images)

            prompt_blocks = build_prompt_blocks(
                parsed,
                downloaded_images,
                agent_supports_image=supports_image,
            )

            # Send prompt to the ACP agent and wait for complete response
            response_parts = await self._agent_manager.send_prompt(chat_key, prompt_blocks)

            # Cancel timers immediately after AI responds to avoid late notifications
            for timer in timers:
                timer.cancel()

            # Send reply based on response
            has_text = any(p.type == "text" and p.text for p in response_parts)
            has_image = any(p.type == "image" and p.image_base64 for p in response_parts)
            if has_text or has_image:
                text_len = sum(len(p.text) for p in response_parts if p.type == "text")
                logger.info(
                    "Sending AI reply to %s (text=%d chars, parts=%d)",
                    chat_key,
                    text_len,
                    len(response_parts),
                )
                await self._reply_content_fn(event, response_parts)
            else:
                logger.warning("Agent returned empty content for %s", chat_key)
                await self._reply_fn(event, "AI 未返回有效回复")

        except asyncio.CancelledError:
            # Cancelled by /stop — notification already sent by the command executor
            logger.info("AI request cancelled for %s (user /stop)", chat_key)
            raise

        except AgentErrorWithPartialContent as e:
            # Agent failed mid-stream; send any partial content first, then the error
            logger.error("AI processing error for %s: %s", chat_key, e.cause, exc_info=True)
            if e.partial_parts:
                has_text = any(p.type == "text" and p.text for p in e.partial_parts)
                has_image = any(p.type == "image" and p.image_base64 for p in e.partial_parts)
                if has_text or has_image:
                    await self._reply_content_fn(event, e.partial_parts)
                    await self._reply_fn(
                        event,
                        f"Agent 发生错误，以上为已生成的部分内容。\n错误信息：{e.cause}\n当前会话已关闭，下次对话将自动开启新会话。",
                    )
                else:
                    await self._reply_fn(
                        event, f"Agent 异常：{e.cause}\n当前会话已关闭，下次对话将自动开启新会话。"
                    )
            else:
                await self._reply_fn(
                    event, f"Agent 异常：{e.cause}\n当前会话已关闭，下次对话将自动开启新会话。"
                )
            await self._agent_manager.close_session(chat_key)

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
