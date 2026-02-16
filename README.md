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

然后配置 NapCatQQ 连接到 ncat 的 WebSocket 服务器（默认：`ws://127.0.0.1:8282`）。

## 部署为系统服务 (Systemd)

在 Linux 上，你可以将 ncat 部署为自动随系统启动的服务：

```bash
sudo bash scripts/install-service.sh
```

安装后：
- 启动服务: `sudo systemctl start ncat`
- 查看状态: `sudo systemctl status ncat`
- 实时日志: `journalctl -u ncat -f`
