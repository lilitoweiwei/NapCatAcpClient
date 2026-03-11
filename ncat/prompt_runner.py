"""Prompt lifecycle manager — handles ACP prompt calls with timeout and cancellation.

Owns the per-chat active task tracking, timeout notification timers,
and ACP agent interaction via AgentManager.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ncat.agent_manager import (
    MSG_AGENT_NOT_CONNECTED,
    AgentErrorWithPartialContent,
    AgentManager,
)
from ncat.image_utils import download_image
from ncat.log import error_event, info_event, warning_event
from ncat.models import ContentPart, ParsedMessage, VisibleTurnEvent
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
        # Seconds before sending first "AI is thinking" notification (0 = disabled)
        self._thinking_notify_seconds = thinking_notify_seconds
        # Seconds before sending "thinking too long, use /stop" notification (0 = disabled)
        self._thinking_long_notify_seconds = thinking_long_notify_seconds
        # Timeout for downloading images from NapCat URLs (used when agent supports images)
        self._image_download_timeout = image_download_timeout
        # Active AI processing tasks per chat, keyed by chat_id
        self._active_tasks: dict[str, asyncio.Task[None]] = {}
        # Number of content parts already flushed to the user for the active turn.
        self._flushed_part_counts: dict[str, int] = {}

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
            # Also send ACP cancel notification to the agent
            cancel_task = asyncio.create_task(self._agent_manager.cancel(chat_id))
            cancel_task.add_done_callback(lambda t: t.result() if not t.cancelled() else None)
            info_event(logger, "prompt_cancelled", "AI task cancelled", chat_id=chat_id)
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
        self._flushed_part_counts[chat_key] = 0
        self._agent_manager.set_visible_event_notifier(
            chat_key,
            lambda: self._flush_visible_events(chat_key, event),
        )

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
            chat_id = parsed.chat_id
            await self._agent_manager.ensure_connection(chat_id)
            supports_image = getattr(
                self._agent_manager,
                "supports_image",
                lambda cid: False,
            )(chat_id)
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

            await self._flush_visible_events(chat_key, event)

            flushed_part_count = self._flushed_part_counts.get(chat_key, 0)
            if flushed_part_count:
                response_parts = response_parts[flushed_part_count:]

            # Send reply based on response
            has_text = any(p.type == "text" and p.text for p in response_parts)
            has_image = any(p.type == "image" and p.image_base64 for p in response_parts)
            if has_text or has_image:
                text_len = sum(len(p.text) for p in response_parts if p.type == "text")
                info_event(
                    logger,
                    "reply_ready",
                    "Sending AI reply",
                    chat_id=chat_key,
                    text_len=text_len,
                    part_count=len(response_parts),
                )
                await self._reply_content_fn(event, response_parts)
            else:
                warning_event(
                    logger,
                    "reply_empty",
                    "Agent returned empty content",
                    chat_id=chat_key,
                )
                await self._reply_fn(event, "AI 未返回有效回复")

        except asyncio.CancelledError:
            # Cancelled by /stop — notification already sent by the command executor
            info_event(
                logger,
                "prompt_cancelled_user",
                "AI request cancelled by user",
                chat_id=chat_key,
            )
            raise

        except AgentErrorWithPartialContent as e:
            # Agent failed mid-stream; send any partial content first, then the error
            error_event(
                logger,
                "prompt_fail_partial",
                "AI processing failed after partial output",
                chat_id=chat_key,
                err=str(e.cause),
                exc_info=True,
            )
            remaining_partial_parts = e.partial_parts[self._flushed_part_counts.get(chat_key, 0) :]
            if remaining_partial_parts:
                has_text = any(p.type == "text" and p.text for p in remaining_partial_parts)
                has_image = any(
                    p.type == "image" and p.image_base64 for p in remaining_partial_parts
                )
                if has_text or has_image:
                    await self._reply_content_fn(event, remaining_partial_parts)
                    await self._reply_fn(
                        event,
                        "Agent 发生错误，以上为已生成的部分内容。\n"
                        f"错误信息：{e.cause}\n"
                        "当前会话已关闭，下次对话将自动开启新会话。",
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
            # Agent not running (e.g. connection failed or crashed)
            error_event(
                logger,
                "agent_error",
                "Agent runtime error",
                chat_id=chat_key,
                err=str(e),
            )
            if str(e) == MSG_AGENT_NOT_CONNECTED:
                await self._reply_fn(event, MSG_AGENT_NOT_CONNECTED)
            else:
                await self._reply_fn(
                    event, f"Agent 异常：{e}\n当前会话已关闭，下次对话将自动开启新会话。"
                )
                await self._agent_manager.close_session(chat_key)

        except TimeoutError as e:
            # ACP initialize or connection timed out (e.g. agent cold start > timeout)
            error_event(
                logger,
                "agent_connect_timeout",
                "Agent connection timeout",
                chat_id=chat_key,
                err=str(e),
            )
            await self._reply_fn(
                event, "连接 Agent 超时，请稍后再试。"
            )

        except Exception as e:
            error_event(
                logger,
                "prompt_fail",
                "AI processing error",
                chat_id=chat_key,
                err=str(e),
                exc_info=True,
            )
            await self._reply_fn(
                event, f"Agent 异常：{e}\n当前会话已关闭，下次对话将自动开启新会话。"
            )
            # Close session on any unexpected error
            await self._agent_manager.close_session(chat_key)

        finally:
            self._agent_manager.set_visible_event_notifier(chat_key, None)
            self._flushed_part_counts.pop(chat_key, None)
            self._agent_manager.clear_completed_turn_state(chat_key)
            # Ensure all notification timers are cancelled (idempotent)
            for timer in timers:
                timer.cancel()
            # Remove from active tasks if we are still the registered task
            if self._active_tasks.get(chat_key) is current_task:
                del self._active_tasks[chat_key]

    async def _send_after_delay(self, event: dict, delay: float, message: str) -> None:
        """Send a notification message after a delay (used for timeout notifications)."""
        await asyncio.sleep(delay)
        info_event(
            logger,
            "thinking_notice_sent",
            "Sending timeout notification",
            msg_preview=message[:50],
        )
        await self._reply_fn(event, message)

    async def _flush_visible_events(self, chat_id: str, event: dict) -> None:
        """Flush buffered content when a visible ACP event boundary arrives."""
        flushed_part_count = self._flushed_part_counts.get(chat_id, 0)
        flushes, next_flushed_part_count = self._agent_manager.drain_visible_event_flushes(
            chat_id,
            flushed_part_count,
        )
        if not flushes:
            return

        self._flushed_part_counts[chat_id] = next_flushed_part_count
        for parts, visible_event in flushes:
            await self._send_visible_event_flush(event, parts, visible_event)

    async def _send_visible_event_flush(
        self,
        event: dict,
        parts: list[ContentPart],
        visible_event: VisibleTurnEvent,
    ) -> None:
        """Send buffered text plus one visible status line as a QQ reply."""
        status_part = ContentPart(type="text", text=visible_event.status_text)
        if parts:
            merged_parts = list(parts)
            if merged_parts[-1].type == "text":
                suffix = "\n" if merged_parts[-1].text else ""
                merged_parts[-1] = ContentPart(
                    type="text",
                    text=f"{merged_parts[-1].text}{suffix}{visible_event.status_text}",
                )
            else:
                merged_parts.append(status_part)
            await self._reply_content_fn(event, merged_parts)
            return

        await self._reply_fn(event, visible_event.status_text)
