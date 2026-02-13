"""AI prompt construction — builds context-enriched prompts for OpenCode.

Reads optional system prompt files from disk on every call, allowing
hot-reload without restarting the server.
"""

import logging
from pathlib import Path

from nochan.converter import ParsedMessage

logger = logging.getLogger("nochan.prompt")


class PromptBuilder:
    """
    Builds the full prompt sent to OpenCode, assembling up to three parts:
    1. session_init  — prepended on the first message of a new session only
    2. message_prefix — prepended on every message
    3. context header + user text — always present
    """

    def __init__(
        self,
        prompt_dir: Path,
        session_init_file: str = "session_init.md",
        message_prefix_file: str = "message_prefix.md",
    ) -> None:
        # Resolved directory containing prompt template files
        self._prompt_dir = prompt_dir
        # Full paths to the two prompt files
        self._session_init_path = prompt_dir / session_init_file
        self._message_prefix_path = prompt_dir / message_prefix_file

        # Ensure directory and files exist (create empty ones if missing)
        self._prompt_dir.mkdir(parents=True, exist_ok=True)
        if not self._session_init_path.exists():
            self._session_init_path.touch()
            logger.info("Created empty prompt file: %s", self._session_init_path)
        if not self._message_prefix_path.exists():
            self._message_prefix_path.touch()
            logger.info("Created empty prompt file: %s", self._message_prefix_path)

        logger.info(
            "PromptBuilder initialized: dir=%s, session_init=%s, message_prefix=%s",
            self._prompt_dir,
            session_init_file,
            message_prefix_file,
        )

    def build(self, parsed: ParsedMessage, is_new_session: bool) -> str:
        """
        Build the full prompt for OpenCode.

        Args:
            parsed: The parsed incoming message
            is_new_session: True if this is the first message in the session
                            (triggers inclusion of session_init prompt)
        """
        parts: list[str] = []

        # Session-level system prompt (only for the first message in a session)
        if is_new_session:
            session_init = self._read_prompt(self._session_init_path)
            if session_init:
                parts.append(session_init)
                logger.debug("Included session_init prompt (%d chars)", len(session_init))

        # Per-message prefix prompt
        message_prefix = self._read_prompt(self._message_prefix_path)
        if message_prefix:
            parts.append(message_prefix)
            logger.debug("Included message_prefix prompt (%d chars)", len(message_prefix))

        # Context header + user message (always present)
        parts.append(self._build_header(parsed))

        return "\n\n".join(parts)

    def _build_header(self, parsed: ParsedMessage) -> str:
        """Build the context header with sender/group info and user message."""
        if parsed.message_type == "private":
            header = f"[私聊，用户 {parsed.sender_name}({parsed.sender_id})]"
        else:
            header = (
                f"[群聊 {parsed.group_name}({parsed.chat_id.split(':')[1]})，"
                f"用户 {parsed.sender_name}({parsed.sender_id})]"
            )
        return f"{header}\n{parsed.text}"

    def _read_prompt(self, path: Path) -> str:
        """Read and return trimmed content of a prompt file. Returns '' if empty or missing."""
        try:
            return path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""
