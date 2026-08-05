# Python single process, despite the TypeScript convention in common/

mitmproxy is a Python library, so running the capture addon and the MCP server in one Python process lets the addon hold native flow objects — no IPC, no bridge protocol to design and version, and no file round-trip for live queries.

Both sibling projects in `common/` are TypeScript (`figma-console-mcp`, `react-native-sdd`), and `figma-console-mcp` already establishes a pattern for bridging an MCP server to an external process over WebSocket, so TypeScript was the conventional choice and was considered seriously.

## Considered Options

- **TypeScript MCP + Python capture sidecar** over a channel we define. Keeps the repo convention and still gets native flow access, at the cost of two languages and a protocol to own.
- **TypeScript driving mitmweb's HTTP/WebSocket API.** One language, but builds on an undocumented, unversioned API surface that can break on any mitmproxy release.
- **TypeScript reading HAR/flow files from disk.** No bridge and no undocumented API, but every live query becomes a file poll, which makes `tail_exchanges` and `wait_for_exchanges` structurally worse.

## Consequences

The repo needs `uv` to manage a modern Python, since system Python is 3.9.6. Anyone moving between this project and its siblings switches language and toolchain. Accepted deliberately: the data path is the product here, and every alternative degraded it to preserve a convention.
