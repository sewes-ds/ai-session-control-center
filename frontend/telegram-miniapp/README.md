# Telegram Mini App

The production client for CUS account, quota, project, Runtime health, and topic
onboarding workflows.

The Mini App is a client only. Provider logic, Runtime reconciliation, Telegram
authorization, and secrets belong to backend adapters. Topic onboarding uses a
short-lived opaque `startapp` token and completes only after Runtime acknowledges
the binding and the CUS Bot confirms delivery in the same topic.

Codex is currently implemented. Claude Code and Gemini CLI must not be presented as
working providers before their adapters are implemented and independently qualified.

See [architecture](../../docs/architecture.md) and
[Runtime integration](../../docs/runtime-integration.md).
