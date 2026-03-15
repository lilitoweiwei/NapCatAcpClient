"""ncat entry point - starts the ACP agent and WebSocket server."""

import asyncio
import logging
from pathlib import Path

from ncat.agent_manager import AgentManager
from ncat.bsp_client import BspClient
from ncat.config import get_config_path, load_config
from ncat.log import info_event, setup_logging
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
    info_event(logger, "service_start", "ncat starting up", config_path=config_path)

    # Ensure workspace directories exist
    workspace_root = Path(config.agent.workspace_root).expanduser().resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    default_workspace = workspace_root / config.agent.default_workspace
    default_workspace.mkdir(parents=True, exist_ok=True)
    info_event(
        logger,
        "workspace_ready",
        "workspace directories ready",
        workspace_root=str(workspace_root),
        default_workspace=str(default_workspace),
    )

    # Initialize BSP client for background session management
    bsp_client = None
    if config.bsp_server.enabled:
        base_url = f"http://{config.bsp_server.host}:{config.bsp_server.port}"
        bsp_client = BspClient(base_url)
        info_event(logger, "bsp_client_ready", "BSP client initialized", base_url=base_url)

    # Initialize MQTT subscriber for session notifications
    mqtt_subscriber = None
    if config.mqtt.enabled and bsp_client:
        mqtt_subscriber = MqttSubscriber(
            host=config.mqtt.host,
            port=config.mqtt.port,
            topic_prefix=config.mqtt.topic_prefix,
            client_id=config.mqtt.client_id,
        )
        info_event(
            logger,
            "mqtt_configured",
            "MQTT subscriber configured",
            host=config.mqtt.host,
            port=config.mqtt.port,
            client_id=config.mqtt.client_id,
        )

    # Initialize agent manager (no connection at startup; connect on first user message)
    agent_manager = AgentManager(
        command=config.agent.command,
        args=config.agent.args,
        workspace_root=str(workspace_root),
        default_workspace=config.agent.default_workspace,
        env=config.agent.env or None,
        log_extra_context_env_var=config.agent.log_extra_context_env_var,
        mcp_servers=config.mcp,
        initialize_timeout_seconds=config.agent.initialize_timeout_seconds,
        retry_interval_seconds=config.agent.retry_interval_seconds,
    )
    info_event(
        logger,
        "server_start",
        "WebSocket server starting; agent will connect on first user message",
        host=config.server.host,
        port=config.server.port,
    )

    # Start WebSocket server for NapCatQQ
    server = NcatNapCatServer(
        host=config.server.host,
        port=config.server.port,
        agent_manager=agent_manager,
        thinking_notify_seconds=config.ux.thinking_notify_seconds,
        thinking_long_notify_seconds=config.ux.thinking_long_notify_seconds,
        max_reply_text_length=config.ux.max_reply_text_length,
        reply_split_start_length=config.ux.reply_split_start_length,
        image_download_timeout=config.ux.image_download_timeout,
        max_inline_image_mb=config.ux.max_inline_image_mb,
        file_ingress_enabled=config.file_ingress.enabled,
        file_inbox_dirname=config.file_ingress.inbox_dirname,
        file_download_timeout=config.file_ingress.download_timeout,
        pending_ttl_seconds=config.file_ingress.pending_ttl_seconds,
        max_file_size_mb=config.file_ingress.max_file_size_mb,
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
        info_event(logger, "service_stop", "ncat shut down")


if __name__ == "__main__":
    import contextlib

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
