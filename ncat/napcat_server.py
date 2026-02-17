"""NapCat-facing WebSocket server.

Transport layer for communicating with NapCatQQ via OneBot 11.
"""

import asyncio
import json
import logging
import uuid

import websockets
from websockets.asyncio.server import ServerConnection

from ncat.agent_manager import AgentManager
from ncat.converter import ai_to_onebot, content_to_onebot
from ncat.dispatcher import MessageDispatcher
from ncat.models import ContentPart
from ncat.permission import PermissionBroker

logger = logging.getLogger("ncat.napcat_server")


class NcatNapCatServer:
    """
    WebSocket server that handles the NapCatQQ transport layer.

    Responsibilities: connection lifecycle, event dispatching, API call/response
    matching, and sending messages. Business logic is delegated to MessageDispatcher.
    """

    def __init__(
        self,
        host: str,
        port: int,
        agent_manager: AgentManager,
        thinking_notify_seconds: float = 10,
        thinking_long_notify_seconds: float = 30,
        permission_timeout: float = 300,
        permission_raw_input_max_len: int = 500,
        image_download_timeout: float = 15.0,
    ) -> None:
        # WebSocket bind address and port
        self._host = host
        self._port = port
        # ACP agent manager (needed for session cleanup on disconnect)
        self._agent_manager = agent_manager

        # Currently active WebSocket connection from NapCatQQ (only one expected)
        self._connection: ServerConnection | None = None
        # Bot's own QQ ID, extracted from self_id in the first received event
        self._bot_id: int | None = None
        # In-flight API calls awaiting response, keyed by echo ID
        self._pending: dict[str, asyncio.Future[dict]] = {}
        # Background message-handling tasks (prevent GC from cancelling them)
        self._tasks: set[asyncio.Task[None]] = set()

        # Permission broker — forwarding permission requests to QQ users
        permission_broker = PermissionBroker(
            reply_fn=self._reply_text,
            timeout=permission_timeout,
            raw_input_max_len=permission_raw_input_max_len,
        )
        # Wire the broker into the agent manager so NcatAcpClient can use it
        agent_manager.permission_broker = permission_broker

        # Message dispatcher — business logic, decoupled from transport
        self._dispatcher = MessageDispatcher(
            agent_manager=agent_manager,
            reply_fn=self._reply_text,
            reply_content_fn=self._reply_content,
            permission_broker=permission_broker,
            thinking_notify_seconds=thinking_notify_seconds,
            thinking_long_notify_seconds=thinking_long_notify_seconds,
            image_download_timeout=image_download_timeout,
        )

    async def start(self) -> None:
        """Start the WebSocket server and run forever."""
        logger.info("Starting ncat server on ws://%s:%d", self._host, self._port)
        # Use None for host to bind all interfaces (IPv4 + IPv6)
        host = None if self._host == "0.0.0.0" else self._host
        async with websockets.serve(self._handler_ws, host, self._port):
            logger.info("Server ready, waiting for NapCatQQ connection...")
            await asyncio.Future()  # run forever

    # --- WebSocket connection handling ---

    async def _handler_ws(self, websocket: ServerConnection) -> None:
        """Handle a WebSocket connection from NapCatQQ."""
        remote = websocket.remote_address
        logger.info("NapCatQQ connected from %s", remote)
        self._connection = websocket

        try:
            async for raw_message in websocket:
                try:
                    data = json.loads(raw_message)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON message received: %s", str(raw_message)[:200])
                    continue

                # Check if this is an API response (has echo field matching a pending request)
                if "echo" in data and data["echo"] in self._pending:
                    echo = data["echo"]
                    self._pending[echo].set_result(data)
                    del self._pending[echo]
                    continue

                # Dispatch by event type
                await self._dispatch_event(data)

        except websockets.ConnectionClosed as e:
            logger.warning("Connection closed: code=%s reason=%s", e.code, e.reason)
        finally:
            if self._connection is websocket:
                self._connection = None
                # Close all ACP sessions when NapCat disconnects
                logger.info("NapCat disconnected, closing all ACP sessions")
                await self._agent_manager.close_all_sessions()
            logger.info("Connection handler exited")

    async def _dispatch_event(self, event: dict) -> None:
        """Route an incoming OneBot event to the appropriate handler."""
        post_type = event.get("post_type", "")

        # Extract bot ID from any event's self_id
        if self._bot_id is None and "self_id" in event:
            self._bot_id = event["self_id"]
            logger.info("Bot QQ ID: %d", self._bot_id)

        if post_type == "meta_event":
            meta_type = event.get("meta_event_type", "")
            if meta_type == "lifecycle":
                logger.info("Lifecycle event: sub_type=%s", event.get("sub_type"))
            elif meta_type == "heartbeat":
                logger.debug("Heartbeat received")
            else:
                logger.debug("Unhandled meta event: %s", meta_type)

        elif post_type == "message":
            # Log every incoming message at DEBUG for full traceability
            msg_type = event.get("message_type", "?")
            user_id = event.get("user_id", "?")
            raw_msg = event.get("raw_message", "")[:150]
            logger.debug(
                "Raw message event: type=%s user=%s raw=%s",
                msg_type,
                user_id,
                raw_msg,
            )
            # Delegate to message handler in a separate task
            if self._bot_id is not None:
                task = asyncio.create_task(self._dispatcher.handle_message(event, self._bot_id))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:
                logger.warning("Received message before bot_id was set, ignoring")

        elif post_type == "notice":
            logger.debug(
                "Unhandled notice event: type=%s data=%s",
                event.get("notice_type", "?"),
                {k: v for k, v in event.items() if k not in ("post_type",)},
            )

        elif post_type == "request":
            logger.debug(
                "Unhandled request event: type=%s data=%s",
                event.get("request_type", "?"),
                {k: v for k, v in event.items() if k not in ("post_type",)},
            )

        else:
            logger.debug("Unknown post_type: %s keys=%s", post_type, list(event.keys()))

    # --- Outbound messaging ---

    async def _reply_text(self, event: dict, text: str) -> None:
        """Send a text reply back to the source of the message event."""
        message_type = event.get("message_type", "")
        segments = ai_to_onebot(text)

        # Log the reply text at DEBUG (may be long)
        logger.debug("Reply text (%d chars): %s", len(text), text[:300])

        if message_type == "private":
            resp = await self.send_api(
                "send_private_msg",
                {
                    "user_id": event["user_id"],
                    "message": segments,
                },
            )
            if resp and resp.get("retcode") != 0:
                logger.warning("send_private_msg failed: %s", resp)
        elif message_type == "group":
            resp = await self.send_api(
                "send_group_msg",
                {
                    "group_id": event["group_id"],
                    "message": segments,
                },
            )
            if resp and resp.get("retcode") != 0:
                logger.warning("send_group_msg failed: %s", resp)

    async def _reply_content(self, event: dict, parts: list[ContentPart]) -> None:
        """Send a mixed (text+image) reply back to the source of the message event."""
        message_type = event.get("message_type", "")
        segments = content_to_onebot(parts)

        text_preview = "".join(p.text for p in parts if p.type == "text")[:300]
        logger.debug(
            "Reply content: parts=%d segments=%d text_preview=%s",
            len(parts),
            len(segments),
            text_preview,
        )

        if message_type == "private":
            resp = await self.send_api(
                "send_private_msg",
                {
                    "user_id": event["user_id"],
                    "message": segments,
                },
            )
            if resp and resp.get("retcode") != 0:
                logger.warning("send_private_msg failed: %s", resp)
        elif message_type == "group":
            resp = await self.send_api(
                "send_group_msg",
                {
                    "group_id": event["group_id"],
                    "message": segments,
                },
            )
            if resp and resp.get("retcode") != 0:
                logger.warning("send_group_msg failed: %s", resp)

    async def send_api(self, action: str, params: dict | None = None) -> dict | None:
        """
        Send an OneBot 11 API request via WebSocket and wait for response.

        Returns the response dict, or None if no connection or timeout.
        """
        if self._connection is None:
            logger.warning("Cannot send API %s: no active connection", action)
            return None

        echo = str(uuid.uuid4())[:8]
        request = {
            "action": action,
            "params": params or {},
            "echo": echo,
        }

        # Create future for response
        loop = asyncio.get_event_loop()
        future: asyncio.Future[dict] = loop.create_future()
        self._pending[echo] = future

        try:
            logger.debug("API request: action=%s echo=%s", action, echo)
            await self._connection.send(json.dumps(request))
            # Wait for response with 10s timeout
            response = await asyncio.wait_for(future, timeout=10.0)
            logger.debug(
                "API response: action=%s status=%s retcode=%s",
                action,
                response.get("status"),
                response.get("retcode"),
            )
            return response
        except TimeoutError:
            logger.warning("API call %s timed out", action)
            self._pending.pop(echo, None)
            return None
        except websockets.ConnectionClosed:
            logger.warning("Connection closed while waiting for API %s", action)
            self._pending.pop(echo, None)
            return None
