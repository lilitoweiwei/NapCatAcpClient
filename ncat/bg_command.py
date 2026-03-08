"""Background session commands registered in unified command system.

All /bg * commands are registered in the global command_registry with
regex patterns and handler functions.
"""

import logging

import httpx

from ncat.bsp_client import BspClient
from ncat.command import command_registry

logger = logging.getLogger("ncat.bg_command")

# Add section header for background commands
_background_header_added = False


def _ensure_background_header():
    """Add background commands section header to help text."""
    global _background_header_added
    if not _background_header_added:
        # This is a hack - we need to modify the registry's header
        # A better approach would be to support sections in CommandRegistry
        _background_header_added = True


# Helper functions for formatting


def _format_elapsed(seconds: float) -> str:
    """Format elapsed seconds as human-readable string."""
    if seconds < 60:
        return f"{int(seconds)}秒"
    elif seconds < 3600:
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins}分{secs}秒"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}小时{mins}分"


# Register /bg commands


@command_registry.register(
    pattern=r"^/bg new\s+(?P<prompt>.+)$",
    help_text="/bg new <prompt> - 创建后台会话",
    name="bg_new",
)
async def handle_bg_new(
    chat_id: str,
    prompt: str,
    event: dict,
    reply_fn,
    bsp_client: BspClient,
    **kwargs,
) -> None:
    """Handle /bg new <prompt>."""
    try:
        name = await bsp_client.create_session(
            prompt=prompt,
            notify_frontend="ncat",
            notify_chat=chat_id,
        )
        await reply_fn(event, f"后台任务已创建，ID: {name}")
    except httpx.HTTPError as e:
        logger.error("Failed to create session: %s", e)
        await reply_fn(event, f"创建失败：{e}")


@command_registry.register(
    pattern=r"^/bg newn\s+(?P<name>\S+)\s+(?P<prompt>.+)$",
    help_text="/bg newn <name> <prompt> - 创建后台会话（指定名称）",
    name="bg_newn",
)
async def handle_bg_newn(
    chat_id: str,
    name: str,
    prompt: str,
    event: dict,
    reply_fn,
    bsp_client: BspClient,
    **kwargs,
) -> None:
    """Handle /bg newn <name> <prompt>."""
    try:
        final_name = await bsp_client.create_session(
            prompt=prompt,
            notify_frontend="ncat",
            notify_chat=chat_id,
            name=name,
        )
        await reply_fn(event, f"后台任务已创建，ID: {final_name}")
    except httpx.HTTPError as e:
        logger.error("Failed to create session with name: %s", e)
        await reply_fn(event, f"创建失败：{e}")


@command_registry.register(
    pattern=r"^/bg ls$",
    help_text="/bg ls - 列出所有后台会话",
    name="bg_ls",
)
async def handle_bg_ls(
    chat_id: str,
    event: dict,
    reply_fn,
    bsp_client: BspClient,
    **kwargs,
) -> None:
    """Handle /bg ls."""
    try:
        sessions = await bsp_client.list_sessions()
        if not sessions:
            await reply_fn(event, "没有后台任务")
            return

        lines = ["后台会话列表："]
        for i, s in enumerate(sessions, 1):
            status_icon = "🟢" if s["status"] == "running" else "🟡"
            prompt_preview = (
                s["initial_prompt"][:40] + "..."
                if len(s["initial_prompt"]) > 40
                else s["initial_prompt"]
            )
            elapsed = _format_elapsed(s["elapsed_seconds"])
            lines.append(
                f'{i}. {status_icon} [{s["status"]}] {s["name"]}  "{prompt_preview}"  {elapsed}'
            )
        await reply_fn(event, "\n".join(lines))
    except httpx.HTTPError as e:
        logger.error("Failed to list sessions: %s", e)
        await reply_fn(event, f"获取列表失败：{e}")


@command_registry.register(
    pattern=r"^/bg to i\s+(?P<index>\d+)\s+(?P<prompt>.+)$",
    help_text="/bg to i <index> <prompt> - 向指定编号的会话发送 prompt",
    name="bg_to_index",
)
async def handle_bg_to_index(
    chat_id: str,
    index: str,
    prompt: str,
    event: dict,
    reply_fn,
    bsp_client: BspClient,
    **kwargs,
) -> None:
    """Handle /bg to i <index> <prompt>."""
    try:
        index_int = int(index)
        sessions = await bsp_client.list_sessions()
        if index_int < 1 or index_int > len(sessions):
            await reply_fn(
                event, f"无效的编号：{index_int}（共 {len(sessions)} 个会话）"
            )
            return
        name = sessions[index_int - 1]["name"]
        await bsp_client.send_prompt(name, prompt)
        await reply_fn(event, f"已向 {name} 发送 prompt")
    except ValueError:
        await reply_fn(event, "无效的编号")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            await reply_fn(event, "该会话正在运行中，无法发送 prompt")
        elif e.response.status_code == 404:
            await reply_fn(event, "会话不存在")
        else:
            await reply_fn(event, f"发送失败：{e}")
    except httpx.HTTPError as e:
        logger.error("Failed to send prompt: %s", e)
        await reply_fn(event, f"发送失败：{e}")


@command_registry.register(
    pattern=r"^/bg to n\s+(?P<name>\S+)\s+(?P<prompt>.+)$",
    help_text="/bg to n <name> <prompt> - 向指定名称的会话发送 prompt",
    name="bg_to_name",
)
async def handle_bg_to_name(
    chat_id: str,
    name: str,
    prompt: str,
    event: dict,
    reply_fn,
    bsp_client: BspClient,
    **kwargs,
) -> None:
    """Handle /bg to n <name> <prompt>."""
    try:
        await bsp_client.send_prompt(name, prompt)
        await reply_fn(event, f"已向 {name} 发送 prompt")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            await reply_fn(event, "该会话正在运行中，无法发送 prompt")
        elif e.response.status_code == 404:
            await reply_fn(event, "会话不存在")
        else:
            await reply_fn(event, f"发送失败：{e}")
    except httpx.HTTPError as e:
        logger.error("Failed to send prompt: %s", e)
        await reply_fn(event, f"发送失败：{e}")


@command_registry.register(
    pattern=r"^/bg stop i\s+(?P<index>\d+)$",
    help_text="/bg stop i <index> - 停止指定编号的会话",
    name="bg_stop_index",
)
async def handle_bg_stop_index(
    chat_id: str,
    index: str,
    event: dict,
    reply_fn,
    bsp_client: BspClient,
    **kwargs,
) -> None:
    """Handle /bg stop i <index>."""
    try:
        index_int = int(index)
        sessions = await bsp_client.list_sessions()
        if index_int < 1 or index_int > len(sessions):
            await reply_fn(event, f"无效的编号：{index_int}")
            return
        name = sessions[index_int - 1]["name"]
        await bsp_client.delete_session(name)
        await reply_fn(event, f"已停止会话：{name}")
    except ValueError:
        await reply_fn(event, "无效的编号")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            await reply_fn(event, "会话不存在")
        else:
            await reply_fn(event, f"停止失败：{e}")
    except httpx.HTTPError as e:
        logger.error("Failed to stop session: %s", e)
        await reply_fn(event, f"停止失败：{e}")


@command_registry.register(
    pattern=r"^/bg stop n\s+(?P<name>\S+)$",
    help_text="/bg stop n <name> - 停止指定名称的会话",
    name="bg_stop_name",
)
async def handle_bg_stop_name(
    chat_id: str,
    name: str,
    event: dict,
    reply_fn,
    bsp_client: BspClient,
    **kwargs,
) -> None:
    """Handle /bg stop n <name>."""
    try:
        await bsp_client.delete_session(name)
        await reply_fn(event, f"已停止会话：{name}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            await reply_fn(event, "会话不存在")
        else:
            await reply_fn(event, f"停止失败：{e}")
    except httpx.HTTPError as e:
        logger.error("Failed to stop session: %s", e)
        await reply_fn(event, f"停止失败：{e}")


@command_registry.register(
    pattern=r"^/bg stop wait$",
    help_text="/bg stop wait - 停止所有等待中的会话",
    name="bg_stop_wait",
)
async def handle_bg_stop_wait(
    chat_id: str,
    event: dict,
    reply_fn,
    bsp_client: BspClient,
    **kwargs,
) -> None:
    """Handle /bg stop wait."""
    try:
        sessions = await bsp_client.list_sessions()
        waiting_sessions = [s for s in sessions if s["status"] == "waiting"]

        if not waiting_sessions:
            await reply_fn(event, "没有等待中的会话")
            return

        stopped = []
        for s in waiting_sessions:
            await bsp_client.delete_session(s["name"])
            stopped.append(s["name"])

        await reply_fn(
            event, f"已停止 {len(stopped)} 个等待中的会话：{', '.join(stopped)}"
        )
    except httpx.HTTPError as e:
        logger.error("Failed to stop waiting sessions: %s", e)
        await reply_fn(event, f"停止失败：{e}")


@command_registry.register(
    pattern=r"^/bg stop all$",
    help_text="/bg stop all - 停止所有会话",
    name="bg_stop_all",
)
async def handle_bg_stop_all(
    chat_id: str,
    event: dict,
    reply_fn,
    bsp_client: BspClient,
    **kwargs,
) -> None:
    """Handle /bg stop all."""
    try:
        sessions = await bsp_client.list_sessions()

        if not sessions:
            await reply_fn(event, "没有后台会话")
            return

        stopped = []
        for s in sessions:
            await bsp_client.delete_session(s["name"])
            stopped.append(s["name"])

        await reply_fn(event, f"已停止所有 {len(stopped)} 个会话：{', '.join(stopped)}")
    except httpx.HTTPError as e:
        logger.error("Failed to stop all sessions: %s", e)
        await reply_fn(event, f"停止失败：{e}")


@command_registry.register(
    pattern=r"^/bg history i\s+(?P<index>\d+)$",
    help_text="/bg history i <index> - 查看指定编号会话的历史",
    name="bg_history_index",
)
async def handle_bg_history_index(
    chat_id: str,
    index: str,
    event: dict,
    reply_fn,
    bsp_client: BspClient,
    **kwargs,
) -> None:
    """Handle /bg history i <index>."""
    try:
        index_int = int(index)
        sessions = await bsp_client.list_sessions()
        if index_int < 1 or index_int > len(sessions):
            await reply_fn(event, f"无效的编号：{index_int}")
            return
        name = sessions[index_int - 1]["name"]

        messages = await bsp_client.get_history(name)
        if not messages:
            await reply_fn(event, f"{name} 没有历史记录")
            return

        lines = [f"{name} 的会话历史："]
        total_chars = 0
        max_chars = 1500

        for msg in messages:
            line = f"[{msg['role']}] {msg['content'][:100]}"
            if total_chars + len(line) > max_chars:
                lines.append("...（历史过长，已截断）")
                break
            lines.append(line)
            total_chars += len(line)

        await reply_fn(event, "\n".join(lines))
    except ValueError:
        await reply_fn(event, "无效的编号")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            await reply_fn(event, "会话不存在")
        else:
            await reply_fn(event, f"获取历史失败：{e}")
    except httpx.HTTPError as e:
        logger.error("Failed to get history: %s", e)
        await reply_fn(event, f"获取历史失败：{e}")


@command_registry.register(
    pattern=r"^/bg history n\s+(?P<name>\S+)$",
    help_text="/bg history n <name> - 查看指定名称会话的历史",
    name="bg_history_name",
)
async def handle_bg_history_name(
    chat_id: str,
    name: str,
    event: dict,
    reply_fn,
    bsp_client: BspClient,
    **kwargs,
) -> None:
    """Handle /bg history n <name>."""
    try:
        messages = await bsp_client.get_history(name)
        if not messages:
            await reply_fn(event, f"{name} 没有历史记录")
            return

        lines = [f"{name} 的会话历史："]
        total_chars = 0
        max_chars = 1500

        for msg in messages:
            line = f"[{msg['role']}] {msg['content'][:100]}"
            if total_chars + len(line) > max_chars:
                lines.append("...（历史过长，已截断）")
                break
            lines.append(line)
            total_chars += len(line)

        await reply_fn(event, "\n".join(lines))
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            await reply_fn(event, "会话不存在")
        else:
            await reply_fn(event, f"获取历史失败：{e}")
    except httpx.HTTPError as e:
        logger.error("Failed to get history: %s", e)
        await reply_fn(event, f"获取历史失败：{e}")


@command_registry.register(
    pattern=r"^/bg last i\s+(?P<index>\d+)$",
    help_text="/bg last i <index> - 查看指定编号会话的最后一条输出",
    name="bg_last_index",
)
async def handle_bg_last_index(
    chat_id: str,
    index: str,
    event: dict,
    reply_fn,
    bsp_client: BspClient,
    **kwargs,
) -> None:
    """Handle /bg last i <index>."""
    try:
        index_int = int(index)
        sessions = await bsp_client.list_sessions()
        if index_int < 1 or index_int > len(sessions):
            await reply_fn(event, f"无效的编号：{index_int}")
            return
        name = sessions[index_int - 1]["name"]

        last_msg = await bsp_client.get_last(name)
        if not last_msg:
            await reply_fn(event, f"{name} 尚无 agent 输出")
            return

        content = last_msg["content"]
        if len(content) > 500:
            content = content[:500] + "..."

        await reply_fn(event, f"{name} 最后一条输出：\n{content}")
    except ValueError:
        await reply_fn(event, "无效的编号")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            await reply_fn(event, "会话不存在")
        else:
            await reply_fn(event, f"获取失败：{e}")
    except httpx.HTTPError as e:
        logger.error("Failed to get last message: %s", e)
        await reply_fn(event, f"获取失败：{e}")


@command_registry.register(
    pattern=r"^/bg last n\s+(?P<name>\S+)$",
    help_text="/bg last n <name> - 查看指定名称会话的最后一条输出",
    name="bg_last_name",
)
async def handle_bg_last_name(
    chat_id: str,
    name: str,
    event: dict,
    reply_fn,
    bsp_client: BspClient,
    **kwargs,
) -> None:
    """Handle /bg last n <name>."""
    try:
        last_msg = await bsp_client.get_last(name)
        if not last_msg:
            await reply_fn(event, f"{name} 尚无 agent 输出")
            return

        content = last_msg["content"]
        if len(content) > 500:
            content = content[:500] + "..."

        await reply_fn(event, f"{name} 最后一条输出：\n{content}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            await reply_fn(event, "会话不存在")
        else:
            await reply_fn(event, f"获取失败：{e}")
    except httpx.HTTPError as e:
        logger.error("Failed to get last message: %s", e)
        await reply_fn(event, f"获取失败：{e}")
