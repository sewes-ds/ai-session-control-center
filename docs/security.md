# Security

## Never commit

- environment files with real values;
- provider `auth.json` files or profile directories;
- access tokens, refresh tokens, API keys, or bot tokens;
- SSH keys, certificates, runtime databases, or credential-bearing logs;
- Runtime descriptors or state containing deployment-specific identities.

Store provider credentials only on the server under a private directory, for example
`/srv/ai-session-control-center/profiles`, owned by the service account with mode
`0700`. Store the service environment outside the repository with mode `0600`.

## Client boundary

Mini App and PWA clients receive only safe display data such as provider IDs, account
labels, quota percentages, reset timestamps, and sanitized error states. They must
never receive provider credentials, local profile paths, Runtime socket paths, or
process identifiers.

## Telegram boundary

Validate signed Telegram Web App `initData` on the backend and enforce an explicit
user allowlist. Use a dedicated CUS Bot credential for onboarding; do not reuse the
working Telegram Agent token. Webhook and onboarding secrets should be independently
generated high-entropy values.

## Before publishing or deploying

Run `./scripts/prepush_check.sh`, review `git diff --cached`, and verify that no
generated runtime state is staged. Repository scans supplement review; they do not
make committed credentials safe. Rotate any credential immediately if it has ever
entered Git history.
