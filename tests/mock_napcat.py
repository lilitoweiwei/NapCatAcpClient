"""Mock NapCatQQ WebSocket client for testing."""

import asyncio
import json
import time

import websockets


class MockNapCat:
    """Simulates NapCatQQ's WebSocket client behavior for testing."""

    BOT_ID = 1234567890  # Default mock bot QQ ID

    def __init__(self, url: str = "ws://127.0.0.1:8282") -> None:
        self._url = url
        self._ws: websockets.ClientConnection | None = None
        self._message_id_counter = 1000
        # Received API calls from the server
        self._received: list[dict] = []

    async def connect(self) -> None:
        """Connect to the ncat server and send lifecycle event."""
        self._ws = await websockets.connect(self._url)
        # Send lifecycle connect event (what real NapCatQQ does)
        await self._send_event(
            {
                "time": int(time.time()),
                "self_id": self.BOT_ID,
                "post_type": "meta_event",
                "meta_event_type": "lifecycle",
                "sub_type": "connect",
            }
        )

    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def _send_event(self, event: dict) -> None:
        """Send a raw event to the server."""
        assert self._ws is not None
        await self._ws.send(json.dumps(event))

    def _next_message_id(self) -> int:
        """Generate a unique message ID."""
        self._message_id_counter += 1
        return self._message_id_counter

    async def send_private_message(self, user_id: int, nickname: str, text: str) -> None:
        """Send a simulated private message event."""
        await self._send_event(
            {
                "self_id": self.BOT_ID,
                "user_id": user_id,
                "time": int(time.time()),
                "message_id": self._next_message_id(),
                "message_type": "private",
                "sub_type": "friend",
                "sender": {
                    "user_id": user_id,
                    "nickname": nickname,
                    "card": "",
                },
                "message": [{"type": "text", "data": {"text": text}}],
                "message_format": "array",
                "raw_message": text,
                "font": 14,
                "post_type": "message",
            }
        )

    async def send_group_message(
        self,
        group_id: int,
        group_name: str,
        user_id: int,
        nickname: str,
        text: str,
        at_bot: bool = False,
    ) -> None:
        """Send a simulated group message event."""
        segments: list[dict] = []
        raw_parts: list[str] = []

        # Prepend @bot segment if requested
        if at_bot:
            segments.append({"type": "at", "data": {"qq": str(self.BOT_ID)}})
            raw_parts.append(f"[CQ:at,qq={self.BOT_ID}]")

        segments.append({"type": "text", "data": {"text": text}})
        raw_parts.append(text)

        await self._send_event(
            {
                "self_id": self.BOT_ID,
                "user_id": user_id,
                "time": int(time.time()),
                "message_id": self._next_message_id(),
                "message_type": "group",
                "sub_type": "normal",
                "group_id": group_id,
                "group_name": group_name,
                "sender": {
                    "user_id": user_id,
                    "nickname": nickname,
                    "card": "",
                    "role": "member",
                },
                "message": segments,
                "message_format": "array",
                "raw_message": "".join(raw_parts),
                "font": 14,
                "post_type": "message",
            }
        )

    async def send_heartbeat(self) -> None:
        """Send a simulated heartbeat event."""
        await self._send_event(
            {
                "time": int(time.time()),
                "self_id": self.BOT_ID,
                "post_type": "meta_event",
                "meta_event_type": "heartbeat",
                "interval": 30000,
            }
        )

    async def recv_api_call(self, timeout: float = 5.0) -> dict | None:
        """
        Receive the next API call from the server and auto-respond with success.

        Returns the API request dict, or None on timeout.
        """
        assert self._ws is not None
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
            data = json.loads(raw)

            # Auto-respond with success if it has an echo (API call)
            if "echo" in data:
                response = {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": self._next_message_id()},
                    "message": "",
                    "wording": "",
                    "echo": data["echo"],
                }
                await self._ws.send(json.dumps(response))

            self._received.append(data)
            return data

        except TimeoutError:
            return None

    def get_last_api_call(self) -> dict | None:
        """Get the most recent API call received."""
        return self._received[-1] if self._received else None

    def clear_received(self) -> None:
        """Clear all received API calls."""
        self._received.clear()
