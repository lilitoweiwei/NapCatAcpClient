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
from ncat.converter import ai_to_onebot_batches, content_to_onebot_batches
from ncat.dispatcher import MessageDispatcher
from ncat.log import debug_event, info_event, warning_event
from ncat.models import ContentPart

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
        max_reply_text_length: int = 500,
        reply_split_start_length: int = 300,
        image_download_timeout: float = 15.0,
        file_ingress_enabled: bool = True,
        file_inbox_dirname: str = ".qqfiles",
        file_download_timeout: float = 30.0,
        pending_ttl_seconds: float = 1800.0,
        max_file_size_mb: int | None = None,
        max_inline_image_mb: int = 2,
    ) -> None:
        # WebSocket bind address and port
        self._host = host
        self._port = port
        # ACP agent manager (needed for session cleanup on disconnect)
        self._agent_manager = agent_manager
        # Max text characters allowed in a single outbound QQ message.
        self._max_reply_text_length = max_reply_text_length
        # Preferred minimum accumulated text length before splitting at a newline.
        self._reply_split_start_length = reply_split_start_length

        # Currently active WebSocket connection from NapCatQQ (only one expected)
        self._connection: ServerConnection | None = None
        # Bot's own QQ ID, extracted from self_id in the first received event
        self._bot_id: int | None = None
        # In-flight API calls awaiting response, keyed by echo ID
        self._pending: dict[str, asyncio.Future[dict]] = {}
        # Background message-handling tasks (prevent GC from cancelling them)
        self._tasks: set[asyncio.Task[None]] = set()

        # Message dispatcher — business logic, decoupled from transport
        self._dispatcher = MessageDispatcher(
            agent_manager=agent_manager,
            reply_fn=self._reply_text,
            reply_content_fn=self._reply_content,
            thinking_notify_seconds=thinking_notify_seconds,
            thinking_long_notify_seconds=thinking_long_notify_seconds,
            image_download_timeout=image_download_timeout,
            file_ingress_enabled=file_ingress_enabled,
            file_inbox_dirname=file_inbox_dirname,
            file_download_timeout=file_download_timeout,
            pending_ttl_seconds=pending_ttl_seconds,
            max_file_size_mb=max_file_size_mb,
            max_inline_image_mb=max_inline_image_mb,
            get_file_fn=self._get_file_via_api,
        )

    async def start(self) -> None:
        """Start the WebSocket server and run forever."""
        info_event(
            logger,
            "ws_server_start",
            "Starting ncat server",
            host=self._host,
            port=self._port,
        )
        # Use None for host to bind all interfaces (IPv4 + IPv6)
        host = None if self._host == "0.0.0.0" else self._host
        async with websockets.serve(self._handler_ws, host, self._port):
            info_event(logger, "ws_server_ready", "Server ready, waiting for NapCatQQ connection")
            await asyncio.Future()  # run forever

    # --- WebSocket connection handling ---

    async def _handler_ws(self, websocket: ServerConnection) -> None:
        """Handle a WebSocket connection from NapCatQQ."""
        remote = websocket.remote_address
        info_event(logger, "ws_connect_ok", "NapCatQQ connected", remote=str(remote))
        self._connection = websocket

        try:
            async for raw_message in websocket:
                try:
                    data = json.loads(raw_message)
                except json.JSONDecodeError:
                    warning_event(
                        logger,
                        "ws_message_invalid",
                        "Non-JSON message received",
                        raw_preview=str(raw_message)[:200],
                    )
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
            warning_event(
                logger,
                "ws_disconnect",
                "Connection closed",
                code=e.code,
                reason=e.reason,
            )
        finally:
            if self._connection is websocket:
                self._connection = None
                # Close all ACP sessions and disconnect from agent when NapCat disconnects
                info_event(logger, "ws_cleanup", "NapCat disconnected, closing all ACP sessions")
                self._dispatcher.clear_pending_inputs()
                await self._agent_manager.close_all_sessions()
                await self._agent_manager.disconnect()
            info_event(logger, "ws_handler_exit", "Connection handler exited")

    async def _dispatch_event(self, event: dict) -> None:
        """Route an incoming OneBot event to the appropriate handler."""
        post_type = event.get("post_type", "")

        # Extract bot ID from any event's self_id
        if self._bot_id is None and "self_id" in event:
            self._bot_id = event["self_id"]
            info_event(logger, "bot_id_ready", "Bot QQ ID captured", self_id=self._bot_id)

        if post_type == "meta_event":
            meta_type = event.get("meta_event_type", "")
            if meta_type == "lifecycle":
                info_event(
                    logger,
                    "meta_lifecycle",
                    "Lifecycle event received",
                    sub_type=event.get("sub_type"),
                )
            elif meta_type == "heartbeat":
                # logger.debug("Heartbeat received")
                pass
            else:
                debug_event(logger, "meta_unhandled", "Unhandled meta event", meta_type=meta_type)

        elif post_type == "message":
            # Log every incoming message at DEBUG for full traceability
            msg_type = event.get("message_type", "?")
            user_id = event.get("user_id", "?")
            raw_msg = event.get("raw_message", "")[:150]
            debug_event(
                logger,
                "transport_message_received",
                "Raw message event received",
                message_type=msg_type,
                user_id=user_id,
                raw_message=raw_msg,
                group_id=event.get("group_id"),
                message_id=event.get("message_id"),
            )
            # Delegate to message handler in a separate task
            if self._bot_id is not None:
                task = asyncio.create_task(self._dispatcher.handle_message(event, self._bot_id))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:
                warning_event(
                    logger,
                    "transport_message_ignored",
                    "Received message before bot_id was set",
                )

        elif post_type == "notice":
            debug_event(
                logger,
                "notice_unhandled",
                "Unhandled notice event",
                notice_type=event.get("notice_type", "?"),
            )

        elif post_type == "request":
            debug_event(
                logger,
                "request_unhandled",
                "Unhandled request event",
                request_type=event.get("request_type", "?"),
            )

        else:
            debug_event(
                logger,
                "post_type_unknown",
                "Unknown post_type received",
                post_type=post_type,
                keys=list(event.keys()),
            )

    # --- Outbound messaging ---

    async def _send_message_batches(
        self,
        *,
        action: str,
        params: dict,
        message_batches: list[list[dict]],
        log_fields: dict,
    ) -> None:
        """Send one or more outbound QQ messages in order."""
        batch_count = len(message_batches)
        if batch_count > 1:
            info_event(
                logger,
                "reply_chunked",
                "Splitting outbound QQ reply into multiple messages",
                action=action,
                batch_count=batch_count,
                max_reply_text_length=self._max_reply_text_length,
                reply_split_start_length=self._reply_split_start_length,
                **log_fields,
            )

        for batch_index, batch in enumerate(message_batches, start=1):
            resp = await self.send_api(action, {**params, "message": batch})
            if resp and resp.get("retcode") != 0:
                warning_event(
                    logger,
                    "reply_send_fail",
                    f"{action} failed",
                    action=action,
                    retcode=resp.get("retcode"),
                    batch_index=batch_index,
                    batch_count=batch_count,
                    **log_fields,
                )
                return

    async def send_qq_reply(self, chat_id: str, text: str) -> None:
        """Send a QQ reply to the specified chat ID.

        This is a convenience wrapper for sending replies without needing the
        original event dict.

        Args:
            chat_id: QQ chat ID in internal format: "private:{user_id}" or "group:{group_id}"
            text: Reply text
        """
        message_batches = ai_to_onebot_batches(
            text,
            self._max_reply_text_length,
            self._reply_split_start_length,
        )
        if chat_id.startswith("private:"):
            try:
                user_id = int(chat_id.split(":", 1)[1])
                await self._send_message_batches(
                    action="send_private_msg",
                    params={"user_id": user_id},
                    message_batches=message_batches,
                    log_fields={"chat_id": chat_id},
                )
            except ValueError:
                warning_event(
                    logger,
                    "reply_send_invalid_chat",
                    "Invalid private chat_id",
                    chat_id=chat_id,
                )
        elif chat_id.startswith("group:"):
            try:
                group_id = int(chat_id.split(":", 1)[1])
                await self._send_message_batches(
                    action="send_group_msg",
                    params={"group_id": group_id},
                    message_batches=message_batches,
                    log_fields={"chat_id": chat_id},
                )
            except ValueError:
                warning_event(
                    logger,
                    "reply_send_invalid_chat",
                    "Invalid group chat_id",
                    chat_id=chat_id,
                )
        else:
            warning_event(
                logger,
                "reply_send_invalid_chat",
                "Invalid chat_id",
                chat_id=chat_id,
            )

    async def _reply_text(self, event: dict, text: str) -> None:
        """Send a text reply back to the source of the message event."""
        message_type = event.get("message_type", "")
        message_batches = ai_to_onebot_batches(
            text,
            self._max_reply_text_length,
            self._reply_split_start_length,
        )

        # Log the reply text at DEBUG (may be long)
        debug_event(
            logger,
            "reply_prepare",
            "Preparing text reply",
            message_type=message_type,
            text_len=len(text),
            batch_count=len(message_batches),
            text_preview=text[:300],
        )

        if message_type == "private":
            await self._send_message_batches(
                action="send_private_msg",
                params={"user_id": event["user_id"]},
                message_batches=message_batches,
                log_fields={"user_id": event.get("user_id")},
            )
        elif message_type == "group":
            await self._send_message_batches(
                action="send_group_msg",
                params={"group_id": event["group_id"]},
                message_batches=message_batches,
                log_fields={"group_id": event.get("group_id")},
            )

    async def _reply_content(self, event: dict, parts: list[ContentPart]) -> None:
        """Send a mixed (text+image) reply back to the source of the message event."""
        message_type = event.get("message_type", "")
        message_batches = content_to_onebot_batches(
            parts,
            self._max_reply_text_length,
            self._reply_split_start_length,
        )

        text_preview = "".join(p.text for p in parts if p.type == "text")[:300]
        debug_event(
            logger,
            "reply_prepare_content",
            "Preparing mixed reply content",
            message_type=message_type,
            part_count=len(parts),
            batch_count=len(message_batches),
            text_preview=text_preview,
        )

        if message_type == "private":
            await self._send_message_batches(
                action="send_private_msg",
                params={"user_id": event["user_id"]},
                message_batches=message_batches,
                log_fields={"user_id": event.get("user_id")},
            )
        elif message_type == "group":
            await self._send_message_batches(
                action="send_group_msg",
                params={"group_id": event["group_id"]},
                message_batches=message_batches,
                log_fields={"group_id": event.get("group_id")},
            )

    async def _get_file_via_api(self, file_id: str) -> dict | None:
        """Resolve a private-file download source via NapCat's get_file API."""
        return await self.send_api("get_file", {"file_id": file_id})

    async def send_api(self, action: str, params: dict | None = None) -> dict | None:
        """
        Send an OneBot 11 API request via WebSocket and wait for response.

        Returns the response dict, or None if no connection or timeout.
        """
        if self._connection is None:
            warning_event(
                logger,
                "api_request_dropped",
                "Cannot send API request without active connection",
                action=action,
            )
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
            debug_event(
                logger,
                "api_request",
                "Sending OneBot API request",
                action=action,
                echo=echo,
            )
            await self._connection.send(json.dumps(request))
            # Wait for response with 10s timeout
            response = await asyncio.wait_for(future, timeout=10.0)
            debug_event(
                logger,
                "api_response",
                "Received OneBot API response",
                action=action,
                echo=echo,
                status=response.get("status"),
                retcode=response.get("retcode"),
            )
            return response
        except TimeoutError:
            warning_event(
                logger,
                "api_timeout",
                "OneBot API call timed out",
                action=action,
                echo=echo,
            )
            self._pending.pop(echo, None)
            return None
        except websockets.ConnectionClosed:
            warning_event(
                logger,
                "api_connection_closed",
                "Connection closed while waiting for API response",
                action=action,
                echo=echo,
            )
            self._pending.pop(echo, None)
            return None
