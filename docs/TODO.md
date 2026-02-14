# ncat TODO

## Done
- [x] Rename project to ncat (NapCat ACP Client)
- [x] Replace OpenCode backend with ACP protocol
- [x] Remove SQLite session persistence (in-memory only)
- [x] System prompt (now handled by agent)
- [x] Immediate notification after receiving a message (before AI finishes)

## Planned
- [x] Forward permission requests to QQ user for interactive approval
- [x] 添加`/send`指令用于将消息原封不动转发给agent，从而能够调用agent自己的slash command
- [x] 重构：将AgentManager独立到一个新文件中
- [x] 更新一下`README.md`
- [x] Image support
- [ ] Expose NapCat capabilities as MCP server for agent
- [ ] agent->qq：允许分段发送消息，当等待时间超过一定时间后就将已经积累的消息发送出去
- [ ] AI思考引起的超时反馈机制的改善：现在会分段发送消息了，所以只有当距离上一条AI发送的消息过太久了才会进行超时反馈
- [ ] Agent process auto-restart on crash
- [ ] Smarter group filtering (listen to all messages, reply when relevant)
- [ ] Configurable context header (currently hardcoded)
