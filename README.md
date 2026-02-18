# ncat (NapCat ACP Client)

一个 Python 桥接器，将 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 连接到任何支持 [ACP](https://agentclientprotocol.com/) 的 AI 智能体 —— 让你通过 QQ 与 AI 编程助手聊天。

## 工作原理

ncat 作为一个 **ACP 客户端**：它以子进程方式启动兼容 ACP 的智能体，通过标准输入/输出 (stdin/stdout) 进行 ACP 协议通信，并将来自 QQ（通过 NapCatQQ）的用户消息桥接到智能体。

```
QQ 用户 → NapCat (反向 WS) → ncat → ACP 智能体 (stdin/stdout)
QQ 用户 ← NapCat (反向 WS) ← ncat ← ACP 智能体 (stdin/stdout)
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
```

ncat 会先启动 WebSocket 服务；Agent 连接在后台按固定间隔重试。若 Agent 尚未就绪，发消息或发送 `/new` 会收到提示：「Agent 未连接，请稍后再试。」

**可选配置**（`config.toml` 的 `[agent]` 下）：
- `initialize_timeout_seconds`：单次 ACP Initialize 等待超时（秒），默认 30。
- `retry_interval_seconds`：连接失败或断开后，下次重试的间隔（秒），默认 10。

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
