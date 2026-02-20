"""ncat entry point - starts the ACP agent and WebSocket server."""

import asyncio
import logging
from pathlib import Path

from ncat.agent_manager import AgentManager
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

    # Initialize agent manager (no connection at startup; connect on first user message)
    agent_manager = AgentManager(
        command=config.agent.command,
        args=config.agent.args,
        cwd=str(cwd),
        env=config.agent.env or None,
        mcp_servers=config.mcp,
        initialize_timeout_seconds=config.agent.initialize_timeout_seconds,
        retry_interval_seconds=config.agent.retry_interval_seconds,
    )
    logger.info("WebSocket server starting; agent will connect on first user message")

    # Start WebSocket server for NapCatQQ
    server = NcatNapCatServer(
        host=config.server.host,
        port=config.server.port,
        agent_manager=agent_manager,
        thinking_notify_seconds=config.ux.thinking_notify_seconds,
        thinking_long_notify_seconds=config.ux.thinking_long_notify_seconds,
        permission_timeout=config.ux.permission_timeout,
        permission_raw_input_max_len=config.ux.permission_raw_input_max_len,
        image_download_timeout=config.ux.image_download_timeout,
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
