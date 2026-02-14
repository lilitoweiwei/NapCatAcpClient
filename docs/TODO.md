# ncat TODO

## Done
- [x] Rename project to ncat (NapCat ACP Client)
- [x] Replace OpenCode backend with ACP protocol
- [x] Remove SQLite session persistence (in-memory only)
- [x] System prompt (now handled by agent)
- [x] Immediate notification after receiving a message (before AI finishes)

## Planned
- [x] Forward permission requests to QQ user for interactive approval
- [ ] 添加`/send`指令用于将消息原封不动转发给agent，从而能够调用agent自己的slash command
- [ ] Expose NapCat capabilities as MCP server for agent
- [ ] Image support
- [ ] Agent process auto-restart on crash
- [ ] Smarter group filtering (listen to all messages, reply when relevant)
- [ ] Configurable context header (currently hardcoded)
