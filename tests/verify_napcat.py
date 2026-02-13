"""
NapCatQQ WebSocket reverse-connection verification script.

This script starts a minimal WebSocket server that:
1. Accepts NapCatQQ's reverse WebSocket connection
2. Logs all incoming events (messages, heartbeats, etc.)
3. Provides interactive commands to send OneBot 11 API calls

Usage:
    uv run python tests/verify_napcat.py

Then send a message to the bot via QQ and observe the output.

Interactive commands (type in terminal):
    login       - Call get_login_info to get bot's QQ ID
    group <id>  - Call get_group_info for a specific group
    send <id> <text> - Send a private message to user <id>
    gsend <id> <text> - Send a group message to group <id>
    quit        - Stop the server
"""

import asyncio
import json
import sys
import uuid
from datetime import datetime

import websockets
from websockets.server import ServerConnection

# Server config
# Use None to listen on all interfaces (both IPv4 and IPv6),
# which avoids IPv4/IPv6 mismatch issues with SSH tunnels.
HOST = None
PORT = 8282

# Store active connection for sending API calls
active_connection: ServerConnection | None = None
# Store pending API responses keyed by echo id
pending_responses: dict[str, asyncio.Future[dict]] = {}


def timestamp() -> str:
    """Return current timestamp string for log output."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def pretty_json(data: dict) -> str:
    """Format JSON with indentation for readable output."""
    return json.dumps(data, ensure_ascii=False, indent=2)


def print_event(label: str, data: dict) -> None:
    """Print a labeled event with timestamp."""
    print(f"\n[{timestamp()}] === {label} ===")
    print(pretty_json(data))
    print("=" * 50)


async def send_api(action: str, params: dict | None = None) -> dict | None:
    """Send an OneBot 11 API request and wait for response."""
    if active_connection is None:
        print(f"[{timestamp()}] No active connection, cannot send API call")
        return None

    echo = str(uuid.uuid4())[:8]
    request = {
        "action": action,
        "params": params or {},
        "echo": echo,
    }

    # Create a future to wait for response
    loop = asyncio.get_event_loop()
    future: asyncio.Future[dict] = loop.create_future()
    pending_responses[echo] = future

    print(f"\n[{timestamp()}] >>> Sending API: {action}")
    print(pretty_json(request))

    await active_connection.send(json.dumps(request))

    # Wait for response with timeout
    try:
        response = await asyncio.wait_for(future, timeout=10.0)
        print_event(f"API Response ({action})", response)
        return response
    except asyncio.TimeoutError:
        print(f"[{timestamp()}] API call {action} timed out (10s)")
        pending_responses.pop(echo, None)
        return None


async def handle_connection(websocket: ServerConnection) -> None:
    """Handle a WebSocket connection from NapCatQQ."""
    global active_connection

    # Log connection info
    print(f"\n[{timestamp()}] NapCatQQ connected!")
    print(f"  Remote: {websocket.remote_address}")
    headers = websocket.request.headers if websocket.request else {}
    print(f"  User-Agent: {headers.get('User-Agent', 'N/A')}")
    print(f"  Protocol: {headers.get('Sec-WebSocket-Protocol', 'N/A')}")

    active_connection = websocket

    try:
        async for raw_message in websocket:
            try:
                data = json.loads(raw_message)
            except json.JSONDecodeError:
                print(f"[{timestamp()}] Non-JSON message: {raw_message}")
                continue

            # Check if this is an API response (has 'echo' field)
            if "echo" in data and data["echo"] in pending_responses:
                echo = data["echo"]
                pending_responses[echo].set_result(data)
                del pending_responses[echo]
                continue

            # Categorize and print the event
            post_type = data.get("post_type", "unknown")

            if post_type == "meta_event":
                meta_type = data.get("meta_event_type", "")
                if meta_type == "heartbeat":
                    # Heartbeat: print short form to avoid flooding
                    print(f"[{timestamp()}] heartbeat (interval={data.get('interval')})")
                elif meta_type == "lifecycle":
                    print_event("LIFECYCLE", data)
                else:
                    print_event(f"META_EVENT ({meta_type})", data)

            elif post_type == "message":
                msg_type = data.get("message_type", "")
                sender = data.get("sender", {})
                nickname = sender.get("card") or sender.get("nickname", "?")
                user_id = data.get("user_id", "?")

                if msg_type == "private":
                    print_event(
                        f"PRIVATE MESSAGE from {nickname}({user_id})", data
                    )
                elif msg_type == "group":
                    group_id = data.get("group_id", "?")
                    print_event(
                        f"GROUP MESSAGE in {group_id} from {nickname}({user_id})",
                        data,
                    )
                else:
                    print_event(f"MESSAGE ({msg_type})", data)

            elif post_type == "notice":
                print_event(f"NOTICE ({data.get('notice_type', '')})", data)

            elif post_type == "request":
                print_event(f"REQUEST ({data.get('request_type', '')})", data)

            else:
                print_event(f"UNKNOWN EVENT ({post_type})", data)

    except websockets.ConnectionClosed as e:
        print(f"\n[{timestamp()}] Connection closed: code={e.code}, reason={e.reason}")
    finally:
        active_connection = None
        print(f"[{timestamp()}] Connection handler exited")


async def interactive_console() -> None:
    """Read interactive commands from stdin."""
    # Give server a moment to start
    await asyncio.sleep(1)
    print(
        "\nInteractive commands:\n"
        "  login              - Get bot login info\n"
        "  group <group_id>   - Get group info\n"
        "  send <user_id> <text>  - Send private message\n"
        "  gsend <group_id> <text> - Send group message\n"
        "  quit               - Stop server\n"
    )

    loop = asyncio.get_event_loop()
    while True:
        # Read from stdin in a non-blocking way
        line = await loop.run_in_executor(None, sys.stdin.readline)
        line = line.strip()
        if not line:
            continue

        parts = line.split(maxsplit=2)
        cmd = parts[0].lower()

        if cmd == "quit":
            print("Shutting down...")
            # Signal the main loop to stop
            raise KeyboardInterrupt

        elif cmd == "login":
            await send_api("get_login_info")

        elif cmd == "group" and len(parts) >= 2:
            try:
                group_id = int(parts[1])
                await send_api("get_group_info", {"group_id": group_id})
            except ValueError:
                print("Usage: group <group_id>")

        elif cmd == "send" and len(parts) >= 3:
            try:
                user_id = int(parts[1])
                text = parts[2]
                await send_api("send_private_msg", {
                    "user_id": user_id,
                    "message": [{"type": "text", "data": {"text": text}}],
                })
            except ValueError:
                print("Usage: send <user_id> <text>")

        elif cmd == "gsend" and len(parts) >= 3:
            try:
                group_id = int(parts[1])
                text = parts[2]
                await send_api("send_group_msg", {
                    "group_id": group_id,
                    "message": [{"type": "text", "data": {"text": text}}],
                })
            except ValueError:
                print("Usage: gsend <group_id> <text>")

        else:
            print(f"Unknown command: {line}")
            print("Commands: login, group <id>, send <id> <text>, gsend <id> <text>, quit")


async def main() -> None:
    """Start the WebSocket server and interactive console."""
    print(f"[{timestamp()}] Starting NapCatQQ verification server on port {PORT} (all interfaces)")
    print(f"[{timestamp()}] Waiting for NapCatQQ to connect...")

    async with websockets.serve(handle_connection, HOST, PORT):
        try:
            await interactive_console()
        except KeyboardInterrupt:
            print(f"\n[{timestamp()}] Server stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
