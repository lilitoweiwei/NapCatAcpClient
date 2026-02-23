# ncat (NapCat ACP Client)

一个 Python 桥接器，将 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 连接到任何支持 [ACP](https://agentclientprotocol.com/) 的 AI 智能体 —— 让你通过 QQ 与 AI 编程助手聊天。

## 工作原理

ncat 作为一个 **ACP 客户端**：启动后不主动连接 Agent，仅在用户发送**第一条需要 AI 的消息**时建立到 Agent 网关（FAG）的连接，通过 FAG 与兼容 ACP 的智能体通信；来自 QQ（通过 NapCatQQ）的消息经 ncat 桥接到智能体。

```
QQ 用户 → NapCat (反向 WS) → ncat →（按需连接）→ FAG → ACP 智能体
QQ 用户 ← NapCat (反向 WS) ← ncat ← FAG ← ACP 智能体
```

## 快速启动

### 安装与运行

```bash
# 安装依赖
uv sync

# 从模板创建配置
cp config.example.toml config.toml

# 编辑 config.toml 后启动
uv run python main.py
# 或指定配置文件路径（用于指定另一套配置及持久化数据位置）
uv run python main.py /path/to/your.toml
```

**持久化数据与存储位置**：ncat 运行过程中产生的持久化数据仅有 **日志文件** 和 **Agent 工作目录**（子进程可能在其中创建文件）。两者均可通过配置文件指定位置；通过命令行传入配置文件路径即可间接指定存储位置。

| 数据 | 配置项 | 默认值 |
|------|--------|--------|
| 日志目录 | `[logging] dir` | `data/logs` |
| Agent 工作目录 | `[agent] cwd` | `~/.ncat/workspace` |

在 `config.toml` 中设置 `dir`、`cwd` 为绝对路径或相对路径（相对当前工作目录）；使用不同配置文件（如 `uv run python main.py /path/to/your.toml`）即可使用不同的数据目录。

ncat 启动后先起 WebSocket 服务，**不**建立到 Agent 的连接；用户发送第一条需要 AI 的消息时会**先尝试建立连接**，再处理该条消息。若建连失败（如 FAG 未就绪），会收到提示：「Agent 未连接，请稍后再试。」（执行 `/new` 后下次发消息时同样会先尝试重连。）

**指令 `/new` 与工作目录**：`/new` 结束当前会话并**断开与 Agent 的连接**，下次发消息时再重新连接（全新 Agent）。新会话的工作目录由 **Agent 网关（FAG）** 的默认配置决定。`/new <dir>` 则指定新会话的工作目录为网关的 workspace 下的 `<dir>`（如 `/new projectA` 表示 `/workspace/projectA`，具体路径由网关配置）。该指定仅对紧接着的那一次建会话生效，不持久化。

**可选配置**（`config.toml` 的 `[agent]` 下）：
- `initialize_timeout_seconds`：首次建连或重连时 ACP Initialize 等待超时（秒），默认 30。
- `retry_interval_seconds`：连接失败后，下次用户发消息时重试前的间隔（秒），默认 10。

然后配置 NapCatQQ 连接到 ncat 的 WebSocket 服务器（默认：`ws://127.0.0.1:8282`）。

若 Agent 在处理过程中发生错误（如流式输出中途失败），ncat 会先把**已生成的部分内容**原样发给用户，再发送错误说明并关闭当前会话，避免用户完全收不到回复。

## 部署为系统服务 (Systemd)

在 Linux 上，你可以将 ncat 部署为自动随系统启动的服务：

```bash
sudo bash scripts/install-service.sh
```

安装后：
- 启动服务: `sudo systemctl start ncat`
- 查看状态: `sudo systemctl status ncat`
- 实时日志: `journalctl -u ncat -f`

## 架构与模块

ncat 具备两个通信面：**NapCat 侧 (Server)** 接收 NapCatQQ 的 WebSocket（OneBot v11），**ACP 侧 (Client)** 以子进程方式启动 AI Agent，通过 stdin/stdout 进行 ACP (JSON-RPC 2.0) 通信。

**模块一览**：`main.py` 入口；`ncat/napcat_server.py` 面向 NapCat 的传输层；`dispatcher.py` 消息分发（解析 → 过滤 → 路由）；`prompt_runner.py` 单次 prompt 生命周期（超时、发送、取消）；`command.py` 命令执行（/new、/stop、/help）；`agent_manager.py` 会话编排（chat_id ↔ session_id）；`agent_process.py` Agent 子进程与 ACP 连接；`acp_client.py` ACP 回调（session_update、request_permission 服务端一律批准）；`converter.py`、`prompt_builder.py`、`image_utils.py` 负责 OneBot 与 ACP 格式转换及图片下载；`config.py`、`log.py`、`models.py` 配置、日志与共享数据类型。

**数据流概要**：NapCat 事件 → NcatNapCatServer 分发 → MessageDispatcher（解析、过滤、命令/忙碌检查）→ PromptRunner → AgentManager.send_prompt（映射会话、发 ACP、积累 ContentPart）→ 回复经 NcatAcpClient.session_update 回传 → 转 OneBot 段发回 NapCat。会话为内存映射，无持久化；/new 或 /new \<dir\> 清除映射并可选设置下次建会话的 cwd（用后即清），下次消息新建会话。

## 路线图与待办

**已完成**：项目更名为 ncat、后端切换为 ACP、移除 SQLite 持久化、System prompt 交由 Agent、即时通知、权限请求由服务端默认一律批准、/send 指令、AgentManager 独立、Image 支持等。

**计划中**：agent→qq 分段发送（超时前先发已积累内容）；超时反馈机制改善；将 NapCat 能力暴露为 MCP server；Agent 崩溃后自动重启；更智能的群消息过滤；可配置的 context header。
