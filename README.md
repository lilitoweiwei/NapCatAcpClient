# nochan (NapCatOpenCodeChannel)

A Python WebSocket server that bridges [NapCatQQ](https://github.com/NapNeko/NapCatQQ) and [OpenCode](https://github.com/sst/opencode) — chat with an AI coding assistant through QQ.

## Quick Start

```bash
# Install dependencies (requires uv and Python 3.12+)
uv sync

# Edit config.toml as needed, then start the server
uv run python main.py
```

## Configuration

See `config.toml` for all options. Key settings:

- `server.port` — WebSocket port for NapCatQQ to connect to
- `opencode.max_concurrent` — Max parallel AI processes (default 1)
- `logging.level` — Console log level; file always captures DEBUG

## QQ Commands

| Command | Description |
|---------|-------------|
| `/new`  | Start a new AI session (clears context) |
| `/help` | Show available commands |

In group chats, the bot must be @-mentioned to respond.

## Deploy as System Service

To run nochan as a systemd service with auto-start on boot (Linux):

```bash
sudo bash scripts/install-service.sh
```

Options: `--user USER`, `--project-dir DIR`, `--uv-path PATH` (all auto-detected by default).

After installation:

```bash
sudo systemctl start nochan          # Start now
sudo systemctl status nochan         # Check status
journalctl -u nochan -f              # Follow live logs
```

## Development

Run the following commands to ensure code quality (same as CI):

```bash
# Lint
uv run ruff check nochan/ tests/ main.py

# Format
uv run ruff format nochan/ tests/ main.py

# Type check
uv run mypy nochan/ main.py

# Test
uv run pytest tests/ -v
```

## Documentation

- [Product Spec](docs/product-v1.md) — Detailed v1 design
- [Dev Plan](docs/dev-plan-v1.md) — Step-by-step development plan
