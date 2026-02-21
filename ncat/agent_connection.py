"""Encapsulates a single ACP connection for one QQ chat.

This module contains `AgentConnection`, which holds all state and components
for a single chat's connection to the ACP agent, including the agent subprocess,
ACP client, content accumulators, and active prompt tracking.
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
        accumulators: Dict mapping session_id to list of ContentPart
        active_prompt: Whether a prompt is currently in flight for this chat
    """

    chat_id: str
    acp_client: NcatAcpClient
    agent_process: AgentProcess
    accumulators: dict[str, list[ContentPart]] = field(default_factory=dict)
    active_prompt: bool = False

    @property
    def is_running(self) -> bool:
        """Check if the agent process for this connection is alive."""
        return self.agent_process.is_running

    @property
    def supports_image(self) -> bool:
        """Whether the connected agent for this connection supports image blocks."""
        return self.agent_process.supports_image
