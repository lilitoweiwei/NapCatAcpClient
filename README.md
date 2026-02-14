# ncat (NapCat ACP Client)

A Python bridge that connects [NapCatQQ](https://github.com/NapNeko/NapCatQQ) to any [ACP](https://agentclientprotocol.com/)-compatible AI agent — chat with an AI coding assistant through QQ.

## How It Works

ncat acts as an **ACP client**: it launches an ACP-compatible agent as a subprocess, communicates via the Agent Client Protocol over stdin/stdout, and bridges user messages from QQ (via NapCatQQ) to the agent.

```
QQ User → NapCat (reverse WS) → ncat → ACP Agent (stdin/stdout)
QQ User ← NapCat (reverse WS) ← ncat ← ACP Agent (stdin/stdout)
```

## Prerequisites

- NapCatQQ configured in **reverse WebSocket** (OneBot v11) mode
- An **ACP-compatible agent** executable (configured via `agent.command` / `agent.args`)
- Python **3.12+** and `uv`

## Quick Start

### Linux / macOS

```bash
# Install dependencies (requires uv and Python 3.12+)
uv sync

# Create your config from the template
cp config.example.toml config.toml
# Edit config.toml as needed (especially [agent] section), then start
uv run python main.py
```

### Windows (PowerShell)

```powershell
# Install dependencies (requires uv and Python 3.12+)
uv sync

# Create your config from the template
Copy-Item config.example.toml config.toml

# Edit config.toml as needed (especially [agent] section), then start
uv run python main.py
```

Then configure NapCatQQ to connect to ncat's WebSocket server (default: `ws://127.0.0.1:8282`).

## Configuration

Copy `config.example.toml` to `config.toml` and customize. Key settings:

- `server.host` / `server.port` — WebSocket bind address + port for NapCatQQ to connect to
- `agent.command` / `agent.args` / `agent.cwd` — ACP agent subprocess command line + working directory
- `ux.thinking_notify_seconds` / `ux.thinking_long_notify_seconds` — "AI is thinking" notifications
- `ux.permission_timeout` / `ux.permission_raw_input_max_len` — permission prompt behavior
- `logging.level` — Console log level (log file always captures DEBUG)
- `logging.dir` / `logging.keep_days` / `logging.max_total_mb` — log retention and disk cap

By default, ncat loads `config.toml` from the current working directory. You can pass a config path:

```bash
uv run python main.py path/to/config.toml
```

## QQ Commands

| Command | Description |
|---------|-------------|
| `/new`  | Start a new AI session (clears context) |
| `/stop` | Cancel the current AI thinking |
| `/send <text>` | Forward text to the agent verbatim (bypass ncat command parsing) |
| `/help` | Show available commands |

Tip: use `/send /help` to invoke the agent's own `/help` without colliding with ncat commands.

In group chats, the bot must be @-mentioned to respond.

## Permission Requests

Some agents request permission before executing an operation/tool call. ncat will send a numbered
list of options; reply with `1`, `2`, ... to select. `/stop` also cancels a pending permission prompt.

## Deploy as System Service

To run ncat as a systemd service with auto-start on boot (Linux):

```bash
sudo bash scripts/install-service.sh
```

Options: `--user USER`, `--project-dir DIR`, `--uv-path PATH` (all auto-detected by default).

Note: the systemd unit runs `uv run python main.py` in the project directory, so it loads
`config.toml` from the project root by default.

After installation:

```bash
sudo systemctl start ncat          # Start now
sudo systemctl status ncat         # Check status
journalctl -u ncat -f              # Follow live logs
```

## Development

Run the following commands to ensure code quality (same as CI):

```bash
# Lint
uv run ruff check ncat/ tests/ main.py

# Format
uv run ruff format ncat/ tests/ main.py

# Type check
uv run mypy ncat/ main.py

# Test
uv run pytest tests/ -v
```

## License

MIT. See `LICENSE`.
