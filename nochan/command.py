"""User command parsing — handles /new, /stop, /help, and unknown commands."""

# Help text template shown for /help and unknown commands
HELP_TEXT = (
    "nochan 指令列表：\n"
    "/new  - 创建新会话（清空 AI 上下文）\n"
    "/stop - 中断当前 AI 思考\n"
    "/help - 显示本帮助信息\n"
    "直接发送文字即可与 AI 对话。"
)


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
