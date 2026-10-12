# AI Agent Runtime V1 integration

CUS communicates with the separate
[`ai-agent-runtime`](https://github.com/sewes-ds/ai-agent-runtime) service through a
framed, protocol-major-versioned Unix-socket adapter. Neither repository imports
the other, and CUS does not patch the Telegram Agent.

## Identity and desired state

An `AgentInstanceRecord` identifies one working Telegram Agent, one forum, and one
Runtime socket. A `RuntimeBindingRecord` identifies one Telegram topic independently
from a project path. Two topics may share a canonical working directory while
retaining different accounts, models, or reasoning settings.

Desired state carries a generation, monotonic revision, and canonical SHA-256
digest. Runtime rejects revision rollback, digest conflicts, incomplete snapshots,
and cross-instance events. Credentials and bot tokens are never part of the Runtime
protocol.

## Reconciliation

The optional CUS reconciliation loop polls active Runtime instances independently.
It repairs missed desired-state pushes, renews the CUS synchronization lease,
processes provider-validated observations, and records notification delivery state.
A failed Runtime instance does not block other namespaces.

Set `RUNTIME_RECONCILE_ENABLED=true` only after all required CUS Bot secrets,
allowlisted forum IDs, and reviewed AgentInstance descriptors are configured.
Runtime sockets and descriptors are deployment state and must not be committed.

## Onboarding

Topic onboarding uses a short-lived opaque deep-link token whose digest, exact chat,
and thread are persisted by CUS. The backend verifies signed Telegram Mini App data
and current forum membership. A binding is marked configured only after Runtime
acknowledges the desired state and the CUS Bot confirms completion delivery.

## Failure behavior

Runtime outage returns an explicit retryable or pending state. CUS does not fall back
to a default provider profile. Notification outcomes distinguish confirmed delivery,
definite retryable failure, and delivery-unknown network outcomes so retries do not
claim stronger guarantees than Telegram provides.
