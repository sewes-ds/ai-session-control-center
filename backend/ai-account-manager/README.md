# AI Account Manager backend

Provider-agnostic FastAPI backend for the CUS Mini App and future clients.
Codex is the first implemented provider; Claude Code and Gemini CLI are planned.

Runtime reconciliation is opt-in and configured only through the server environment:

- `RUNTIME_RECONCILE_ENABLED=true` enables the application-lifespan worker;
- `RUNTIME_RECONCILE_INTERVAL_SECONDS` sets the bounded poll interval;
- `RUNTIME_RECONCILE_BATCH_SIZE` bounds each AgentInstance outbox claim;
- `RUNTIME_CONTROL_PATH` points to the durable CUS Runtime control store.

The worker communicates with each registered Runtime over its local Unix socket.
It does not place CUS or Telegram network calls in the provider wrapper startup path.
