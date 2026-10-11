# Architecture

CUS is the source of truth for desired project and binding configuration. It does
not own Telegram message delivery or provider processes.

## Components and ownership

| Component | Owns | Must not own |
| --- | --- | --- |
| CUS backend | authorization, project inventory, provider metadata, desired binding revisions, reconciliation | Telegram delivery loops, provider process lifecycle |
| CUS Bot | topic onboarding notices and completion messages | provider credentials or process control |
| Mini App / future PWA | user interaction through the backend API | provider logic, secrets, direct Runtime access |
| AI Agent Runtime | local Unix-socket protocol, process lifecycle, last-known-good launch state | Telegram API calls, global CUS policy |
| Telegram Agent | topic input/output and conversation transport | account policy or CUS state |
| Provider adapter | provider-specific profile, capability, quota, and launcher behavior | Telegram-specific behavior |

## Runtime V1 data flow

1. A Telegram topic is mapped to one immutable Runtime binding.
2. CUS validates an authorized change and commits a new desired revision.
3. CUS sends the complete desired snapshot to the Runtime over a local Unix socket.
4. Runtime validates the generation, revision, digest, and binding identity before
   persisting the snapshot atomically.
5. Runtime applies changes at a safe process boundary and reports sanitized health
   and observations to CUS.
6. The Mini App reads CUS state; it never talks to Runtime or a provider directly.

Runtime outages fail closed: CUS keeps the desired revision pending and retries
reconciliation. It does not silently choose a default account.

## Trust boundaries

Telegram Mini App launch data is verified server-side. The dedicated CUS Bot token
is separate from the working Telegram Agent token. Provider credentials remain in
server-side profile directories and are represented to clients only by safe IDs and
status metadata.

The Runtime transport is local-only, versioned, bounded, and request-ID checked.
See [Runtime integration](runtime-integration.md) for the protocol-level model.
