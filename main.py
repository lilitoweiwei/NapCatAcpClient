"""ncat entry point - starts the ACP agent and WebSocket server."""

import asyncio
import logging
from pathlib import Path

from ncat.acp_client import AgentManager
from ncat.config import get_config_path, load_config
from ncat.log import setup_logging
from ncat.napcat_server import NcatNapCatServer

logger = logging.getLogger("ncat.main")


async def main() -> None:
    """Initialize the ACP agent and start the WebSocket server."""
    # Load configuration
    config_path = get_config_path()
    config = load_config(config_path)

    # Initialize logging
    setup_logging(config.logging)
    logger.info("ncat starting up (config: %s)", config_path)

    # Ensure agent working directory exists
    cwd = Path(config.agent.cwd).expanduser().resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    logger.info("Agent working directory: %s", cwd)

    # Initialize and start the ACP agent manager
    agent_manager = AgentManager(
        command=config.agent.command,
        args=config.agent.args,
        cwd=str(cwd),
    )
    await agent_manager.start()
    logger.info("ACP agent started")

    # Start WebSocket server for NapCatQQ
    server = NcatNapCatServer(
        host=config.server.host,
        port=config.server.port,
        agent_manager=agent_manager,
        thinking_notify_seconds=config.ux.thinking_notify_seconds,
        thinking_long_notify_seconds=config.ux.thinking_long_notify_seconds,
    )

    try:
        await server.start()
    finally:
        await agent_manager.stop()
        logger.info("ncat shut down.")


if __name__ == "__main__":
    import contextlib

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
