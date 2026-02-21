"""Background session command handler for /bg * commands."""

import logging

import httpx

from ncat.bsp_client import BspClient

logger = logging.getLogger("ncat.bg_command")


class BgCommandHandler:
    """Handles /bg * commands.

    Responsibilities:
    - Parse command arguments
    - Call BSP client
    - Format responses for QQ
    - Handle errors and edge cases
    """

    def __init__(self, bsp_client: BspClient):
        """Initialize command handler.

        Args:
            bsp_client: BSP client instance
        """
        self.bsp_client = bsp_client

    async def handle_bg_new(self, chat_id: str, prompt: str) -> str:
        """Handle /bg new <prompt>.

        Args:
            chat_id: QQ chat ID for notifications
            prompt: Initial prompt text

        Returns:
            QQ reply text
        """
        try:
            name = await self.bsp_client.create_session(
                prompt=prompt,
                notify_frontend="ncat",
                notify_chat=chat_id,
            )
            return f"后台任务已创建，ID: {name}"
        except httpx.HTTPError as e:
            logger.error("Failed to create session: %s", e)
            return f"创建失败：{e}"

    async def handle_bg_newn(self, chat_id: str, name: str, prompt: str) -> str:
        """Handle /bg newn <name> <prompt>.

        Args:
            chat_id: QQ chat ID for notifications
            name: Desired session name
            prompt: Initial prompt text

        Returns:
            QQ reply text
        """
        try:
            final_name = await self.bsp_client.create_session(
                prompt=prompt,
                notify_frontend="ncat",
                notify_chat=chat_id,
                name=name,
            )
            return f"后台任务已创建，ID: {final_name}"
        except httpx.HTTPError as e:
            logger.error("Failed to create session with name: %s", e)
            return f"创建失败：{e}"

    async def handle_bg_ls(self, chat_id: str) -> str:
        """Handle /bg ls.

        Args:
            chat_id: QQ chat ID

        Returns:
            Formatted session list
        """
        try:
            sessions = await self.bsp_client.list_sessions()
            if not sessions:
                return "没有后台任务"

            lines = [f"后台会话列表（共 {len(sessions)} 个）："]
            for i, s in enumerate(sessions, 1):
                status_icon = "🟢" if s["status"] == "running" else "🟡"
                prompt_preview = (
                    s["initial_prompt"][:40] + "..."
                    if len(s["initial_prompt"]) > 40
                    else s["initial_prompt"]
                )
                elapsed = self._format_elapsed(s["elapsed_seconds"])
                lines.append(
                    f'{i}. {status_icon} [{s["status"]}] {s["name"]}  "{prompt_preview}"  {elapsed}'
                )
            return "\n".join(lines)
        except httpx.HTTPError as e:
            logger.error("Failed to list sessions: %s", e)
            return f"获取列表失败：{e}"

    def _format_elapsed(self, seconds: float) -> str:
        """Format elapsed seconds as human-readable string.

        Args:
            seconds: Elapsed seconds

        Returns:
            Formatted string (e.g., "5 分 30 秒")
        """
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

    async def handle_bg_to(self, chat_id: str, ref: str, prompt: str, by_index: bool) -> str:
        """Handle /bg to i <index> <prompt> or /bg to n <name> <prompt>.

        Args:
            chat_id: QQ chat ID
            ref: Index (string) or name
            prompt: Prompt text to send
            by_index: If True, ref is index; else ref is name

        Returns:
            QQ reply text
        """
        try:
            if by_index:
                # Resolve index to name
                index = int(ref)
                sessions = await self.bsp_client.list_sessions()
                if index < 1 or index > len(sessions):
                    return f"无效的编号：{index}（共 {len(sessions)} 个会话）"
                name = sessions[index - 1]["name"]
            else:
                name = ref

            # Send prompt
            await self.bsp_client.send_prompt(name, prompt)
            return f"已向 {name} 发送 prompt"
        except ValueError:
            return "无效的编号"
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                return "该会话正在运行中，无法发送 prompt"
            elif e.response.status_code == 404:
                return "会话不存在"
            else:
                return f"发送失败：{e}"
        except httpx.HTTPError as e:
            logger.error("Failed to send prompt: %s", e)
            return f"发送失败：{e}"

    async def handle_bg_stop(self, chat_id: str, ref: str, by_index: bool) -> str:
        """Handle /bg stop i <index> or /bg stop n <name>.

        Args:
            chat_id: QQ chat ID
            ref: Index or name
            by_index: If True, ref is index; else ref is name

        Returns:
            QQ reply text
        """
        try:
            if by_index:
                index = int(ref)
                sessions = await self.bsp_client.list_sessions()
                if index < 1 or index > len(sessions):
                    return f"无效的编号：{index}"
                name = sessions[index - 1]["name"]
            else:
                name = ref

            await self.bsp_client.delete_session(name)
            return f"已停止会话：{name}"
        except ValueError:
            return "无效的编号"
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return "会话不存在"
            else:
                return f"停止失败：{e}"
        except httpx.HTTPError as e:
            logger.error("Failed to stop session: %s", e)
            return f"停止失败：{e}"

    async def handle_bg_stop_wait(self, chat_id: str) -> str:
        """Handle /bg stop wait.

        Stops all waiting sessions.

        Args:
            chat_id: QQ chat ID

        Returns:
            QQ reply text
        """
        try:
            sessions = await self.bsp_client.list_sessions()
            waiting_sessions = [s for s in sessions if s["status"] == "waiting"]

            if not waiting_sessions:
                return "没有等待中的会话"

            # Stop all waiting sessions
            stopped = []
            for s in waiting_sessions:
                await self.bsp_client.delete_session(s["name"])
                stopped.append(s["name"])

            return f"已停止 {len(stopped)} 个等待中的会话：{', '.join(stopped)}"
        except httpx.HTTPError as e:
            logger.error("Failed to stop waiting sessions: %s", e)
            return f"停止失败：{e}"

    async def handle_bg_stop_all(self, chat_id: str) -> str:
        """Handle /bg stop all.

        Stops all sessions.

        Args:
            chat_id: QQ chat ID

        Returns:
            QQ reply text
        """
        try:
            sessions = await self.bsp_client.list_sessions()

            if not sessions:
                return "没有后台会话"

            # Stop all sessions
            stopped = []
            for s in sessions:
                await self.bsp_client.delete_session(s["name"])
                stopped.append(s["name"])

            return f"已停止所有 {len(stopped)} 个会话：{', '.join(stopped)}"
        except httpx.HTTPError as e:
            logger.error("Failed to stop all sessions: %s", e)
            return f"停止失败：{e}"

    async def handle_bg_history(self, chat_id: str, ref: str, by_index: bool) -> str:
        """Handle /bg history i <index> or /bg history n <name>.

        Args:
            chat_id: QQ chat ID
            ref: Index or name
            by_index: If True, ref is index; else ref is name

        Returns:
            Formatted session history
        """
        try:
            if by_index:
                index = int(ref)
                sessions = await self.bsp_client.list_sessions()
                if index < 1 or index > len(sessions):
                    return f"无效的编号：{index}"
                name = sessions[index - 1]["name"]
            else:
                name = ref

            messages = await self.bsp_client.get_history(name)
            if not messages:
                return f"{name} 没有历史记录"

            # Format history (truncate if too long)
            lines = [f"{name} 的会话历史："]
            total_chars = 0
            max_chars = 1500  # QQ message length limit

            for msg in messages:
                line = f"[{msg['role']}] {msg['content'][:100]}"
                if total_chars + len(line) > max_chars:
                    lines.append("...（历史过长，已截断）")
                    break
                lines.append(line)
                total_chars += len(line)

            return "\n".join(lines)
        except ValueError:
            return "无效的编号"
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return "会话不存在"
            else:
                return f"获取历史失败：{e}"
        except httpx.HTTPError as e:
            logger.error("Failed to get history: %s", e)
            return f"获取历史失败：{e}"

    async def handle_bg_last(self, chat_id: str, ref: str, by_index: bool) -> str:
        """Handle /bg last i <index> or /bg last n <name>.

        Args:
            chat_id: QQ chat ID
            ref: Index or name
            by_index: If True, ref is index; else ref is name

        Returns:
            Last agent output
        """
        try:
            if by_index:
                index = int(ref)
                sessions = await self.bsp_client.list_sessions()
                if index < 1 or index > len(sessions):
                    return f"无效的编号：{index}"
                name = sessions[index - 1]["name"]
            else:
                name = ref

            last_msg = await self.bsp_client.get_last(name)
            if not last_msg:
                return f"{name} 尚无 agent 输出"

            # Truncate if too long
            content = last_msg["content"]
            if len(content) > 500:
                content = content[:500] + "..."

            return f"{name} 最后一条输出：\n{content}"
        except ValueError:
            return "无效的编号"
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return "会话不存在"
            else:
                return f"获取失败：{e}"
        except httpx.HTTPError as e:
            logger.error("Failed to get last message: %s", e)
            return f"获取失败：{e}"
