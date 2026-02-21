"""ncat entry point - starts the ACP agent and WebSocket server."""

import asyncio
import logging
from pathlib import Path

from ncat.agent_manager import AgentManager
from ncat.bsp_client import BspClient
from ncat.config import get_config_path, load_config
from ncat.log import setup_logging
from ncat.mqtt_subscriber import MqttSubscriber
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

    # Initialize BSP client for background session management
    bsp_client = None
    if config.bsp_server.enabled:
        base_url = f"http://{config.bsp_server.host}:{config.bsp_server.port}"
        bsp_client = BspClient(base_url)
        logger.info("BSP client initialized: %s", base_url)

    # Initialize MQTT subscriber for session notifications
    mqtt_subscriber = None
    if config.mqtt.enabled and bsp_client:
        mqtt_subscriber = MqttSubscriber(
            host=config.mqtt.host,
            port=config.mqtt.port,
            topic_prefix=config.mqtt.topic_prefix,
            client_id=config.mqtt.client_id,
        )
        logger.info(
            "MQTT subscriber configured: %s:%d",
            config.mqtt.host,
            config.mqtt.port,
        )

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
        image_download_timeout=config.ux.image_download_timeout,
        bsp_client=bsp_client,
    )

    try:
        # Start MQTT subscriber if enabled
        if mqtt_subscriber:
            # Set reply function for MQTT notifications
            mqtt_subscriber.set_reply_fn(server.send_qq_reply)
            await mqtt_subscriber.start()

        await server.start()
    finally:
        # Cleanup resources
        if mqtt_subscriber:
            await mqtt_subscriber.stop()
        if bsp_client:
            await bsp_client.close()
        await agent_manager.stop()
        logger.info("ncat shut down.")


if __name__ == "__main__":
    import contextlib

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
