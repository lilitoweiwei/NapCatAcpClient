"""User command parsing and execution — handles /new, /stop, /help, etc.

parse_command() is a pure function for identifying commands from message text.
CommandExecutor handles the actual execution of commands, with dependencies
injected via constructor to stay decoupled from the AI processing layer.
"""

import logging
from collections.abc import Awaitable, Callable

from nochan.converter import ParsedMessage
from nochan.session import SessionManager

logger = logging.getLogger("nochan.command")

# Type alias for the reply callback provided by the transport layer.
# Signature: async reply_fn(event: dict, text: str) -> None
ReplyFn = Callable[[dict, str], Awaitable[None]]

# Type alias for the cancel callback provided by AiProcessor.
# Signature: cancel_fn(chat_id: str) -> bool (True if cancelled, False if no active task)
CancelFn = Callable[[str], bool]

# Help text template shown for /help and unknown commands
HELP_TEXT = (
    "nochan 指令列表：\n"
    "/new  - 创建新会话（清空 AI 上下文）\n"
    "/stop - 中断当前 AI 思考\n"
    "/help - 显示本帮助信息\n"
    "直接发送文字即可与 AI 对话。"
)

# --- Command response messages ---
_MSG_STOPPED = "已中断当前 AI 思考。"
_MSG_NO_ACTIVE = "当前没有进行中的 AI 思考。"


def parse_command(text: str) -> str | None:
    """
    Parse user command from message text.

    Returns:
        "new" for /new, "stop" for /stop, "help" for /help,
        "unknown" for other /commands, None for regular messages.
    """
    if not text.startswith("/"):
        return None

    # Extract command name (first word after /)
    cmd = text.split()[0][1:].lower() if text.split() else ""
    # Map known commands; anything else is "unknown"
    known = {"new": "new", "stop": "stop", "help": "help"}
    return known.get(cmd, "unknown")


class CommandExecutor:
    """
    Executes user commands (/new, /stop, /help).

    Dependencies are injected via constructor to keep this module decoupled
    from the AI processing layer — /stop uses a cancel_fn callback rather
    than a direct reference to AiProcessor.
    """

    def __init__(
        self,
        session_manager: SessionManager,
        reply_fn: ReplyFn,
        cancel_fn: CancelFn,
    ) -> None:
        # Session manager for /new command (archive + create session)
        self._session_manager = session_manager
        # Callback to send a text reply back to the QQ message source
        self._reply_fn = reply_fn
        # Callback to cancel an active AI task (bridges to AiProcessor.cancel)
        self._cancel_fn = cancel_fn

    async def execute(
        self, command: str, parsed: ParsedMessage, event: dict
    ) -> None:
        """Execute a parsed command."""
        if command == "new":
            # Archive current session and create a new one
            await self._session_manager.archive_active_session(parsed.chat_id)
            await self._session_manager.create_session(parsed.chat_id)
            await self._reply_fn(event, "已创建新会话，AI 上下文已清空。")
            logger.info("New session created for %s", parsed.chat_id)

        elif command == "stop":
            # Cancel the active AI task for this chat via callback
            if self._cancel_fn(parsed.chat_id):
                await self._reply_fn(event, _MSG_STOPPED)
            else:
                await self._reply_fn(event, _MSG_NO_ACTIVE)

        elif command == "help" or command == "unknown":
            await self._reply_fn(event, HELP_TEXT)
