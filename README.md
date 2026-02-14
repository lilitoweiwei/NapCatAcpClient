# ncat (NapCat ACP Client)

A Python bridge that connects [NapCatQQ](https://github.com/NapNeko/NapCatQQ) to any [ACP](https://agentclientprotocol.com/)-compatible AI agent — chat with an AI coding assistant through QQ.

## How It Works

ncat acts as an **ACP client**: it launches an ACP-compatible agent as a subprocess, communicates via the Agent Client Protocol over stdin/stdout, and bridges user messages from QQ (via NapCatQQ) to the agent.

```
QQ User → NapCat (reverse WS) → ncat → ACP Agent (stdin/stdout)
QQ User ← NapCat (reverse WS) ← ncat ← ACP Agent (stdin/stdout)
```

## Quick Start

```bash
# Install dependencies (requires uv and Python 3.12+)
uv sync

# Create your config from the template
cp config.example.toml config.toml
# Edit config.toml as needed (especially [agent] section), then start
uv run python main.py
```

## Configuration

Copy `config.example.toml` to `config.toml` and customize. Key settings:

- `server.port` — WebSocket port for NapCatQQ to connect to
- `agent.command` — ACP agent executable (e.g. `"claude"`, `"gemini"`)
- `agent.args` — Agent arguments (e.g. `["--experimental-acp"]`)
- `agent.cwd` — Working directory for the agent process
- `logging.level` — Console log level; file always captures DEBUG

## QQ Commands

| Command | Description |
|---------|-------------|
| `/new`  | Start a new AI session (clears context) |
| `/stop` | Cancel the current AI thinking |
| `/help` | Show available commands |

In group chats, the bot must be @-mentioned to respond.

## Deploy as System Service

To run ncat as a systemd service with auto-start on boot (Linux):

```bash
sudo bash scripts/install-service.sh
```

Options: `--user USER`, `--project-dir DIR`, `--uv-path PATH` (all auto-detected by default).

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
