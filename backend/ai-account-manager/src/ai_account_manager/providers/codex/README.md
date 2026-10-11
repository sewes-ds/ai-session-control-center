# Codex provider

Planned MVP provider.

Responsibilities:

- profile directory layout
- `CODEX_HOME` resolver
- server-side `auth.json` handling
- token refresh
- quota refresh
- safe public quota DTO for Mini App/PWA

## Approved OpenViking defaults for new accounts

During Codex account enrollment, CUS copies only the reviewed OpenViking
`0.11.2` plugin package from `CODEX_OPENVIKING_SEED_ACCOUNT` (default:
`codex_a`) into the staged profile. It checks the complete package digest,
adds the exact eight approved hook hashes and enables the plugin before the
profile is published. No credential or session files are copied from the seed
account. A missing/changed package or conflicting Codex config fails enrollment
closed; it never enables `--dangerously-bypass-hook-trust`.

The pin covers the currently approved plugin package, not future updates. A
new OpenViking release or changed hook must be reviewed and requalified before
its digest/hashes are changed here. Existing profiles are not rewritten by the
enrollment path. This is a Codex-only adapter; the CUS API remains
provider-agnostic.
