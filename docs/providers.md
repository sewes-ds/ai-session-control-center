# Providers

The backend API and Runtime contract are provider-agnostic. Provider-specific behavior
belongs behind an adapter.

## Codex

Codex is the first implemented provider. Its adapter covers isolated profile
directories, server-side authentication status, capability discovery, quota display,
and launch environment resolution. Provider credentials never cross the backend/client
boundary.

## Claude Code

Planned. Authentication, capability, quota, and launcher semantics require a separate
provider audit before implementation. CUS must not report fabricated quota support.

## Gemini CLI

Planned under the same constraints as Claude Code. Runtime support must be added as a
real adapter and qualified independently.
