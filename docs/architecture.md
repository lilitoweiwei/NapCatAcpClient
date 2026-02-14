# ncat Architecture

**ncat** (NapCat ACP Client) bridges NapCatQQ and ACP-compatible AI agents.
It acts as a **WebSocket server** for NapCatQQ (OneBot v11) and an
**ACP client** for the AI agent subprocess.

## High-Level Overview

```mermaid
sequenceDiagram
    participant NapCatQQ as NapCatQQ (QQ Bot)
    participant ncat as ncat (Server + Client)
    participant Agent as AI Agent (e.g. Claude)

    NapCatQQ->>ncat: WebSocket Connection (OneBot v11)
    ncat->>Agent: Spawn Subprocess
    ncat<<->>Agent: ACP JSON-RPC (stdin/stdout)
```

ncat has two communication interfaces:

- **NapCat side (server)**: Listens for WebSocket connections from NapCatQQ,
  receives OneBot v11 events, sends API calls back.
- **ACP side (client)**: Spawns an AI agent as a subprocess, communicates via
  the Agent Client Protocol (JSON-RPC 2.0 over stdin/stdout).

## Module Map

```
main.py                      Entry point: loads config, starts agent, runs server
ncat/
├── config.py                Configuration loading (config.toml → dataclasses)
├── log.py                   Logging setup (console + rotating file handler)
├── napcat_server.py         NapCat-facing WebSocket server (transport layer)
├── dispatcher.py            Message dispatcher (parse → filter → route)
├── prompt_runner.py         Prompt lifecycle manager (timeout, send, cancel)
├── command.py               Command executor (/new, /stop, /help)
├── converter.py             Message format conversion (OneBot ↔ internal)
├── acp_client.py            ACP protocol callbacks + agent subprocess manager
└── __init__.py
```

## Data Flow

A typical message goes through the following path:

```
NapCatQQ
  │ WebSocket event (OneBot v11 JSON)
  ▼
NcatNapCatServer._dispatch_event()
  │ Filters meta/notice/request events; dispatches message events
  ▼
MessageDispatcher.handle_message()
  │ Parses OneBot event → ParsedMessage
  │ Filters group messages without @bot
  │ Tries CommandExecutor first (for /commands)
  │ Checks busy state (rejects if AI already processing)
  ▼
PromptRunner.process()
  │ Builds context header (sender info + chat context)
  │ Starts timeout notification timers
  ▼
AgentManager.send_prompt()
  │ Maps chat_id → ACP session_id (creates session if needed)
  │ Sends prompt via ACP connection
  │ Waits for agent to complete the turn
  │                                          ┌──────────────────────────┐
  │   (during await, agent streams chunks)   │ NcatAcpClient            │
  │ ◄─────────────────────────────────────── │   .session_update()      │
  │                                          │   accumulates text       │
  │                                          └──────────────────────────┘
  │ Returns accumulated response text
  ▼
PromptRunner.process()
  │ Cancels timeout timers
  │ Calls reply_fn(event, response_text)
  ▼
NcatNapCatServer._reply_text()
  │ Converts text → OneBot segments
  │ Sends via WebSocket API call
  ▼
NapCatQQ → QQ User
```

## Module Responsibilities

### `napcat_server.py` — NcatNapCatServer

The transport layer facing NapCatQQ. Responsibilities:

- WebSocket server lifecycle (bind, accept connection)
- Raw JSON parsing and event dispatching by `post_type`
- Bot QQ ID extraction from first received event
- Outbound OneBot API call/response matching (echo-based)
- Reply sending (`send_private_msg` / `send_group_msg`)
- Closing all ACP sessions on NapCat disconnect

Does **not** contain any business logic — delegates to `MessageDispatcher`.

### `dispatcher.py` — MessageDispatcher

Thin message dispatcher. Pipeline:

1. Parse raw event → `ParsedMessage` (via `converter.onebot_to_internal`)
2. Filter: ignore group messages without @bot
3. Route to `CommandExecutor.try_handle()` (lightweight, non-blocking)
4. Check `PromptRunner.is_busy()` → reject if already processing
5. Dispatch to `PromptRunner.process()`

### `prompt_runner.py` — PromptRunner

Manages the full lifecycle of a single AI prompt request:

- Context header construction (`converter.build_context_header`)
- Active task tracking per chat_id
- Timeout notifications ("AI is thinking...", "/stop hint")
- Delegating to `AgentManager.send_prompt()`
- Error handling (agent crash → notify user, close session)
- Cancellation support (via `asyncio.Task.cancel` + ACP `session/cancel`)

### `command.py` — CommandExecutor

Parses and executes user commands:

| Command | Action |
|---------|--------|
| `/new`  | Close current ACP session (next message creates a new one) |
| `/stop` | Cancel the active AI task via callback to `PromptRunner.cancel` |
| `/help` | Show available commands |

Uses callback injection (`cancel_fn`) to avoid direct dependency on `PromptRunner`.

### `acp_client.py` — NcatAcpClient + AgentManager

Contains two classes with distinct responsibilities:

**NcatAcpClient** (ACP protocol callbacks):
- `session_update`: Accumulates `AgentMessageChunk` text into `AgentManager`
- `request_permission`: Auto-approves all tool call permission requests
- File system / terminal methods: All rejected (`method_not_found`)

**AgentManager** (agent subprocess + session lifecycle):
- Spawns agent subprocess, establishes ACP connection over stdio
- Initializes ACP protocol handshake
- Maps `chat_id` (QQ chat identifier) → ACP `session_id`
- Sends prompts and collects accumulated responses
- Handles cancellation via ACP `session/cancel`
- Manages agent process start/stop

### `converter.py`

Pure conversion functions, no state:

- `onebot_to_internal()`: OneBot v11 event dict → `ParsedMessage` dataclass
- `build_context_header()`: `ParsedMessage` → context-enriched prompt string
- `ai_to_onebot()`: AI response text → OneBot message segment array

### `config.py`

Configuration hierarchy loaded from `config.toml`:

```
NcatConfig
├── ServerConfig      (host, port)
├── AgentConfig       (command, args, cwd)
├── UxConfig          (thinking_notify_seconds, thinking_long_notify_seconds)
└── LoggingConfig     (level, dir, keep_days, max_total_mb)
```

## Session Model

Sessions are **in-memory only** — no persistence. The mapping is:

```
chat_id (e.g. "private:12345")  →  ACP session_id (UUID)
```

- A new ACP session is created on the first message from a chat.
- `/new` command removes the mapping; next message creates a fresh session.
- NapCat disconnect closes all sessions immediately.
- Agent crash closes the affected session.

## Dependency Graph

```
main.py
  ├── config.py
  ├── log.py
  ├── acp_client.py        (AgentManager)
  └── napcat_server.py     (NcatNapCatServer)
        └── dispatcher.py  (MessageDispatcher)
              ├── prompt_runner.py  (PromptRunner)
              │     ├── acp_client.py  (AgentManager)
              │     └── converter.py
              ├── command.py        (CommandExecutor)
              │     └── acp_client.py  (AgentManager)
              └── converter.py
```

Key design principle: dependencies flow **inward** — transport modules
(`napcat_server`) know about business logic (`handler`, `prompt_runner`),
but business logic does not import transport. Reply delivery uses an
injected callback (`reply_fn`).
