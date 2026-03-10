# ncat

This repo owns the NapCat ACP bridge.

## Start here

- Read `README.md` first for the protocol model, user commands, and session lifecycle.
- The main runtime entrypoint is `main.py`.
- The highest-value runtime files are `ncat/dispatcher.py`, `ncat/agent_manager.py`, and `ncat/acp_client.py`.
- If the issue is process startup or stdio transport, also inspect `ncat/agent_process.py` and `ncat/agent_connection.py`.

## Validation

- Install deps with `uv sync`.
- Run tests with `uv run pytest`.
- Run locally with `uv run python main.py [config_path]`.

## Logging

- Standalone repo-local runs default to `data/logs/`.
- `ncat.log` is now JSONL structured logs; prefer field-based queries over whole-file reading when tooling is available.

## Boundaries

- Keep this repo focused on QQ message flow, session orchestration, and ACP transport.
- Use `[agent].workspace_root` and `[agent].default_workspace` for workspace selection; do not add `[agent].cwd` back.
- The current foreground model is one chat -> one agent subprocess -> one ACP session until `/new` or a hard failure.
