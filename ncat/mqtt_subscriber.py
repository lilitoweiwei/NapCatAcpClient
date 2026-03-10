"""MQTT subscriber for receiving session notifications from BSP server."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import aiomqtt

from ncat.log import debug_event, error_event, info_event, warning_event

logger = logging.getLogger("ncat.mqtt_subscriber")

# Type alias for the reply callback
# Signature: async fn(chat_id: str, text: str) -> None
ReplyFn = Callable[[str, str], Awaitable[None]]


class MqttSubscriber:
    """Subscribes to MQTT notifications from BSP server.

    Routes notifications to the appropriate QQ chat.
    """

    def __init__(
        self,
        host: str,
        port: int,
        topic_prefix: str,
        client_id: str,
    ):
        """Initialize MQTT subscriber.

        Args:
            host: MQTT broker hostname
            port: MQTT broker port
            topic_prefix: Topic prefix (e.g., "suzu")
            client_id: MQTT client identifier
        """
        self.host = host
        self.port = port
        self.topic_prefix = topic_prefix
        self.client_id = client_id
        self._client: aiomqtt.Client | None = None
        self._reply_fn: ReplyFn | None = None
        self._running = False
        self._listen_task: asyncio.Task[None] | None = None

    def set_reply_fn(self, fn: ReplyFn) -> None:
        """Set the callback to send QQ replies.

        Args:
            fn: Async callback with signature fn(chat_id: str, text: str)
        """
        self._reply_fn = fn

    async def start(self) -> None:
        """Connect and subscribe to MQTT notifications."""
        try:
            # Connect using async context manager
            self._client = aiomqtt.Client(
                hostname=self.host,
                port=self.port,
                identifier=self.client_id,
            )
            await self._client.__aenter__()

            # Subscribe to ncat notifications
            topic = f"{self.topic_prefix}/system/ncat/#"
            await self._client.subscribe(topic)
            info_event(
                logger,
                "mqtt_subscribed",
                "Subscribed to MQTT topic",
                topic=topic,
                host=self.host,
                port=self.port,
                client_id=self.client_id,
            )

            # Start listening loop
            self._running = True
            # Store task reference to prevent GC from cancelling it
            self._listen_task = asyncio.create_task(self._listen_loop())
        except Exception as e:
            error_event(
                logger,
                "mqtt_connect_fail",
                "Failed to connect to MQTT broker",
                host=self.host,
                port=self.port,
                err=str(e),
            )
            raise

    async def stop(self) -> None:
        """Disconnect from MQTT broker."""
        self._running = False
        if self._client:
            await self._client.__aexit__(None, None, None)
            self._client = None
            info_event(logger, "mqtt_disconnect", "Disconnected from MQTT broker")

    async def _listen_loop(self) -> None:
        """Listen for MQTT messages."""
        if not self._client:
            return
        try:
            async for message in self._client.messages:
                if not self._running:
                    break

                topic = message.topic.value
                payload = json.loads(message.payload.decode("utf-8"))
                await self._handle_message(topic, payload)
        except Exception as e:
            error_event(logger, "mqtt_listen_fail", "Error in MQTT listen loop", err=str(e))

    async def _handle_message(self, topic: str, payload: dict) -> None:
        """Route MQTT notification to QQ chat.

        Args:
            topic: MQTT topic string
            payload: Parsed JSON payload
        """
        # Parse topic: {prefix}/system/{frontend}/{chat_id} (e.g. suzu/system/ncat/private:123)
        parts = topic.split("/")
        if len(parts) < 4:
            warning_event(
                logger,
                "mqtt_topic_invalid",
                "Invalid MQTT topic format",
                topic=topic,
            )
            return

        chat_id = parts[3]
        msg_type = payload.get("type")

        if msg_type == "bg_created":
            text = f"后台任务已创建，ID: {payload.get('name', 'unknown')}"
            info_event(
                logger,
                "mqtt_bg_created",
                "Received bg_created notification",
                chat_id=chat_id,
                bg_name=payload.get("name"),
            )
        elif msg_type == "bg_waiting":
            last_msg = payload.get("last_message", "")
            if last_msg and len(last_msg) > 200:
                last_msg = last_msg[:200] + "..."
            text = f"后台任务 {payload.get('name', 'unknown')} 已完成等待输入"
            if last_msg:
                text += f"，最后输出：{last_msg}"
            info_event(
                logger,
                "mqtt_bg_waiting",
                "Received bg_waiting notification",
                chat_id=chat_id,
                bg_name=payload.get("name"),
            )
        else:
            debug_event(
                logger,
                "mqtt_ignored",
                "Ignoring unknown MQTT message type",
                chat_id=chat_id,
                msg_type=msg_type,
            )
            return

        # Send QQ reply
        if self._reply_fn:
            await self._reply_fn(chat_id, text)
        else:
            warning_event(
                logger,
                "mqtt_reply_missing",
                "No reply function set for MQTT notification",
                chat_id=chat_id,
            )
