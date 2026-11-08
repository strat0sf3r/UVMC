# UVMC extension for Claude Desktop

`uvmc.mcpb` is a desktop extension (MCP Bundle) that lets Claude Desktop hand tasks to the
agent in the UVMC machine. `connect-claude-apps` installs it; you can also drag `uvmc.mcpb`
into the Claude Desktop window. In UVMC, switch it on: panel, Settings, "Claude apps on your PC".

`server/index.js` is a small relay (plain Node, no dependencies; Claude Desktop brings Node).
It finds the machine and its panel token through Multipass each time it starts, and passes
MCP messages to the supervisor's `POST /mcp`. Nothing secret is stored in the extension.

Rebuild after changing it:

    npx @anthropic-ai/mcpb pack extension extension/uvmc.mcpb
