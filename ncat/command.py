"""User command parsing and execution — handles /new, /stop, /help, etc.

parse_command() is a pure function for identifying commands from message text.
CommandExecutor.try_handle() uses it internally — callers only need to know
whether the message was handled as a command (True) or not (False).
"""

import logging
from collections.abc import Awaitable, Callable

from ncat.agent_manager import AgentManager, MSG_AGENT_NOT_CONNECTED
from ncat.models import ParsedMessage

logger = logging.getLogger("ncat.command")

# Type alias for the reply callback provided by the transport layer.
# Signature: async reply_fn(event: dict, text: str) -> None
ReplyFn = Callable[[dict, str], Awaitable[None]]

# Type alias for the cancel callback provided by PromptRunner.
# Signature: cancel_fn(chat_id: str) -> bool (True if cancelled, False if no active task)
CancelFn = Callable[[str], bool]

# Help text template shown for /help and unknown commands
HELP_TEXT = (
    "ncat 指令列表：\n"
    "/new  - 创建新会话（清空 AI 上下文）\n"
    "/stop - 中断当前 AI 思考\n"
    "/send <text> - 将文本原样转发给 agent（不触发 ncat 指令）\n"
    "/help - 显示本帮助信息\n"
    "直接发送文字即可与 AI 对话。"
)

# --- Command response messages ---
_MSG_NEW_SESSION = "已创建新会话，AI 上下文已清空。"
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
    Parses and executes user commands (/new, /stop, /help).

    The sole public entry point is try_handle(), which checks whether the
    message is a command and executes it if so. Callers only see a bool.
    Dependencies are injected via constructor to keep this module decoupled
    from the AI processing layer — /stop uses a cancel_fn callback rather
    than a direct reference to PromptRunner.
    """

    def __init__(
        self,
        agent_manager: AgentManager,
        reply_fn: ReplyFn,
        cancel_fn: CancelFn,
    ) -> None:
        # Agent manager for /new command (close + recreate session)
        self._agent_manager = agent_manager
        # Callback to send a text reply back to the QQ message source
        self._reply_fn = reply_fn
        # Callback to cancel an active AI task (bridges to PromptRunner.cancel)
        self._cancel_fn = cancel_fn

    async def try_handle(self, parsed: ParsedMessage, event: dict) -> bool:
        """
        Try to handle the message as a command.

        Returns True if the message was a command (handled), False otherwise.
        """
        command = parse_command(parsed.text)
        if command is None:
            return False

        logger.info("Command received: /%s from %s", command, parsed.chat_id)
        await self._execute(command, parsed, event)
        return True

    async def _execute(self, command: str, parsed: ParsedMessage, event: dict) -> None:
        """Execute a parsed command (internal dispatch)."""
        if command == "new":
            if not self._agent_manager.is_running:
                await self._reply_fn(event, MSG_AGENT_NOT_CONNECTED)
                return
            # Close current ACP session; a new one will be created on next message
            await self._agent_manager.close_session(parsed.chat_id)
            await self._reply_fn(event, _MSG_NEW_SESSION)
            logger.info("New session will be created for %s on next message", parsed.chat_id)

        elif command == "stop":
            # Cancel the active AI task for this chat via callback
            if self._cancel_fn(parsed.chat_id):
                await self._reply_fn(event, _MSG_STOPPED)
            else:
                await self._reply_fn(event, _MSG_NO_ACTIVE)

        elif command == "help" or command == "unknown":
            await self._reply_fn(event, HELP_TEXT)
