# AI Session Control Center

AI Session Control Center (CUS) is a provider-agnostic control plane for AI CLI
agents. It manages projects, provider accounts, desired model settings, Telegram
topic onboarding, and synchronization with a separate local runtime.

This public repository starts with the Runtime V1 architecture. The previous
combined Telegram overlay and its Git history are intentionally not published.
A small set of compatibility API modules remains because the current Mini App
still uses those endpoints while migration to binding-scoped APIs is completed.

## Architecture

```text
Telegram topic ──> Telegram Agent ──> AI Agent Runtime ──> provider CLI
                         ^                    ^
                         │                    │ Unix socket
                    messages only      desired/observed state
                                              ^
                                              │
CUS Bot / Mini App ──> CUS backend ───────────┘
```

The backend owns desired configuration and authorization policy. The Runtime owns
local process control and last-known-good execution state. Telegram-specific
authentication is isolated from provider credentials. The Mini App is a client;
it never receives provider tokens.

Codex is the first implemented provider. Claude Code and Gemini CLI adapters are
planned, but their quota support is not claimed or simulated.

See [architecture](docs/architecture.md), [Runtime integration](docs/runtime-integration.md),
[providers](docs/providers.md), and [security](docs/security.md).

## Local development

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
cd backend/ai-account-manager
uv sync --locked --extra dev
uv run uvicorn ai_account_manager.main:app --host 127.0.0.1 --port 8710
```

Runtime reconciliation is disabled by default. Copy `.env.example` to a protected
location outside the repository, set the required values, and load that file from
your service manager. Do not commit the resulting environment file.

## Verification

```bash
cd backend/ai-account-manager
uv run ruff check src tests
uv run pytest -q
cd ../..
./scripts/prepush_check.sh
node --check frontend/telegram-miniapp/assets/app.js
```

The companion runtime is maintained in
[`SeweS01/ai-agent-runtime`](https://github.com/SeweS01/ai-agent-runtime).
