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
from ncat.image_utils import (
    ImagePreparationError,
    download_image,
    encode_image_base64,
    prepare_image_for_inline,
)
from ncat.log import error_event, info_event, warning_event
from ncat.models import ContentPart, DownloadedImage, ParsedMessage, PromptImageAttachment, TurnFlush, VisibleTurnEvent
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


def _coerce_downloaded_image(downloaded: object, *, url: str) -> DownloadedImage | tuple[str, str] | None:
    """Accept legacy test doubles that still return `(base64, mime)` tuples."""
    if downloaded is None or isinstance(downloaded, DownloadedImage):
        return downloaded
    if isinstance(downloaded, tuple) and len(downloaded) == 2:
        data_b64, mime_type = downloaded
        if isinstance(data_b64, str) and isinstance(mime_type, str):
            return data_b64, mime_type
    if all(hasattr(downloaded, attr) for attr in ("data", "mime_type")):
        data = getattr(downloaded, "data")
        mime_type = getattr(downloaded, "mime_type")
        if isinstance(data, bytes) and isinstance(mime_type, str):
            return DownloadedImage(
                url=getattr(downloaded, "url", url),
                data=data,
                mime_type=mime_type,
            )
    return None
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
        max_inline_image_mb: int = 2,
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
        self._max_inline_image_bytes = max(1, max_inline_image_mb) * 1024 * 1024
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
            prompt_images: list[PromptImageAttachment] = []
            if supports_image:
                for att in parsed.images:
                    if att.url:
                        downloaded = await download_image(
                            att.url,
                            timeout_seconds=self._image_download_timeout,
                        )
                        downloaded = _coerce_downloaded_image(downloaded, url=att.url)
                        if downloaded is None:
                            raise ImagePreparationError(
                                "图片下载失败，无法发送给 Agent，请稍后重试。"
                            )
                        if isinstance(downloaded, tuple):
                            data_b64, mime_type = downloaded
                            prompt_images.append(
                                PromptImageAttachment(
                                    replacement_text="[图片]",
                                    inline_image_base64=data_b64,
                                    inline_image_mime=mime_type,
                                )
                            )
                            continue
                        prepared = prepare_image_for_inline(
                            downloaded,
                            max_inline_bytes=self._max_inline_image_bytes,
                        )
                        data_b64, mime_type = encode_image_base64(prepared)
                        prompt_images.append(
                            PromptImageAttachment(
                                replacement_text="[图片]",
                                inline_image_base64=data_b64,
                                inline_image_mime=mime_type,
                            )
                        )
                        info_event(
                            logger,
                            "image_inline_selected",
                            "Prepared inline ACP image delivery",
                            chat_id=chat_id,
                            url=att.url,
                            original_mime=downloaded.mime_type,
                            mime_type=mime_type,
                            original_size_bytes=len(downloaded.data),
                            final_size_bytes=len(prepared.data),
                            max_inline_bytes=self._max_inline_image_bytes,
                        )
                    else:
                        prompt_images.append(PromptImageAttachment(replacement_text="[图片]"))
            else:
                # Agent does not support images: use pure text fallback.
                prompt_images = [
                    PromptImageAttachment(
                        replacement_text=(f"[图片 url={att.url.strip()}]" if att.url.strip() else "[图片]")
                    )
                    for att in parsed.images
                ]

            prompt_blocks = build_prompt_blocks(
                parsed,
                prompt_images,
                agent_supports_image=supports_image,
            )

            # Send prompt to the ACP agent and wait for complete response
            response_parts = await self._agent_manager.send_prompt(chat_key, prompt_blocks)

            # Cancel timers immediately after AI responds to avoid late notifications
            for timer in timers:
                timer.cancel()

            await self._flush_visible_events(chat_key, event)
            response_parts = self._agent_manager.consume_completed_turn_parts(chat_key)

            # Send reply based on response
            has_text = any(p.type == "text" and p.text for p in response_parts)
            has_image = any(p.type == "image" and p.image_base64 for p in response_parts)
            turn_had_content = getattr(
                self._agent_manager,
                "turn_had_content",
                lambda cid: False,
            )(chat_key)
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
            elif not turn_had_content:
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
            remaining_partial_parts = e.partial_parts
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

        except ImagePreparationError as e:
            warning_event(
                logger,
                "image_prepare_fail",
                "Failed to prepare image for inline delivery",
                chat_id=chat_key,
                err=str(e),
            )
            await self._reply_fn(event, str(e))

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
        flushes = self._agent_manager.drain_visible_event_flushes(chat_id)
        if not flushes:
            return

        for flush in flushes:
            await self._send_turn_flush(event, flush)

    async def _send_turn_flush(self, event: dict, flush: TurnFlush) -> None:
        if flush.visible_event is not None:
            await self._send_visible_event_flush(event, flush.parts, flush.visible_event)
            return

        if flush.parts:
            await self._reply_content_fn(event, flush.parts)

    async def _send_visible_event_flush(
        self,
        event: dict,
        parts: list[ContentPart],
        visible_event: VisibleTurnEvent,
    ) -> None:
        """Send buffered text plus one visible status line as a QQ reply."""
        if not visible_event.status_text:
            if parts:
                await self._reply_content_fn(event, parts)
            return
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
