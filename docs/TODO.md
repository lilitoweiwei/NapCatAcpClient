# ncat TODO

## Done
- [x] Rename project to ncat (NapCat ACP Client)
- [x] Replace OpenCode backend with ACP protocol
- [x] Remove SQLite session persistence (in-memory only)
- [x] System prompt (now handled by agent)
- [x] Immediate notification after receiving a message (before AI finishes)

## Planned
- [ ] Forward permission requests to QQ user for interactive approval
- [ ] Expose NapCat capabilities as MCP server for agent
- [ ] Image support
- [ ] Agent process auto-restart on crash
- [ ] Smarter group filtering (listen to all messages, reply when relevant)
- [ ] Memory mechanism
- [ ] Configurable context header (currently hardcoded)
