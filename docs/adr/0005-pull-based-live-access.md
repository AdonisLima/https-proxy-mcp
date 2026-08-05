---
status: accepted
---

# Live access is pull-based; no push to the agent

Agents observe traffic by calling `tail_exchanges` (incremental, cursor-based) and `wait_for_exchanges` (blocking long-poll). The server never pushes Exchanges at the agent.

MCP does define resource subscriptions and server notifications, but whether a given client surfaces them into a model's context mid-turn varies by client, and the research corpus behind this project covers MCP transports only — it has nothing on the notification primitives. Designing the core interaction model on a mechanism whose delivery cannot be relied on would be a mistake.

`wait_for_exchanges` covers the case that actually matters — "do this, then wait for that request" — without any push at all, and without a polling loop.

## Consequences

A human wanting a live view uses `mitmweb` against the same capture, which drives its UI from the proxy core over REST and a WebSocket; that costs us nothing to support. Other consumers can tail the Session's `exchanges.ndjson`. If MCP notifications later prove reliable across clients, they can be added as an enhancement without changing these tools.
