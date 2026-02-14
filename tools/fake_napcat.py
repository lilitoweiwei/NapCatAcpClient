"""
Fake NapCatQQ client — interactive CLI tool for local testing of ncat.

Pretends to be a NapCatQQ instance by connecting to ncat's WebSocket server
via reverse WS, then lets you send messages interactively and see AI replies.

Usage:
    uv run python tools/fake_napcat.py [options]

Options:
    --url URL           WebSocket URL (default: read from config.toml)
    --config PATH       Path to config.toml (default: config.toml)
    --bot-id ID         Bot QQ ID to simulate (default: 1234567890)
    --user-id ID        Your QQ user ID (default: 111222)
    --user-name NAME    Your display name (default: TestUser)

Interactive commands:
    <text>              Send as private message (default mode)
    /group <text>       Send as group message with @bot
    /setuser <id> <name>    Change your user identity
    /setgroup <id> <name>   Change the default group
    /mode private|group     Switch the default send mode
    /help               Show this help
    /quit               Exit
"""

import argparse
import asyncio
import json
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

import websockets

# --- Display helpers ---


def timestamp() -> str:
    """Return current timestamp string for log output."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def print_colored(text: str, color: str) -> None:
    """Print text with ANSI color codes."""
    colors = {
        "green": "\033[92m",
        "yellow": "\033[93m",
        "cyan": "\033[96m",
        "red": "\033[91m",
        "gray": "\033[90m",
        "bold": "\033[1m",
        "reset": "\033[0m",
    }
    code = colors.get(color, "")
    reset = colors["reset"]
    print(f"{code}{text}{reset}")


def print_info(msg: str) -> None:
    print_colored(f"[{timestamp()}] {msg}", "gray")


def print_reply(text: str) -> None:
    print_colored(f"\n  AI: {text}\n", "green")


def print_error(msg: str) -> None:
    print_colored(f"[{timestamp()}] ERROR: {msg}", "red")


# --- State ---


class FakeNapCatState:
    """Mutable state for the fake NapCat client."""

    def __init__(
        self,
        bot_id: int = 1234567890,
        user_id: int = 111222,
        user_name: str = "TestUser",
    ) -> None:
        self.bot_id: int = bot_id
        self.user_id: int = user_id
        self.user_name: str = user_name
        # Default group for /group commands
        self.group_id: int = 999888
        self.group_name: str = "TestGroup"
        # Default send mode: "private" or "group"
        self.mode: str = "private"
        # Auto-incrementing message ID counter
        self._msg_id: int = 1000

    def next_msg_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    @property
    def prompt(self) -> str:
        """Build the interactive prompt string showing current mode."""
        if self.mode == "private":
            return f"[{self.user_name}@private] > "
        return f"[{self.user_name}@group:{self.group_id}] > "


# --- Event builders ---


def build_lifecycle_event(state: FakeNapCatState) -> dict:
    """Build a NapCat lifecycle connect event."""
    return {
        "time": int(time.time()),
        "self_id": state.bot_id,
        "post_type": "meta_event",
        "meta_event_type": "lifecycle",
        "sub_type": "connect",
    }


def build_private_message(state: FakeNapCatState, text: str) -> dict:
    """Build a OneBot 11 private message event."""
    return {
        "self_id": state.bot_id,
        "user_id": state.user_id,
        "time": int(time.time()),
        "message_id": state.next_msg_id(),
        "message_type": "private",
        "sub_type": "friend",
        "sender": {
            "user_id": state.user_id,
            "nickname": state.user_name,
            "card": "",
        },
        "message": [{"type": "text", "data": {"text": text}}],
        "message_format": "array",
        "raw_message": text,
        "font": 14,
        "post_type": "message",
    }


def build_group_message(state: FakeNapCatState, text: str) -> dict:
    """Build a OneBot 11 group message event with @bot."""
    return {
        "self_id": state.bot_id,
        "user_id": state.user_id,
        "time": int(time.time()),
        "message_id": state.next_msg_id(),
        "message_type": "group",
        "sub_type": "normal",
        "group_id": state.group_id,
        "group_name": state.group_name,
        "sender": {
            "user_id": state.user_id,
            "nickname": state.user_name,
            "card": "",
            "role": "member",
        },
        "message": [
            {"type": "at", "data": {"qq": str(state.bot_id)}},
            {"type": "text", "data": {"text": f" {text}"}},
        ],
        "message_format": "array",
        "raw_message": f"[CQ:at,qq={state.bot_id}] {text}",
        "font": 14,
        "post_type": "message",
    }


# --- WebSocket handling ---


async def recv_loop(
    ws: websockets.ClientConnection,
    state: FakeNapCatState,
) -> None:
    """Background task: receive messages from ncat and display replies."""
    try:
        async for raw in ws:
            data = json.loads(raw)

            # If it's an API call from the server (has 'action'), auto-respond
            if "action" in data and "echo" in data:
                action = data["action"]
                echo = data["echo"]

                # Extract the reply text from the message segments
                params = data.get("params", {})
                message_segments = params.get("message", [])
                reply_text = "".join(
                    seg.get("data", {}).get("text", "")
                    for seg in message_segments
                    if seg.get("type") == "text"
                )

                # Display the reply
                if reply_text:
                    print_reply(reply_text)
                else:
                    print_info(f"Received API call: {action} (no text content)")

                # Send success response back
                response = {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": state.next_msg_id()},
                    "message": "",
                    "wording": "",
                    "echo": echo,
                }
                await ws.send(json.dumps(response))

            else:
                # Unknown message from server
                print_info(f"Received: {json.dumps(data, ensure_ascii=False)[:200]}")

    except websockets.ConnectionClosed as e:
        print_error(f"Connection closed: code={e.code} reason={e.reason}")
    except Exception as e:
        print_error(f"Receive error: {e}")


async def interactive_loop(
    ws: websockets.ClientConnection,
    state: FakeNapCatState,
) -> None:
    """Read user input and send messages to ncat."""
    loop = asyncio.get_running_loop()

    print_help(state)

    while True:
        try:
            # Read input from stdin (non-blocking via executor)
            line = await loop.run_in_executor(None, lambda: input(state.prompt))
        except (EOFError, KeyboardInterrupt):
            print("\nExiting...")
            break

        line = line.strip()
        if not line:
            continue

        # --- Command dispatch ---
        if line.startswith("/"):
            parts = line.split(maxsplit=2)
            cmd = parts[0].lower()

            if cmd in ("/quit", "/exit", "/q"):
                print("Exiting...")
                break

            elif cmd == "/help":
                print_help(state)

            elif cmd == "/group" and len(parts) >= 2:
                # Send as group message
                text = line[len("/group") :].strip()
                event = build_group_message(state, text)
                await ws.send(json.dumps(event))
                print_info(f"Sent group message to {state.group_id}: {text}")

            elif cmd == "/setuser" and len(parts) >= 3:
                try:
                    state.user_id = int(parts[1])
                    state.user_name = parts[2]
                    print_info(f"User set to {state.user_name}({state.user_id})")
                except ValueError:
                    print_error("Usage: /setuser <id> <name>")

            elif cmd == "/setgroup" and len(parts) >= 3:
                try:
                    state.group_id = int(parts[1])
                    state.group_name = parts[2]
                    print_info(f"Group set to {state.group_name}({state.group_id})")
                except ValueError:
                    print_error("Usage: /setgroup <id> <name>")

            elif cmd == "/mode" and len(parts) >= 2:
                mode = parts[1].lower()
                if mode in ("private", "group"):
                    state.mode = mode
                    print_info(f"Default mode set to: {mode}")
                else:
                    print_error("Usage: /mode private|group")

            elif cmd == "/status":
                # Show current state
                print_colored(
                    f"  Bot ID:    {state.bot_id}\n"
                    f"  User:      {state.user_name}({state.user_id})\n"
                    f"  Group:     {state.group_name}({state.group_id})\n"
                    f"  Mode:      {state.mode}",
                    "cyan",
                )

            else:
                print_error(f"Unknown command: {cmd}. Type /help for help.")
        else:
            # Regular text — send according to current mode
            if state.mode == "private":
                event = build_private_message(state, line)
                await ws.send(json.dumps(event))
                print_info(f"Sent private message: {line}")
            else:
                event = build_group_message(state, line)
                await ws.send(json.dumps(event))
                print_info(f"Sent group message to {state.group_id}: {line}")


def print_help(state: FakeNapCatState) -> None:
    """Print help and current status."""
    print_colored(
        "\n=== Fake NapCat Client ===\n"
        f"  Bot ID:  {state.bot_id}\n"
        f"  User:    {state.user_name}({state.user_id})\n"
        f"  Group:   {state.group_name}({state.group_id})\n"
        f"  Mode:    {state.mode}\n"
        "\nCommands:\n"
        "  <text>              Send message (in current mode)\n"
        "  /group <text>       Send as group message with @bot\n"
        "  /setuser <id> <name>    Change user identity\n"
        "  /setgroup <id> <name>   Change default group\n"
        "  /mode private|group     Switch default send mode\n"
        "  /status             Show current settings\n"
        "  /help               Show this help\n"
        "  /quit               Exit\n",
        "cyan",
    )


# --- URL resolution ---


def resolve_url(args: argparse.Namespace) -> str:
    """Determine the WebSocket URL to connect to.

    Priority: --url flag > config.toml > default.
    """
    if args.url:
        return args.url

    # Try to read from config.toml
    config_path = Path(args.config)
    if config_path.exists():
        try:
            with open(config_path, "rb") as f:
                raw = tomllib.load(f)
            host = raw.get("server", {}).get("host", "127.0.0.1")
            port = raw.get("server", {}).get("port", 8080)
            # "0.0.0.0" means all interfaces — connect to localhost
            if host == "0.0.0.0":
                host = "127.0.0.1"
            url = f"ws://{host}:{port}"
            print_info(f"Read server address from {config_path}: {url}")
            return url
        except Exception as e:
            print_error(f"Failed to read {config_path}: {e}, using default")

    return "ws://127.0.0.1:8080"


# --- Main ---


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fake NapCatQQ client for local testing of ncat.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="If --url is not given, the server address is read from config.toml.",
    )
    parser.add_argument("--url", help="WebSocket URL (e.g. ws://127.0.0.1:8282)")
    parser.add_argument(
        "--config", default="config.toml", help="Path to config.toml (default: config.toml)"
    )
    parser.add_argument(
        "--bot-id", type=int, default=1234567890, help="Bot QQ ID (default: 1234567890)"
    )
    parser.add_argument(
        "--user-id", type=int, default=111222, help="Your QQ user ID (default: 111222)"
    )
    parser.add_argument(
        "--user-name", default="TestUser", help="Your display name (default: TestUser)"
    )
    args = parser.parse_args()

    # Resolve the WebSocket URL
    url = resolve_url(args)

    # Initialize state
    state = FakeNapCatState(
        bot_id=args.bot_id,
        user_id=args.user_id,
        user_name=args.user_name,
    )

    # Connect to ncat
    print_info(f"Connecting to {url} ...")
    try:
        async with websockets.connect(url) as ws:
            print_colored(f"[{timestamp()}] Connected to ncat!", "bold")

            # Send lifecycle event (what real NapCat does on connect)
            await ws.send(json.dumps(build_lifecycle_event(state)))
            print_info("Sent lifecycle connect event")

            # Run receiver and interactive input concurrently
            recv_task = asyncio.create_task(recv_loop(ws, state))
            try:
                await interactive_loop(ws, state)
            finally:
                recv_task.cancel()

    except ConnectionRefusedError:
        print_error(f"Connection refused at {url}. Is ncat running?")
        sys.exit(1)
    except Exception as e:
        print_error(f"Connection error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    import contextlib

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
