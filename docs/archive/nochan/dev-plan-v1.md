# nochan v1 开发计划

本文档基于 `product-v1.md` 制定，将开发过程拆分为 7 个步骤。每个步骤都是一个可独立运行和验证的完整变更，可作为单独的 commit 提交。

## 测试策略

### 目录结构

```
tests/
├── conftest.py              # pytest 公共 fixtures
├── mock_napcat.py           # 模拟 NapCatQQ 的 WebSocket 客户端（被 fixtures 和手动测试共用）
├── test_config.py           # 步骤 1 的自动化测试
├── test_server.py           # 步骤 2 的自动化测试
├── test_converter.py        # 步骤 3 的自动化测试
├── test_session.py          # 步骤 4 的自动化测试
├── test_opencode.py         # 步骤 5 的自动化测试（mock subprocess）
├── test_integration.py      # 步骤 6 的自动化集成测试
└── manual/                  # 手动验证脚本（需要真实外部服务）
    ├── verify_napcat.py     # 连接真实 NapCatQQ 验证协议
    └── verify_opencode.py   # 调用真实 OpenCode CLI 验证输出
```

### 原则

- **自动化测试**（`tests/test_*.py`）：用 `pytest` + `pytest-asyncio` 运行，不依赖外部服务（NapCatQQ、OpenCode），通过 mock 实现隔离。每个步骤完成后编写对应的测试。执行命令：`uv run pytest`
- **手动验证脚本**（`tests/manual/`）：需要真实的 NapCatQQ 连接或 OpenCode CLI，用于首次协议验证和端到端确认。将现有的 `verify_napcat.py`、`verify_opencode.py` 迁移至此
- **Mock NapCatQQ 客户端**（`tests/mock_napcat.py`）：模拟 NapCatQQ 的 WebSocket 客户端，供自动化测试和手动调试共用

### 测试依赖

```
pytest
pytest-asyncio
```

### Mock NapCatQQ 客户端

`tests/mock_napcat.py` 提供 `MockNapCat` 类，模拟 NapCatQQ 的行为：

- 作为 WebSocket 客户端连接到 nochan 服务器
- 连接后自动发送 lifecycle connect 事件（含 `self_id`）
- 提供方法发送各类模拟事件：
  - `send_private_message(user_id, nickname, text)` — 发送私聊消息事件
  - `send_group_message(group_id, group_name, user_id, nickname, text, at_bot=False)` — 发送群聊消息事件
  - `send_heartbeat()` — 发送心跳
- 提供方法接收 nochan 的 API 调用：
  - `recv_api_call()` — 接收下一个 API 请求，自动回复成功响应
  - `get_last_sent_message()` — 获取最近一次 send_msg API 的消息内容

此 mock 既作为 pytest fixture（`conftest.py` 中定义），也可独立运行用于手动调试。

---

## 步骤 1：项目骨架、配置加载、日志系统

**目标**：搭建项目基础结构，让 `uv run python main.py` 能正常启动并输出日志。

**产出文件**：
- `nochan/__init__.py`
- `nochan/config.py` — 配置加载
- `nochan/log.py` — 日志初始化
- `main.py` — 入口（此步骤仅加载配置 + 初始化日志）
- `config.toml` — 默认配置文件
- `tests/test_config.py` — 配置加载的自动化测试

**实现要点**：

`config.py`：
- 用 `tomllib` 读取 `config.toml`，解析为一个 dataclass（`NochanConfig`），包含 `server`、`opencode`、`database`、`logging` 四个子配置
- 配置文件路径通过命令行参数或默认值 `config.toml` 获取

`log.py`：
- 提供 `setup_logging(config)` 函数
- 创建 root logger `nochan`，挂载 `StreamHandler`（控制台）和 `TimedRotatingFileHandler`（文件，按天轮转）
- 自动创建日志目录

`main.py`：
- 读取配置 → 初始化日志 → 打印启动信息 → 退出（后续步骤会加入 WebSocket 服务器）

**自动化测试**（`test_config.py`）：
- 测试加载合法 TOML 配置后各字段值正确
- 测试配置文件不存在时的错误处理
- 测试缺少必填字段时的错误处理

**验证方式**：
```bash
uv run pytest tests/test_config.py
uv run python main.py
# 应输出启动日志到控制台，并生成 data/logs/nochan.log 文件
```

---

## 步骤 2：WebSocket 服务器 + Mock NapCatQQ 客户端

**目标**：nochan 作为 WebSocket 服务器接收连接，解析并分类处理所有收到的 OneBot 事件。同时编写 Mock NapCatQQ 客户端用于自动化测试。

**产出文件**：
- `nochan/server.py` — WebSocket 服务器
- `tests/mock_napcat.py` — 模拟 NapCatQQ 客户端
- `tests/conftest.py` — pytest fixtures
- `tests/test_server.py` — 服务器的自动化测试

**实现要点**：

`server.py`：
- 定义 `NochanServer` 类，持有 WebSocket 服务器实例和当前活跃连接
- `start()` 方法：调用 `websockets.serve(handler, host, port)` 启动服务器
- `handler(websocket)` 方法：逐条接收 JSON 事件，按 `post_type` 分发
  - `meta_event`：记录 lifecycle（从中提取 `self_id` 保存为 bot QQ 号）和 heartbeat
  - `message`：记录日志（本步骤暂不处理，下一步接入）
  - 其他：忽略并记录日志
- `send_api(action, params)` 方法：向 NapCatQQ 发送 API 请求（带 `echo` 字段），通过 `asyncio.Future` 等待响应
- API 响应匹配：收到带 `echo` 的消息时，匹配到对应的 Future 并 set_result

`mock_napcat.py`：
- 实现上文"测试策略"中描述的 `MockNapCat` 类
- 可独立运行：`uv run python tests/mock_napcat.py`，启动后连接到 nochan 并发送测试事件

`main.py` 更新：
- 加载配置 → 初始化日志 → 创建并启动 `NochanServer` → `asyncio.run()` 保持运行

**自动化测试**（`test_server.py`）：
- 测试服务器启动后 MockNapCat 能成功连接
- 测试收到 lifecycle 事件后 `self_id` 被正确记录
- 测试 `send_api()` 能发送请求并收到 MockNapCat 的模拟响应
- 测试收到 message 事件后能正确分类（此步骤仅验证事件被接收，不验证处理逻辑）

**验证方式**：
```bash
uv run pytest tests/test_server.py
# 也可手动验证：一个终端跑 main.py，另一个终端跑 mock_napcat.py
```

---

## 步骤 3：消息转换模块

**目标**：实现 QQ 消息 ↔ AI 文本的双向转换，以及 @bot 检测和指令解析。

**产出文件**：
- `nochan/converter.py` — 消息转换
- `tests/test_converter.py` — 自动化测试

**实现要点**：

`converter.py`：

入站转换（QQ → AI）：
- `parse_message_event(event, bot_id)` 函数：接收 OneBot 消息事件 dict，返回解析结果 dataclass：
  ```python
  @dataclass
  class ParsedMessage:
      chat_id: str           # "private:123" 或 "group:456"
      text: str              # 提取的纯文本
      is_at_bot: bool        # 群聊中是否 @bot
      sender_name: str       # 发送者显示名
      sender_id: int         # 发送者 QQ 号
      group_name: str | None # 群聊名称（私聊为 None）
      message_type: str      # "private" 或 "group"
  ```
- 消息段遍历：`text` 直接提取；`at` 判断 `str(data.qq) == str(bot_id)` 后决定忽略还是转为 `@name`；`image`/`face` 转占位符；其他忽略
- 上下文注入：`build_prompt(parsed)` 函数，在消息文本前附加 `[群聊 xxx(id)，用户 xxx(id)]` 格式的上下文

指令解析：
- `parse_command(text)` 函数：判断文本是否以 `/` 开头，返回指令名或 None
  - `/new` → `"new"`
  - `/help` → `"help"`
  - `/其他` → `"unknown"`
  - 非 `/` 开头 → `None`（普通消息）

出站转换（AI → QQ）：
- `to_onebot_message(text)` 函数：将 AI 回复文本包装为 OneBot 消息段数组
  - v1 直接返回 `[{"type": "text", "data": {"text": text}}]`

**自动化测试**（`test_converter.py`）：
- 测试纯文本私聊消息的解析（chat_id、text、sender_name）
- 测试群聊消息中 @bot 的检测（`data.qq` 为字符串 vs `bot_id` 为 int）
- 测试群聊消息中非 @bot 的情况
- 测试混合消息段（text + at + image）的拼接
- 测试 `build_prompt` 的上下文格式（私聊 vs 群聊）
- 测试指令解析：`/new`、`/help`、`/unknown`、普通文本
- 测试 `to_onebot_message` 的输出格式

---

## 步骤 4：会话管理模块

**目标**：实现基于 SQLite 的会话持久化，支持创建、查询、归档会话。

**产出文件**：
- `nochan/session.py` — 会话管理
- `tests/test_session.py` — 自动化测试

**实现要点**：

`session.py`：
- 定义 `SessionManager` 类，持有 `aiosqlite` 连接
- `init()` 方法：创建数据库和表（`sessions` 表 + 索引），开启 WAL 模式
- `get_active_session(chat_id)` 方法：查询指定 chat_id 的活跃会话，返回 Session 或 None
- `create_session(chat_id)` 方法：创建新会话（UUID, status=active），返回 Session
- `archive_active_session(chat_id)` 方法：将 chat_id 的活跃会话状态改为 archived
- `update_opencode_session_id(session_id, opencode_session_id)` 方法：首次调用 OpenCode 后回填 session ID
- `close()` 方法：关闭数据库连接

Session dataclass：
```python
@dataclass
class Session:
    id: str
    chat_id: str
    opencode_session_id: str | None
    status: str  # "active" / "archived"
    created_at: str
    updated_at: str
```

**自动化测试**（`test_session.py`）：
- 使用内存数据库（`:memory:`）或临时文件，避免测试污染
- 测试 `create_session` 创建后能通过 `get_active_session` 查到
- 测试同一 chat_id 只有一个 active 会话
- 测试 `archive_active_session` 后 `get_active_session` 返回 None
- 测试归档后再创建新会话，新会话成为 active
- 测试 `update_opencode_session_id` 回填成功
- 测试 `init()` 的幂等性（多次调用不报错）

---

## 步骤 5：OpenCode 封装模块

**目标**：实现通过子进程调用 `opencode run --format json` 的封装，包含 JSONL 解析和并发控制。

**产出文件**：
- `nochan/opencode.py` — OpenCode 后端封装
- `tests/test_opencode.py` — 自动化测试（mock subprocess）

**实现要点**：

`opencode.py`：

接口定义：
```python
@dataclass
class OpenCodeResponse:
    session_id: str
    content: str
    success: bool
    error: str | None
```

实现 `SubprocessOpenCodeBackend` 类：
- 构造函数接收配置：`command`、`work_dir`、`max_concurrent`
- 内部持有 `asyncio.Semaphore(max_concurrent)`

`send_message(session_id, message)` 方法：
- 通过 Semaphore 控制并发，提供 `is_queue_full()` 方法供上层判断是否需要提示用户排队
- 构建命令：`[command, "run", "--format", "json"]`，有 session_id 则追加 `["-s", session_id]`，最后追加 message
- 用 `asyncio.create_subprocess_exec` 启动子进程，`cwd` 设为 `work_dir`
- 逐行读取 stdout，解析 JSONL 事件：
  - 从首条事件提取 `sessionID`
  - 收集所有 `text` 事件的 `part.text`
  - 检测 `error` 事件
  - 在 `step_finish` + `reason == "stop"` 时确认完成
- 进程结束后组装 `OpenCodeResponse` 返回

JSONL 解析逻辑建议抽取为独立的内部函数 `_parse_jsonl_events(lines)`，方便单独测试。

**自动化测试**（`test_opencode.py`）：
- **JSONL 解析测试**（不依赖 opencode CLI）：构造模拟的 JSONL 行，测试 `_parse_jsonl_events` 能正确提取 session_id、text、error
- **并发控制测试**：启动 2 个并发请求（max_concurrent=1），验证第 2 个被阻塞
- **错误处理测试**：模拟进程退出码非 0、输出包含 error 事件等场景

> 注意：自动化测试通过 mock subprocess 实现，不实际调用 opencode CLI。真实 CLI 的验证使用 `tests/manual/verify_opencode.py`。

---

## 步骤 6：集成联调 — 串联完整消息流

**目标**：将所有模块串联，实现完整的 QQ 消息 → AI 回复 → QQ 回复流程。

**修改文件**：
- `nochan/server.py` — 集成消息处理逻辑
- `main.py` — 初始化所有模块并注入依赖
- `tests/test_integration.py` — 自动化集成测试

**实现要点**：

`main.py` 更新：
- 初始化顺序：配置 → 日志 → SessionManager → SubprocessOpenCodeBackend → NochanServer
- 将 SessionManager 和 OpenCodeBackend 注入 NochanServer
- 启动服务器

`server.py` 中的消息处理流程：

收到 message 事件后：
1. 调用 `converter.parse_message_event()` 解析消息
2. 群聊且未 @bot → 忽略
3. 调用 `converter.parse_command()` 检查指令
   - `/new` → 调用 `session_manager.archive_active_session()` + `create_session()`，回复确认消息
   - `/help` 或未知指令 → 回复帮助文本
   - 普通消息 → 继续
4. 调用 `session_manager.get_active_session(chat_id)`，无则创建
5. 调用 `converter.build_prompt(parsed)` 构建带上下文的 prompt
6. 检查 OpenCode 队列是否已满，满则先发送排队提示
7. 调用 `opencode_backend.send_message(session.opencode_session_id, prompt)`
8. 如果是新 session（`opencode_session_id` 为 None），用返回的 session_id 更新数据库
9. 调用 `converter.to_onebot_message(response.content)` 转换回复
10. 调用 `send_api("send_private_msg"/"send_group_msg", ...)` 发送回复
11. 错误处理：OpenCode 失败时发送错误提示给用户

辅助方法：
- `reply_text(event, text)` — 根据消息类型（私聊/群聊）自动选择正确的 send API

**自动化集成测试**（`test_integration.py`）：
- 使用 MockNapCat + mock OpenCode subprocess，端到端验证完整流程
- 测试私聊消息 → AI 回复 → 消息发送
- 测试群聊 @bot → AI 回复
- 测试群聊不 @bot → 无响应
- 测试 /new 指令 → 会话归档 + 新会话
- 测试 /help 和无效指令 → 帮助信息
- 测试 OpenCode 失败 → 错误提示发送给用户
- 测试排队提示（并发满时）

**手动验证**（真实环境端到端）：
```bash
uv run python main.py
# 1. NapCatQQ 连接成功
# 2. 私聊发送消息 → 收到 AI 回复
# 3. 群聊 @bot 发送消息 → 收到 AI 回复
# 4. 群聊不 @bot → 无响应
# 5. 发送 /new → 收到"已创建新会话"
# 6. 发送 /help → 收到帮助信息
# 7. 发送 /xyz → 收到帮助信息（无效指令）
# 8. 连续发送多条消息 → 第 3 条开始收到排队提示
# 9. 重启 nochan 后发送消息 → 会话继续（持久化验证）
```

---

## 步骤 7：收尾与清理

**目标**：代码清理、文档补充、确保所有测试通过。

**内容**：
- 确保 `uv run pytest` 全部通过
- 将 `tests/verify_napcat.py` 和 `tests/verify_opencode.py` 迁移到 `tests/manual/`
- 清理 `logs/` 目录下的验证日志（加入 `.gitignore`）
- 检查所有模块的日志输出是否清晰
- 更新 `pyproject.toml`（描述、依赖完整性）
- 编写简要的 README

---

## 步骤间的依赖关系

```
步骤 1（骨架/配置/日志）
  ↓
步骤 2（WebSocket 服务器 + Mock 客户端）
  ↓
步骤 3（消息转换）──┐
步骤 4（会话管理）──┤ ← 这三个互相独立，可并行开发
步骤 5（OpenCode）──┘
  ↓
步骤 6（集成联调）
  ↓
步骤 7（收尾清理）
```

步骤 3、4、5 互相独立，不依赖彼此，只依赖步骤 1 的配置和日志基础设施。但它们都需要在步骤 2 之后开发，因为步骤 2 确立了服务器的基本结构和 mock 客户端。步骤 6 依赖前面所有步骤。步骤 7 在功能完成后进行。
