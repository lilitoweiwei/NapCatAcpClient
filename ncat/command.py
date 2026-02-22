"""User command handling with unified command system.

All commands (/new, /stop, /bg *, etc.) are registered in a central registry
with regex patterns and handler functions. Help text is automatically generated.
"""

import logging

from ncat.agent_manager import AgentManager
from ncat.command_system import CommandRegistry

logger = logging.getLogger("ncat.command")

# Create global command registry
command_registry = CommandRegistry(
    header_text="ncat 指令列表：\n\n基础指令："
)

# --- Command response messages ---
_MSG_NEW_SESSION = "已创建新会话，AI 上下文已清空。"
_MSG_STOPPED = "已中断当前 AI 思考。"
_MSG_NO_ACTIVE = "当前没有进行中的 AI 思考。"
_MSG_HELP = "直接发送文字即可与 AI 对话。"


@command_registry.register(
    pattern=r"^/new(?:\s+(?P<dir>\S+))?$",
    help_text="/new [dir] - 创建新会话（工作目录由 Agent 网关默认配置决定）",
    name="new",
)
async def handle_new(
    chat_id: str,
    dir: str | None,
    event: dict,
    reply_fn,
    agent_manager: AgentManager,
) -> None:
    """Handle /new and /new <dir> commands.

    Args:
        chat_id: QQ chat ID
        dir: Optional workspace directory name
        event: Raw event dict
        reply_fn: Callback to send reply
        agent_manager: Agent manager instance
    """
    # Set one-time cwd for next session
    agent_manager.set_next_session_cwd(chat_id, dir)
    # Close current ACP session and disconnect
    await agent_manager.close_session(chat_id)
    await agent_manager.disconnect(chat_id)
    await reply_fn(event, _MSG_NEW_SESSION)
    logger.info(
        "New session will be created for %s on next message (cwd dir=%s)",
        chat_id,
        dir,
    )


@command_registry.register(
    pattern=r"^/stop$",
    help_text="/stop - 中断当前 AI 思考",
    name="stop",
)
async def handle_stop(
    chat_id: str,
    event: dict,
    reply_fn,
    cancel_fn,
) -> None:
    """Handle /stop command.

    Args:
        chat_id: QQ chat ID
        event: Raw event dict
        reply_fn: Callback to send reply
        cancel_fn: Callback to cancel active AI task
    """
    if cancel_fn(chat_id):
        await reply_fn(event, _MSG_STOPPED)
    else:
        await reply_fn(event, _MSG_NO_ACTIVE)


@command_registry.register(
    pattern=r"^/send(?:\s+(?P<body>.*))?$",
    help_text="/send <text> - 将文本原样转发给 agent（不触发 ncat 指令）",
    name="send",
)
async def handle_send(
    body: str | None,
    event: dict,
    reply_fn,
    **kwargs,
) -> None:
    """Handle /send command.

    Args:
        body: Message body to forward
        event: Raw event dict
        reply_fn: Callback to send reply
        **kwargs: Additional dependencies (ignored for this command)
    """
    if not body:
        # /send with no payload - will be handled by dispatcher
        pass
    else:
        await reply_fn(event, body)


@command_registry.register(
    pattern=r"^/help$",
    help_text="/help - 显示本帮助信息",
    name="help",
)
async def handle_help(
    event: dict,
    reply_fn,
    **kwargs,
) -> None:
    """Handle /help command.

    Args:
        event: Raw event dict
        reply_fn: Callback to send reply
        **kwargs: Additional dependencies (ignored for this command)
    """
    help_text = command_registry.generate_help_text() + "\n\n" + _MSG_HELP
    await reply_fn(event, help_text)


# Legacy function for backward compatibility (used by dispatcher)
def get_help_text() -> str:
    """Generate help text from registry.

    Returns:
        Aggregated help text
    """
    return command_registry.generate_help_text() + "\n\n" + _MSG_HELP
