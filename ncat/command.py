"""User command handling with unified command system.

All commands (/new, /stop, /bg *, etc.) are registered in a central registry
with regex patterns and handler functions. Help text is automatically generated.
"""

import logging

from ncat.agent_manager import AgentManager
from ncat.command_system import CommandRegistry
from ncat.log import info_event

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
_MSG_AGENT_RESET = "提示：/agent 只作用于当前 session；发送 /new 后会恢复为 OpenCode 默认 agent。"


def _format_status(agent_manager: AgentManager, chat_id: str) -> str:
    status = agent_manager.get_chat_status(chat_id)
    lines = [
        f"工作区: {status.workspace_name}",
        f"连接: {'已连接' if status.connected else '未连接'}",
        f"会话: {'已创建' if status.has_session else '未创建'}",
        f"Agent: {status.current_mode_id or '未知（首次创建会话后可见）'}",
        f"图片输入: {('支持' if status.supports_image else '不支持') if status.supports_image is not None else '未知'}",
    ]

    if status.usage is None:
        lines.append("上下文: 暂无")
    else:
        percent = 0.0
        if status.usage.size > 0:
            percent = status.usage.used / status.usage.size * 100
        lines.append(
            f"上下文: {status.usage.used} / {status.usage.size} ({percent:.1f}%)"
        )
        if status.usage.cost_amount is not None and status.usage.cost_currency:
            lines.append(
                f"累计成本: {status.usage.cost_amount:.4f} {status.usage.cost_currency}"
            )
    return "\n".join(lines)


def _format_agent_listing(agent_manager: AgentManager, chat_id: str) -> str:
    status = agent_manager.get_chat_status(chat_id)
    lines = [f"当前 Agent: {status.current_mode_id or '未知（首次创建会话后可见）'}"]

    if not status.available_modes:
        lines.append("可用 Agents: 暂无（首次创建会话后可见）")
        return "\n".join(lines)

    lines.append("可用 Agents:")
    for mode in status.available_modes:
        suffix = f" - {mode.description}" if mode.description else ""
        lines.append(f"- {mode.id}{suffix}")
    return "\n".join(lines)


@command_registry.register(
    pattern=r"^/new(?:\s+(?P<dir>\S+))?$",
    help_text="/new [workspace] - 创建新会话并切换到指定工作区（会恢复默认 agent）",
    name="new",
)
async def handle_new(
    chat_id: str,
    dir: str | None,
    event: dict,
    reply_fn,
    agent_manager: AgentManager,
    cancel_fn=None,
    pending_input_store=None,
    **kwargs,
) -> None:
    """Handle /new and /new <workspace> commands.

    Args:
        chat_id: QQ chat ID
        dir: Optional workspace name
        event: Raw event dict
        reply_fn: Callback to send reply
        agent_manager: Agent manager instance
    """
    try:
        agent_manager.set_next_session_cwd(chat_id, dir)
    except ValueError as exc:
        await reply_fn(event, f"工作区无效：{exc}")
        return

    if cancel_fn is not None:
        cancel_fn(chat_id)

    if pending_input_store is not None:
        pending_input_store.clear(chat_id)

    # Close current ACP session and disconnect
    await agent_manager.close_session(chat_id)
    await agent_manager.disconnect(chat_id)
    await reply_fn(event, _MSG_NEW_SESSION)
    info_event(
        logger,
        "command_new",
        "New session requested",
        chat_id=chat_id,
        workspace=dir,
    )


@command_registry.register(
    pattern=r"^/agent(?:\s+(?P<name>\S+))?$",
    help_text="/agent [name] - 查看或切换当前 session 的 agent（/new 后恢复默认）",
    name="agent",
)
async def handle_agent(
    chat_id: str,
    name: str | None,
    event: dict,
    reply_fn,
    agent_manager: AgentManager,
    busy_fn=None,
    **kwargs,
) -> None:
    """Handle /agent and /agent <name> commands."""
    if not name:
        await reply_fn(event, _format_agent_listing(agent_manager, chat_id) + "\n\n" + _MSG_AGENT_RESET)
        return

    if callable(busy_fn) and busy_fn(chat_id):
        await reply_fn(event, "当前 AI 正在思考中，请等待完成或先发送 /stop，再切换 agent。")
        return

    try:
        await agent_manager.set_session_mode(chat_id, name)
    except ValueError as exc:
        await reply_fn(event, str(exc) + "\n\n" + _format_agent_listing(agent_manager, chat_id))
        return
    await reply_fn(event, f"已切换到 agent：{name}\n{_MSG_AGENT_RESET}")
    info_event(
        logger,
        "command_agent",
        "Session agent switched",
        chat_id=chat_id,
        agent=name,
    )


@command_registry.register(
    pattern=r"^/status$",
    help_text="/status - 查看当前工作区、session、agent 与 usage 状态",
    name="status",
)
async def handle_status(
    chat_id: str,
    event: dict,
    reply_fn,
    agent_manager: AgentManager,
    **kwargs,
) -> None:
    """Handle /status command."""
    await reply_fn(event, _format_status(agent_manager, chat_id))


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
    **kwargs,
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
        await reply_fn(event, get_help_text())
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
