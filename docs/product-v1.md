# NapCatOpenCodeChannel (nochan) v1 产品文档

## 1. 项目概述

nochan 是一个 Python WebSocket 服务器，充当 NapCatQQ（QQ 机器人框架）与 OpenCode（终端 AI 助手）之间的桥梁。

**信息流**：QQ 用户 → QQ 服务器 → NapCatQQ → nochan → OpenCode → nochan → NapCatQQ → QQ 服务器 → QQ 用户

nochan 接收来自 QQ 的消息，交给 OpenCode CLI 处理，再将 AI 的回复发回 QQ。

### 1.1 运行环境

- **服务器**：2 核 CPU、2GB 内存的 VPS
- **Python**：3.12+
- **操作系统**：Linux
- **部署方式**：nochan 与 NapCatQQ 部署在同一台机器上，WebSocket 通信走本地回环地址

### 1.2 核心模块

nochan 在架构上分为三个核心模块：

| 模块 | 职责 |
|---|---|
| **会话管理（Session Manager）** | 维护 QQ 会话与 OpenCode session 的映射关系，持久化会话状态 |
| **消息转换（Message Converter）** | QQ OneBot 消息段 ↔ AI 纯文本之间的双向转换 |
| **OpenCode 封装（OpenCode Backend）** | 封装对 OpenCode CLI 的调用，管理并发队列 |

## 2. 技术栈

| 组件 | 选型 | 说明 |
|---|---|---|
| WebSocket 服务器 | `websockets` | 原生 asyncio，轻量低内存 |
| 持久化存储 | `aiosqlite`（SQLite） | 异步 SQLite，零配置，崩溃安全 |
| OpenCode 接入 | `asyncio.subprocess`（调用 `opencode run`） | 子进程方式，按需启动，空闲零占用 |
| 通信协议 | OneBot 11（反向 WebSocket） | NapCatQQ 推荐方式 |

### 2.1 依赖清单

```
websockets
aiosqlite
```

TOML 配置文件解析使用 Python 3.11+ 内置的 `tomllib`，无需额外依赖。

不引入 web 框架（如 FastAPI、aiohttp），因为 nochan 只需要一个 WebSocket 端点，`websockets` 库足够胜任。

### 2.2 日志方案

使用 Python 标准库 `logging`，不引入额外依赖。

- **控制台输出**：`StreamHandler`，级别由配置控制（默认 `INFO`），用于实时查看
- **文件持久化**：`TimedRotatingFileHandler`，**始终记录 DEBUG 级别**，确保所有诊断信息可追溯
- **日志路径**：`data/logs/nochan.log`（当天）、`data/logs/nochan.log.2026-02-13`（历史）
- **日志格式**：`[2026-02-13 10:30:00] [INFO] [session] 消息内容`
- **日志轮转**：按天轮转，保留最近 30 天的日志文件
- **总量上限**：启动时检查日志目录总大小，超过 `max_total_mb`（默认 100MB）时自动删除最旧的日志文件

各模块使用独立的 logger name（如 `nochan.server`、`nochan.session`、`nochan.opencode`），便于按模块过滤。

**日志充分性原则**：仅从日志文件即可判断消息是"没收到"还是"收到了但未处理"。具体而言：
- 每条 QQ 消息到达时都会在 DEBUG 级别记录原始事件
- 群聊消息因未 @bot 而被忽略时，在 DEBUG 级别明确记录原因
- OpenCode 的每条 JSONL 事件、完整响应内容、工具调用详情都在 DEBUG 级别记录
- API 调用的请求和响应在 DEBUG 级别记录
- 发送给用户的回复文本在 DEBUG 级别记录

## 3. 通信协议详述

### 3.1 WebSocket 连接

采用**反向 WebSocket** 模式：

- nochan 启动后监听 `ws://0.0.0.0:<port>/`（同时绑定 IPv4 和 IPv6）
- NapCatQQ 作为客户端主动连接到该地址
- 连接建立后，双方通过该通道双向通信
- 连接建立时 NapCatQQ 会发送 lifecycle 事件（`meta_event_type: "lifecycle"`, `sub_type: "connect"`），nochan 可据此确认连接就绪
- 无需 access_token（纯本地通信）

> **注意**：服务器必须绑定 `0.0.0.0`（或不指定 host）以同时监听 IPv4 和 IPv6。如果只绑定 `127.0.0.1`（IPv4），当客户端通过 IPv6（`::1`）连接时会导致握手超时。这在通过 SSH 隧道远程开发时尤为关键。

NapCatQQ 内置自动重连机制，nochan 无需处理主动重连。

### 3.2 OneBot 11 事件格式

nochan 需要处理的事件类型：

#### 消息事件（message）

nochan 只关注消息事件中的**私聊消息**和**群聊消息**。

**私聊消息示例**（基于实际验证）：

```json
{
  "self_id": 2755873631,
  "user_id": 437566830,
  "time": 1770961587,
  "message_id": 1133503450,
  "message_seq": 1133503450,
  "message_type": "private",
  "sub_type": "friend",
  "sender": {
    "user_id": 437566830,
    "nickname": "用户昵称",
    "card": ""
  },
  "message": [
    {"type": "text", "data": {"text": "你好"}}
  ],
  "message_format": "array",
  "raw_message": "你好",
  "font": 14,
  "post_type": "message"
}
```

**群聊消息示例**（基于实际验证）：

```json
{
  "self_id": 2755873631,
  "user_id": 437566830,
  "time": 1770961668,
  "message_id": 1491789200,
  "message_type": "group",
  "sub_type": "normal",
  "group_id": 446023742,
  "group_name": "群聊名称",
  "sender": {
    "user_id": 437566830,
    "nickname": "用户昵称",
    "card": "",
    "role": "owner"
  },
  "message": [
    {"type": "at", "data": {"qq": "2755873631"}},
    {"type": "text", "data": {"text": " 帮我写个函数"}}
  ],
  "message_format": "array",
  "raw_message": "[CQ:at,qq=2755873631] 帮我写个函数",
  "font": 14,
  "post_type": "message"
}
```

**实际验证中确认的关键细节**：

- `self_id`：所有事件都包含此字段，值为 bot 自身的 QQ 号（int），可直接用于判断 @bot
- `at` 段的 `data.qq` 是**字符串**类型（如 `"2755873631"`），与 `self_id`（int）比较时需注意类型转换
- 群聊消息**直接包含 `group_name` 字段**，无需额外调用 `get_group_info` 即可获取群名称
- `sender.role` 在群聊中可取值 `"owner"` / `"admin"` / `"member"`
- `message_format` 字段确认消息格式为 `"array"`（需在 NapCatQQ 中配置）

#### 元事件（meta_event）

nochan 需要处理的元事件：

- **lifecycle**（`sub_type: "connect"`）：连接建立时 NapCatQQ 发送，nochan 据此确认连接就绪，可在此时通过 `self_id` 记录 bot 自身 QQ 号
- **heartbeat**（每 30 秒一次）：维持连接状态，nochan 无需回复，仅记录日志

#### 通知事件（notice）和请求事件（request）

v1 版本忽略，不做处理。

### 3.3 OneBot 11 API 调用格式

nochan 向 NapCatQQ 发送 API 请求的格式：

```json
{
  "action": "send_msg",
  "params": {
    "message_type": "group",
    "group_id": 789012,
    "message": [
      {"type": "text", "data": {"text": "这是 AI 的回复"}}
    ]
  },
  "echo": "unique_request_id"
}
```

NapCatQQ 返回的响应（基于实际验证）：

```json
{
  "status": "ok",
  "retcode": 0,
  "data": {"message_id": 2007072546},
  "message": "",
  "wording": "",
  "echo": "unique_request_id"
}
```

> 响应中包含额外字段 `message`、`wording`（通常为空字符串），nochan 解析时只需关注 `status`、`retcode`、`data` 和 `echo`。

nochan v1 使用的 API：

| API | 用途 |
|---|---|
| `send_private_msg` | 发送私聊消息 |
| `send_group_msg` | 发送群聊消息 |

> **简化说明**：经实际验证，bot 自身 QQ 号可从任意事件的 `self_id` 字段获取（lifecycle 连接事件最先到达），无需调用 `get_login_info`。群名称直接包含在群消息事件的 `group_name` 字段中，无需调用 `get_group_info`。因此 v1 只需要发送消息的 API。

## 4. 会话管理

### 4.1 会话标识

每个会话由一个 **chat_id** 唯一标识来源：

| 消息类型 | chat_id 构成 | 示例 |
|---|---|---|
| 私聊 | `private:<user_id>` | `private:123456` |
| 群聊 | `group:<group_id>` | `group:789012` |

一个群对应一个会话上下文，群内所有用户共享同一个 AI 会话。

### 4.2 会话结构

每个 chat_id 维护一个**按时间排序的会话列表**，只有最新的会话处于激活状态。

```
chat_id: "group:789012"
  └── sessions: [
        {id: "s1", opencode_session_id: "oc-xxx", status: "archived", created_at: ...},
        {id: "s2", opencode_session_id: "oc-yyy", status: "archived", created_at: ...},
        {id: "s3", opencode_session_id: "oc-zzz", status: "active",   created_at: ...}  ← 当前激活
      ]
```

### 4.3 会话生命周期

1. **自动创建**：当 nochan 收到某个 chat_id 的第一条消息时，自动创建该 chat_id 的第一个会话
2. **用户新建**：用户发送 `/new` 指令时，将当前激活会话归档（status → `archived`），创建新会话
3. **状态流转**：`active` → `archived`（仅在新建时触发）

v1 不支持切换到历史会话，只能和最新的激活会话交互。

### 4.4 持久化方案

使用 SQLite 数据库（WAL 模式），存储在本地文件 `data/nochan.db` 中。

**sessions 表**：

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | nochan 内部会话 ID（UUID） |
| chat_id | TEXT NOT NULL | 来源标识（如 `group:789012`） |
| opencode_session_id | TEXT | 对应的 OpenCode session ID，首次调用 OpenCode 时填入 |
| status | TEXT NOT NULL | `active` 或 `archived` |
| created_at | TEXT NOT NULL | 创建时间（ISO 8601） |
| updated_at | TEXT NOT NULL | 最后更新时间（ISO 8601） |

**索引**：`chat_id + status` 联合索引，用于快速查找某个 chat_id 的激活会话。

**持久化策略**：
- SQLite WAL 模式提供崩溃安全性
- 会话状态变更时立即写入（创建、归档）
- 不缓存会话消息历史（消息历史由 OpenCode 自身管理）

## 5. 消息转换

### 5.1 QQ → AI（入站转换）

将 OneBot 11 消息段数组转换为 AI 可理解的纯文本。

| 消息段类型 | 转换规则 |
|---|---|
| `text` | 直接提取 `data.text` |
| `at` | 忽略 @bot 自身的 at；其他 at 转换为 `@昵称`。注意 `data.qq` 为**字符串**类型，与 `self_id`（int）比较时需类型转换 |
| `image` | 转换为占位符 `[图片]` |
| `face` | 转换为占位符 `[表情]` |
| `reply` | 忽略（v1 不处理引用） |
| 其他类型 | 忽略 |

多个消息段按顺序拼接为单个字符串。

**附加上下文**：在发送给 OpenCode 的 prompt 前面，注入当前消息的来源信息。群名称直接从消息事件的 `group_name` 字段获取，无需额外 API 调用。

私聊示例：

```
[私聊，用户 张三(123456)]
你好，帮我写个排序函数
```

群聊示例：

```
[群聊 开发讨论组(789012)，用户 张三(123456)]
你好，帮我写个排序函数
```

这样 AI 能感知到是谁在和它说话、在哪个群聊中。

### 5.2 AI → QQ（出站转换）

将 OpenCode 的文本输出转换为 OneBot 11 消息段。

v1 采用最简策略：**AI 输出直接作为一个 text 消息段发送**。不做 Markdown 解析或格式转换。

```json
[{"type": "text", "data": {"text": "AI 的完整输出文本"}}]
```

### 5.3 特殊输出处理

OpenCode 的输出可能包含工具调用结果（如文件修改、命令执行等）。v1 中，这些内容直接作为文本原样输出。如果 OpenCode 的输出格式中包含结构化信息，后续版本可将其解析为更友好的格式。

## 6. OpenCode 封装

### 6.1 接入方式

v1 通过子进程调用 `opencode run --format json` 命令：

```bash
# 新会话的首次调用
opencode run --format json "用户的消息"

# 在已有会话中继续对话
opencode run --format json -s <session_id> "用户的消息"
```

`--format json` 使 opencode 以 JSONL 格式（每行一个 JSON 对象）输出结构化事件流到 stdout。nochan 逐行解析这些事件来提取 session ID 和 AI 回复。

### 6.2 接口抽象

为便于未来切换到 SDK 模式（`opencode serve` + `opencode-ai` Python SDK），OpenCode 封装层定义统一的抽象接口：

```python
class OpenCodeBackend(Protocol):
    async def send_message(
        self, session_id: str | None, message: str
    ) -> OpenCodeResponse:
        """
        Send a message to OpenCode.
        - session_id=None: create a new session
        - session_id=<id>: continue an existing session
        Returns the AI response and the session ID used.
        """
        ...
```

```python
@dataclass
class OpenCodeResponse:
    session_id: str      # OpenCode session ID (new or existing)
    content: str         # AI response text
    success: bool        # whether the call succeeded
    error: str | None    # error message if failed
```

v1 实现 `SubprocessOpenCodeBackend`，未来可实现 `SDKOpenCodeBackend`。

### 6.3 并发队列

由于 VPS 资源有限（2 核 2GB），OpenCode 封装层内置并发控制：

- 使用 `asyncio.Semaphore` 限制同时运行的 `opencode run` 进程数，**上限为 2**
- 超出上限的请求在队列中等待
- 队列中的请求按 FIFO 顺序处理
- 当请求进入等待状态时，nochan 向用户发送提示消息（如"AI 正在忙，请稍候..."）

### 6.4 OpenCode 工作目录

所有 OpenCode 调用共享同一个工作目录，默认为 `~/.nochan/workspace`（可通过配置修改），不做用户/群隔离。nochan 启动时自动创建该目录（如不存在）。

### 6.5 JSONL 事件解析

`opencode run --format json` 输出的每一行都是一个 JSON 对象，包含 `type` 和 `sessionID` 字段。nochan 需要解析以下事件类型：

| 事件类型 | 用途 | 关键字段 |
|---|---|---|
| `step_start` | 获取 session ID | `sessionID`（格式 `ses_XXX`） |
| `text` | 提取 AI 回复文本 | `part.text` |
| `tool_use` | 记录工具调用（v1 仅日志记录） | `part.tool`、`part.state.output` |
| `step_finish` | 判断是否完成 | `part.reason`（`"stop"` 表示最终完成） |
| `error` | 捕获错误 | `error.data.message` |

**Session ID 获取**：从第一条事件（通常是 `step_start`）的 `sessionID` 字段提取，格式为 `ses_XXX`。新建会话和继续会话都会在事件中包含 session ID。

**AI 回复提取**：收集所有 `text` 类型事件的 `part.text` 字段，拼接为完整的 AI 回复。

**完成判断**：当收到 `step_finish` 事件且 `part.reason` 为 `"stop"` 时，表示 AI 已完成回复。`part.reason` 为 `"tool-calls"` 时表示 AI 正在调用工具，还未完成。

**JSONL 示例**（精简）：

```jsonl
{"type":"step_start","sessionID":"ses_abc123...","part":{"type":"step-start",...}}
{"type":"tool_use","sessionID":"ses_abc123...","part":{"tool":"bash","state":{"status":"completed","output":"hello\n",...}}}
{"type":"step_finish","sessionID":"ses_abc123...","part":{"reason":"tool-calls",...}}
{"type":"step_start","sessionID":"ses_abc123...","part":{"type":"step-start",...}}
{"type":"text","sessionID":"ses_abc123...","part":{"text":"命令输出为 hello","type":"text",...}}
{"type":"step_finish","sessionID":"ses_abc123...","part":{"reason":"stop","cost":0.001,"tokens":{...}}}
```

## 7. 用户指令

v1 支持的用户指令：

| 指令 | 说明 |
|---|---|
| `/new` | 归档当前会话，创建新会话（清空 AI 上下文） |
| `/help` | 显示帮助信息，列出所有可用指令 |

**指令处理规则**：
- 非 `/` 开头的消息视为发送给 AI 的普通消息
- 以 `/` 开头但不匹配任何已知指令的消息，视为无效指令，回复帮助信息

**帮助信息模板**：

```
nochan 指令列表：
/new  - 创建新会话（清空 AI 上下文）
/help - 显示本帮助信息
直接发送文字即可与 AI 对话。
```

**群聊触发方式**：群聊中必须 @bot 才触发 nochan 处理（包括指令和普通消息），未 @bot 的消息一律忽略。私聊消息则全部处理。

## 8. 完整处理流程

### 8.1 收到 QQ 消息

```
1. NapCatQQ 通过 WebSocket 发送 OneBot 11 事件 JSON
2. nochan 解析事件，过滤出 message 类型事件
3. 判断消息类型：
   - 私聊：直接处理
   - 群聊：检查消息段中是否有 at 段且 data.qq == str(self_id)，未 @bot 则忽略
4. 提取 chat_id（private:<user_id> 或 group:<group_id>）
5. 查询该 chat_id 的激活会话
   - 无激活会话 → 自动创建新会话
   - 有激活会话 → 使用该会话
6. 消息转换：OneBot 消息段 → 纯文本（附加来源上下文）
7. 检查是否为指令：
   - /new → 归档当前会话，创建新会话，回复"已创建新会话"
   - /help → 回复帮助信息
   - /其他 → 回复帮助信息（无效指令）
   - 普通消息 → 进入步骤 8
8. 将消息提交到 OpenCode 封装层的请求队列
   - 如需等待 → 向用户发送"正在排队..."提示
9. OpenCode 封装层调用 opencode run，获取 AI 回复
10. 消息转换：AI 回复文本 → OneBot 消息段
11. 通过 WebSocket 调用 send_msg API 发送回复
```

### 8.2 错误处理（v1 基础版）

| 错误场景 | 处理方式 |
|---|---|
| OpenCode 进程退出码非 0 | 向用户发送"AI 处理出错，请稍后重试" |
| OpenCode 进程输出为空 | 向用户发送"AI 未返回有效回复" |
| WebSocket 连接断开 | 记录日志，等待 NapCatQQ 重连 |
| 数据库操作失败 | 记录日志，向用户发送错误提示 |

## 9. 配置

nochan 通过一个配置文件（`config.toml`）管理运行参数：

```toml
[server]
host = "0.0.0.0"
port = 8080                      # WebSocket 监听端口

[opencode]
command = "opencode"             # opencode 可执行文件路径
work_dir = "~/.nochan/workspace" # opencode 工作目录（默认）
max_concurrent = 1               # 最大并发 opencode 进程数

[database]
path = "data/nochan.db"          # SQLite 数据库路径

[logging]
level = "INFO"                   # 控制台日志级别（文件始终记录 DEBUG）
dir = "data/logs"                # 日志文件目录
keep_days = 30                   # 日志文件保留天数
max_total_mb = 100               # 日志总量上限（MB），超出时删除最旧的
```

## 10. 项目结构

```
nochan/
├── main.py                  # 入口，启动 WebSocket 服务器
├── config.toml              # 运行配置
├── docs/
│   ├── product-v1.md        # 产品文档
│   └── dev-plan-v1.md       # 开发计划
├── data/                    # 运行时数据（运行时生成）
│   ├── nochan.db            # SQLite 数据库
│   └── logs/                # 日志文件目录
├── tests/
│   ├── conftest.py          # pytest 公共 fixtures
│   ├── mock_napcat.py       # 模拟 NapCatQQ 客户端
│   ├── test_config.py       # 配置加载测试
│   ├── test_converter.py    # 消息转换测试
│   ├── test_handler.py      # 消息处理流水线测试（纯业务逻辑，无 WebSocket）
│   ├── test_session.py      # 会话管理测试
│   ├── test_opencode.py     # OpenCode 封装测试
│   ├── test_server.py       # WebSocket 传输层测试
│   ├── test_integration.py  # 端到端集成测试
│   └── manual/              # 手动验证脚本（需真实外部服务）
│       ├── verify_napcat.py
│       └── verify_opencode.py
└── nochan/
    ├── __init__.py
    ├── config.py             # 配置加载
    ├── log.py                # 日志初始化
    ├── server.py             # WebSocket 传输层（连接管理、事件分发、API 调用）
    ├── handler.py            # 消息处理流水线（业务逻辑，不依赖 WebSocket）
    ├── session.py            # 会话管理（Session Manager）
    ├── converter.py          # 消息转换（Message Converter）
    └── opencode.py           # OpenCode 封装（OpenCode Backend）
```

## 11. 未来开发方向

以下功能不在 v1 范围内，记录为后续迭代参考：

- **OpenCode SDK 接入**：当 `opencode serve` + Python SDK 成熟后，切换到 SDK 模式，获得流式响应、更丰富的事件监听等能力
- **流式输出**：AI 回复实时逐步推送到 QQ，而非等全部完成后一次性发送
- **富媒体消息**：支持图片收发（下载 QQ 图片传给 AI，AI 生成的图片发回 QQ）、代码块高亮显示、文件修改 diff 展示等
- **QQ 消息长度处理**：对超长 AI 回复进行智能分段，或使用合并转发消息
- **超时处理**：为 OpenCode 调用设置超时，超时后通知用户并终止进程
- **权限控制**：配置哪些 QQ 号/群号允许使用 nochan，防止资源滥用
- **定时任务**：支持配置定时 AI 任务，到时间自动触发 OpenCode 执行并发送结果
- **历史会话切换**：允许用户通过指令切换到历史会话继续对话
- **多工作目录隔离**：不同用户/群使用独立的 OpenCode 工作目录
- **WebSocket 认证**：为非本地部署场景添加 access_token 认证
- **Web 管理面板**：提供简单的 Web UI 用于查看会话状态、系统监控等
