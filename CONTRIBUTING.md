# Contributing

Contributions are welcome through GitHub issues and pull requests.

Before a substantial change, open an issue describing the problem, proposed API or
behavior, trust-boundary impact, and verification plan. Keep the backend and Runtime
contract provider-agnostic. Provider logic belongs in backend adapters; Telegram
authentication belongs in its auth adapter; Mini App and PWA code remain clients.
Do not introduce a direct dependency on Telegram Agent internals.

Never include credentials, provider profiles, bot tokens, environment files,
deployment state, private paths, or customer data in an issue, commit, test fixture,
or log excerpt.

## Development checks

```bash
cd backend/ai-account-manager
uv sync --locked --extra dev
uv run ruff check src tests
uv run pytest -q
cd ../..
./scripts/prepush_check.sh
node --check frontend/telegram-miniapp/assets/app.js
```

A pull request should explain what changed, why the change is safe, and which checks
were run. New behavior should include tests. Do not claim support for a provider,
quota source, or deployment tuple that has not been independently qualified.
