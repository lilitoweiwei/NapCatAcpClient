"""Message dispatcher — thin orchestrator that routes messages.

Routes incoming QQ message events to either the CommandExecutor (for /commands)
or the PromptRunner (for AI requests). Handles filtering and busy rejection.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from ncat.agent_manager import AgentManager
from ncat.command import HELP_TEXT, CommandExecutor
from ncat.converter import onebot_to_internal
from ncat.models import ContentPart
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


class MessageDispatcher:
    """
    Thin dispatcher: parse → filter → route to CommandExecutor or PromptRunner.

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
    ) -> None:
        # Callback to send a text reply back to the QQ message source
        self._reply_fn = reply_fn
        # Agent manager
        self._agent_manager = agent_manager

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
        )

        # Command executor (cancel_fn bridges to PromptRunner without direct import)
        self._cmd = CommandExecutor(
            agent_manager=agent_manager,
            reply_fn=reply_fn,
            cancel_fn=self._ai.cancel,
        )

    async def handle_message(self, event: dict, bot_id: int) -> None:
        """
        Process an incoming message event through the full pipeline.

        Routing order:
        1. Filter (group @bot check)
        2. /send prefix stripping (forward to agent bypassing commands)
        3. Commands (/new, /stop, /help)
        4. Busy rejection (AI already processing)
        5. AI prompt dispatch

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

            # Step 3: Handle /send — strip prefix and forward as a regular
            # message so that agent slash commands (e.g. /help) don't collide
            # with ncat's own commands.
            send_forwarded = False
            if parsed.text.startswith("/send"):
                rest = parsed.text[5:]  # strip "/send"
                if not rest or rest[0] == " ":
                    body = rest.lstrip(" ") if rest else ""
                    if not body:
                        # /send with no payload — show usage hint
                        await self._reply_fn(event, HELP_TEXT)
                        return
                    parsed.text = body
                    send_forwarded = True

            # Step 4: Try to handle as a command (skip if /send forwarded)
            if not send_forwarded and await self._cmd.try_handle(parsed, event):
                return

            # Step 5: Reject if AI is already processing for this chat
            if self._ai.is_busy(parsed.chat_id):
                logger.info(
                    "Busy rejection for %s (AI already processing)",
                    parsed.chat_id,
                )
                await self._reply_fn(event, _MSG_BUSY)
                return

            # Step 6: Dispatch to AI prompt runner (connection is established on demand in send_prompt)
            logger.debug(
                "Dispatching to AI for %s (agent is_running=%s)",
                parsed.chat_id,
                self._agent_manager.is_running(parsed.chat_id),
            )
            await self._ai.process(parsed, event)

        except asyncio.CancelledError:
            # Let cancellation propagate cleanly (don't send error message for /stop)
            raise
        except Exception as e:
            logger.error("Error handling message: %s", e, exc_info=True)
            with contextlib.suppress(Exception):
                await self._reply_fn(event, "处理消息时发生内部错误")
