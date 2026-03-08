"""Encapsulates a single ACP connection for one QQ chat.

This module contains `AgentConnection`, which holds all state and components
for a single chat's connection to the ACP agent, including the agent subprocess,
ACP client, the long-lived ACP session, and turn-level accumulation state.
"""

from dataclasses import dataclass, field

from ncat.acp_client import NcatAcpClient
from ncat.agent_process import AgentProcess
from ncat.models import ContentPart


@dataclass
class AgentConnection:
    """Encapsulates a single ACP connection for one QQ chat.

    Attributes:
        chat_id: The QQ chat identifier (e.g., "private:12345")
        acp_client: The NcatAcpClient instance for this connection
        agent_process: The AgentProcess managing the subprocess
        active_session_id: Long-lived ACP session currently bound to this chat
        active_turn_session_id: Session currently handling a prompt turn
        turn_accumulator: Streamed content for the current prompt turn only
        active_prompt: Whether a prompt is currently in flight for this chat
    """

    chat_id: str
    acp_client: NcatAcpClient
    agent_process: AgentProcess
    active_session_id: str | None = None
    active_turn_session_id: str | None = None
    turn_accumulator: list[ContentPart] = field(default_factory=list)
    active_prompt: bool = False
    workspace_cwd: str | None = None

    @property
    def is_running(self) -> bool:
        """Check if the agent process for this connection is alive."""
        return self.agent_process.is_running

    @property
    def supports_image(self) -> bool:
        """Whether the connected agent for this connection supports image blocks."""
        return self.agent_process.supports_image
