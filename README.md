# ncat (NapCat ACP Client)

一个 Python 桥接器，将 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 连接到任何支持 [ACP](https://agentclientprotocol.com/) (Agent Client Protocol) 的 AI 智能体 —— 让你通过 QQ 与 AI 编程助手聊天。

## 工作原理

ncat 作为一个 **ACP 客户端**：收到某个 QQ chat 的第一条普通消息后，才会按需拉起该 chat 对应的 Agent 子进程，并通过标准输入输出（stdin/stdout）与之建立 ACP 连接；来自 QQ（通过 NapCatQQ）的消息经 ncat 桥接到智能体。

```text
QQ 用户 → NapCat (WebSocket 客户端) → ncat →（子进程 stdin/stdout）→ ACP 智能体
QQ 用户 ← NapCat (WebSocket 客户端) ← ncat ←（子进程 stdin/stdout）← ACP 智能体
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

**核心配置说明**：

为了让程序正常跑起来，你必须在 `config.toml` 中配置你要启动的 Agent。
打开 `config.toml`，找到 `[agent]` 块：
- `command`: ACP Agent 的可执行文件路径或命令名（例如 `"claude"`）。
- `args`: 传递给 Agent 的启动参数（例如 `["--experimental-acp"]`）。
- `workspace_root`: 工作区根目录（默认 `"/workspace"`）。
- `default_workspace`: 默认工作区名；当用户发送 `/new` 而不带参数时，会使用 `workspace_root/default_workspace`。
- `log_extra_context_env_var`: 可选环境变量名。若配置，ncat 会在每次拉起 Agent 前，把一个 JSON object 写入该环境变量，供外部 wrapper 记录额外日志上下文。

打开 `[ux]` 块时，另一个常用配置是：
- `max_reply_text_length`: 单条发往 QQ 的文本长度上限，默认 `500`。超过后，ncat 会在发送前自动拆成多条消息；设置为 `0` 可关闭预拆分。
- `reply_split_start_length`: 优先按换行切分的起始长度，默认 `300`。当累计文本长度超过该值后，ncat 会在遇到的第一个换行处切分，并移除该换行；若迟迟没有换行，仍会在 `max_reply_text_length` 处强制切分。

其他诸如日志目录、UX 体验优化、网络端口等丰富配置，请直接阅读 `config.example.toml` 中的注释，默认配置即可运行。

**持久化数据**：ncat 运行过程中产生的持久化数据主要有日志文件和工作区目录。它们均可在 `config.toml` 中指定（`[logging] dir` 和 `[agent] workspace_root`）。单独运行时，日志默认落在 `data/logs/`。当前 `ncat.log` 已采用一行一个 JSON 对象的结构化日志格式，适合后续按字段查询。

## Agent 启动时的对外输出约定

如果配置了 `[agent].log_extra_context_env_var`，ncat 会在启动 agent 子进程前，通过该环境变量传递一个 JSON object。ncat 自己只保证“传递 JSON object”，不规定 wrapper 必须使用哪个固定环境变量名，也不规定 wrapper 如何落盘。

当前 ncat 计划放入该 JSON object 的字段包括：

- `workspace`
- `workspace_name`
- `chat_id`
- `spawn_id`
- `agent_cwd`
- `agent_command`

这些字段都属于 ncat 已知、且可能对外部模块日志有帮助的上下文。外部模块可以选择全部接收，也可以只做 best-effort 记录。

**MCP servers**：如果目标 ACP agent 支持在 `session/new` 时接收 MCP server 配置，可以在 `[[mcp]]` 中声明额外的 MCP servers。当前 `ncat` 仅负责把这些配置透传给 ACP session，不负责某个具体 MCP server 的实现、文档或部署说明。

## 常用 NapCat 发送接口笔记

- `send_private_msg`：向私聊发送消息，常用参数是 `user_id` 与 `message`。
- `send_group_msg`：向群聊发送消息，常用参数是 `group_id` 与 `message`。
- `message` 使用 OneBot 11 segment 数组；`ncat` 当前主要发送 `text` 与 `image` 两类 segment。
- 在当前本地 NapCat/QQ 环境里，超长文本会让发送接口返回 `retcode=1200`。实测临界点大约在 6.1k 字附近，私聊和群聊都会触发，因此 `ncat` 默认会先把单条上限收紧到 `500`，并在超过 `reply_split_start_length` 后优先等待换行再切分；若直到 `max_reply_text_length` 仍没有换行，再做强制切分后逐条发送。

## 私聊附件缓冲

- 私聊里的文件-only消息会先落盘到当前 chat workspace 下的 `.qqfiles/`，然后提示用户继续发送说明。
- 私聊里的图片-only消息也会先缓冲，不会立刻触发 Agent。
- 只有当用户后续发送第一条带文本的消息时，ncat 才会把累计的文件和图片一起并入这轮 prompt。
- 文件会通过系统提示文本附加给 Agent，例如 `[SYSTEM: The user attached a file. It has been saved at /workspace/default/.qqfiles/foo.pdf]`。
- 若 Agent 支持图片，所有图片都会先统一预处理，再以内联 ACP 图片块发送：默认目标是压缩到 `ux.max_inline_image_mb = 2` MiB 以内；非透明图会转为 JPEG，透明图优先尝试 PNG optimize，若仍超预算则转为 WebP。
- `/new`、NapCat 断开和 pending TTL 过期都会清空尚未消费的附件缓冲。

## 指令系统

ncat 提供了一套完善的指令系统，你可以直接在 QQ 中向机器人发送以下指令。

### 基础指令

- `/new [workspace]` - 结束当前会话并清空 AI 上下文，同时停止当前 chat 对应的 Agent 子进程。下次发普通消息时，ncat 会在新的工作区懒启动一个新的 Agent，并创建一个新的 ACP 会话。可选参数用于指定 `workspace_root` 下的工作区；若目录不存在会自动创建。ncat 会把该工作区的绝对路径传给 ACP `session/new.cwd`，并在同一路径启动新的 Agent 子进程。
- `/stop` - 只中断当前这一次 AI 思考（当前 prompt turn），不会清空当前会话上下文。
- `/send <text>` - 将文本原样转发给 agent（避免以 `/` 开头的文本误触发 ncat 指令）。
- `/help` - 显示指令列表与帮助信息。

### 后台会话指令 (Background Session)

ncat 支持将耗时较长或需要后台独立运行的 Agent 任务放入后台会话。

> **注意**：要使用后台会话功能，你必须在 `config.toml` 中开启并配置 `[bsp_server]` (Background Session Protocol Server) 和 `[mqtt]` (用于接收后台运行状态的异步推送通知)。

- `/bg new <prompt>` - 创建一个后台会话并发送 prompt。
- `/bg newn <name> <prompt>` - 创建一个指定名称的后台会话。
- `/bg ls` - 列出所有后台会话及其运行状态。
- `/bg to i <index> <prompt>` - 向指定编号的后台会话追加发送 prompt。
- `/bg to n <name> <prompt>` - 向指定名称的后台会话追加发送 prompt。
- `/bg stop i <index>` - 停止指定编号的后台会话。
- `/bg stop n <name>` - 停止指定名称的后台会话。
- `/bg stop wait` - 停止所有等待输入中的后台会话。
- `/bg stop all` - 停止所有后台会话。
- `/bg history i <index>` / `/bg history n <name>` - 查看指定会话的完整对话历史。
- `/bg last i <index>` / `/bg last n <name>` - 查看指定会话的最后一条 AI 输出。

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

ncat 具备两个主要通信面：**NapCat 侧** 接收 NapCatQQ 的 WebSocket 事件，**ACP 侧** 以子进程方式启动 AI Agent，通过 stdin/stdout 进行 ACP (JSON-RPC 2.0) 通信。同时引入了对 BSP 后台任务和 MQTT 异步通知的支持。

```mermaid
graph TD
    User((QQ User)) <--> NapCat[NapCatQQ]
    NapCat <-- WebSocket --> NcatServer[NcatNapCatServer]
    
    subgraph ncat
        NcatServer --> Dispatcher[MessageDispatcher]
        Dispatcher --> CommandSys[CommandRegistry]
        Dispatcher --> PromptRunner[PromptRunner]
        
        PromptRunner --> AgentMgr[AgentManager]
        
        AgentMgr --> AgentConn[AgentConnection]
        AgentConn --> AgentProc[AgentProcess]
        AgentConn --> AcpClient[NcatAcpClient]
        
        AcpClient <--> AgentProc
        
        CommandSys --> BspClient[BspClient]
        MqttSub[MqttSubscriber] --> Dispatcher
    end
    
    AgentProc <-- stdio / ACP --> Agent((ACP Agent))
    BspClient <-- HTTP --> BspServer((BSP Server))
    MqttBroker((MQTT Broker)) -- Notifications --> MqttSub
```

**核心模块一览**：
- `main.py`：程序入口。
- `napcat_server.py`：面向 NapCat 的 WebSocket 传输层。
- `dispatcher.py`：消息分发、解析与过滤。
- `command_system.py` / `command.py` / `bg_command.py`：统一的指令注册与路由系统，支持正则匹配与帮助文档生成。
- `prompt_runner.py`：单次 prompt 生命周期管理（超时、发送、取消）。
- `agent_manager.py`：会话编排与连接生命周期管理。
- `agent_process.py` / `agent_connection.py`：Agent 子进程管理与底层 stdio 管道封装。
- `acp_client.py`：ACP 协议的回调处理。
- `bsp_client.py` / `mqtt_subscriber.py`：后台任务 HTTP 客户端与 MQTT 异步状态订阅。

## 前台会话生命周期

前台对话目前采用“每个 chat 一个 Agent 进程 + 一个 ACP 连接 + 一个持续复用的 ACP session”的模型：

- ncat 启动时不会预先启动 Agent。
- 某个 chat 的第一条普通消息到来时，才会懒启动该 chat 的 Agent 子进程，建立 ACP 连接并完成 `initialize`。
- 连接建立后，ncat 为该 chat 创建一个 ACP session；只要用户没有发送 `/new`，后续普通消息都会持续复用这个 session。
- 某些 Agent 会在 `session/prompt` 返回后继续送达少量尾部 `session/update` 分片；ncat 会在转发到 QQ 前短暂等待这些尾部流式分片，避免长回复被截断。
- 当前前台会话会根据部分 ACP 事件边界提前向 QQ 发送中间状态，而不是始终等到整轮结束后再一次性回复。当前会展示的状态主要包括思考中、规划中、工具调用中、以及权限请求已自动允许等提示。
- `/stop` 只会对当前 prompt turn 发送 `session/cancel`，不会结束会话，也不会重启 Agent 进程。
- `/new` 会丢弃当前 session、本地清空上下文，并停止该 chat 对应的 Agent 子进程；下一条普通消息才会重新启动新的 Agent 并创建新的 session。
- 如果 Agent 在一次对话中发生异常，ncat 会关闭当前 session；下一次普通消息会自动创建一个新的 session。
- 为避免多层 wrapper / ACP / MCP 进程残留，ncat 现在会把每个 chat 的 Agent 启动在独立进程组中；断开连接或 `/new` 时会优先尝试优雅退出，超时后再对整棵进程树执行强制回收。
