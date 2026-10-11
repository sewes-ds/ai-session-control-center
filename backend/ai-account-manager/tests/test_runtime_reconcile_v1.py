from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ai_account_manager.core.runtime_control import (
    commit_observed_proposal,
    read_runtime_control_store,
    register_agent_instance,
    upsert_runtime_binding,
)
from ai_account_manager.integrations.runtime_reconcile import reconcile_runtime_outbox_once
from ai_account_manager.integrations.ai_agent_runtime import RuntimeConnectorError
from ai_account_manager.integrations.cus_bot import CusBotDeliveryError
from ai_account_manager.schemas.runtime_control import (
    AgentInstanceRecord,
    DesiredSpecRecord,
    ObservedSpecProposalRecord,
    RuntimeBindingRecord,
    RuntimeUserNotificationRecord,
    canonical_digest,
)


def _binding() -> RuntimeBindingRecord:
    desired_payload: dict[str, object] = {
        "binding_id": "binding-1",
        "agent_instance_id": "agent-1",
        "generation_id": "gen-1",
        "revision": 1,
        "project_id": "project-1",
        "canonical_cwd": "/srv/project",
        "provider": "codex",
        "account_profile": "work",
        "model": "gpt-old",
        "reasoning_effort": "medium",
    }
    return RuntimeBindingRecord(
        binding_id="binding-1",
        agent_instance_id="agent-1",
        chat_id=-1001,
        thread_id=42,
        topic_name="Project",
        project_id="project-1",
        canonical_cwd="/srv/project",
        desired=DesiredSpecRecord(
            **desired_payload,
            spec_digest=canonical_digest(desired_payload),
            source_kind="cus_ui",
            source_ref="user:1",
        ),
    )


def _prepare_store(path: Path) -> RuntimeBindingRecord:
    register_agent_instance(
        path,
        AgentInstanceRecord(
            agent_instance_id="agent-1",
            pinned_upstream_sha="0" * 40,
            telegram_bot_identity="bot-1",
            telegram_bot_token_fingerprint="sha256:" + "1" * 64,
            forum_chat_id=-1001,
            runtime_socket="/run/user/1000/runtime.sock",
        ),
    )
    binding = _binding()
    upsert_runtime_binding(path, binding)
    return binding


def _proposal(
    binding: RuntimeBindingRecord,
    *,
    sequence: int,
    model: str = "gpt-new",
) -> ObservedSpecProposalRecord:
    return ObservedSpecProposalRecord(
        binding_id=binding.binding_id,
        base_generation_id=binding.desired.generation_id,
        base_revision=binding.desired.revision,
        base_spec_digest=binding.desired.spec_digest,
        model=model,
        reasoning_effort="high",
        instance_id="instance-1",
        observation_sequence=sequence,
        evidence_digest="sha256:" + f"{sequence:x}".rjust(64, "0"),
    )


def test_observed_commit_is_idempotent_and_stale_base_cannot_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "runtime-control.json"
    binding = _prepare_store(path)
    proposal = _proposal(binding, sequence=1)
    payload_digest = canonical_digest(proposal.model_dump())

    ack, committed = commit_observed_proposal(
        path,
        event_id=proposal.expected_event_id,
        payload_digest=payload_digest,
        proposal=proposal,
        policy_allows=lambda _binding, _proposal: True,
    )
    assert ack == "ACKED"
    assert committed is not None
    assert committed.desired.revision == 2
    assert committed.desired.model == "gpt-new"

    replay_ack, replayed = commit_observed_proposal(
        path,
        event_id=proposal.expected_event_id,
        payload_digest=payload_digest,
        proposal=proposal,
        policy_allows=lambda _binding, _proposal: True,
    )
    assert replay_ack == "ACKED"
    assert replayed is not None and replayed.desired.revision == 2

    delayed = _proposal(binding, sequence=2, model="gpt-delayed")
    stale_ack, stale_binding = commit_observed_proposal(
        path,
        event_id=delayed.expected_event_id,
        payload_digest=canonical_digest(delayed.model_dump()),
        proposal=delayed,
        policy_allows=lambda _binding, _proposal: True,
    )
    assert stale_ack == "STALE_OBSERVED"
    assert stale_binding is None
    assert read_runtime_control_store(path).bindings["binding-1"].desired.model == "gpt-new"


def test_policy_rejection_does_not_change_desired(tmp_path: Path) -> None:
    path = tmp_path / "runtime-control.json"
    binding = _prepare_store(path)
    proposal = _proposal(binding, sequence=1)
    ack, committed = commit_observed_proposal(
        path,
        event_id=proposal.expected_event_id,
        payload_digest=canonical_digest(proposal.model_dump()),
        proposal=proposal,
        policy_allows=lambda _binding, _proposal: False,
    )
    assert ack == "POLICY_REJECTED"
    assert committed is None
    assert read_runtime_control_store(path).bindings["binding-1"].desired.revision == 1


def test_reconcile_applies_committed_revision_before_runtime_ack(tmp_path: Path) -> None:
    path = tmp_path / "runtime-control.json"
    binding = _prepare_store(path)
    proposal = _proposal(binding, sequence=1)
    event = {
        "event_id": proposal.expected_event_id,
        "kind": "OBSERVED_SPEC_PROPOSAL",
        "binding_id": proposal.binding_id,
        "payload": proposal.model_dump(),
        "payload_digest": canonical_digest(proposal.model_dump()),
        "attempt": 1,
    }

    class FakeConnector:
        def __init__(self) -> None:
            self.operations: list[tuple[str, object]] = []

        async def pull_outbox(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
            assert limit == 50
            return (event,)

        async def apply_binding(
            self,
            applied: RuntimeBindingRecord,
            *,
            request_id: str | None = None,
        ) -> dict[str, Any]:
            self.operations.append(("apply", (applied.desired.revision, request_id)))
            return {"ok": True}

        async def acknowledge_outbox(self, event_id: str, ack_state: str) -> None:
            self.operations.append(("ack", (event_id, ack_state)))

    connector = FakeConnector()

    async def scenario() -> None:
        report = await reconcile_runtime_outbox_once(
            store_path=path,
            connector=connector,  # type: ignore[arg-type]
            policy_allows=lambda _binding, _proposal: True,
        )
        assert report.accepted == 1

    asyncio.run(scenario())
    assert connector.operations[0][0] == "apply"
    assert connector.operations[1] == ("ack", (proposal.expected_event_id, "ACKED"))
    assert read_runtime_control_store(path).bindings["binding-1"].desired.revision == 2


def test_runtime_apply_outage_retries_receipt_without_new_revision(tmp_path: Path) -> None:
    path = tmp_path / "runtime-control.json"
    binding = _prepare_store(path)
    proposal = _proposal(binding, sequence=1)
    event = {
        "event_id": proposal.expected_event_id,
        "kind": "OBSERVED_SPEC_PROPOSAL",
        "binding_id": proposal.binding_id,
        "payload": proposal.model_dump(),
        "payload_digest": canonical_digest(proposal.model_dump()),
        "attempt": 1,
    }

    class FlakyConnector:
        def __init__(self) -> None:
            self.fail = True
            self.acks = 0

        async def pull_outbox(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
            return (event,)

        async def apply_binding(
            self,
            applied: RuntimeBindingRecord,
            *,
            request_id: str | None = None,
        ) -> dict[str, Any]:
            assert applied.desired.revision == 2
            assert request_id == f"observed-apply:{proposal.expected_event_id}"
            if self.fail:
                raise RuntimeConnectorError(
                    "RUNTIME_UNAVAILABLE",
                    "Runtime is unavailable",
                    retryable=True,
                )
            return {"ok": True}

        async def acknowledge_outbox(self, event_id: str, ack_state: str) -> None:
            self.acks += 1

    connector = FlakyConnector()

    async def scenario() -> None:
        try:
            await reconcile_runtime_outbox_once(
                store_path=path,
                connector=connector,  # type: ignore[arg-type]
                policy_allows=lambda _binding, _proposal: True,
            )
        except RuntimeConnectorError as exc:
            assert exc.retryable is True
        else:
            raise AssertionError("Runtime outage must propagate")
        assert connector.acks == 0
        assert read_runtime_control_store(path).bindings["binding-1"].desired.revision == 2

        connector.fail = False
        await reconcile_runtime_outbox_once(
            store_path=path,
            connector=connector,  # type: ignore[arg-type]
            policy_allows=lambda _binding, _proposal: True,
        )

    asyncio.run(scenario())
    assert connector.acks == 1
    assert read_runtime_control_store(path).bindings["binding-1"].desired.revision == 2


def test_superseded_observed_retry_is_acked_without_reapplying_old_revision(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime-control.json"
    binding = _prepare_store(path)
    proposal = _proposal(binding, sequence=1)
    payload_digest = canonical_digest(proposal.model_dump())
    ack, observed_binding = commit_observed_proposal(
        path,
        event_id=proposal.expected_event_id,
        payload_digest=payload_digest,
        proposal=proposal,
        policy_allows=lambda _binding, _proposal: True,
    )
    assert ack == "ACKED"
    assert observed_binding is not None
    newer_payload = {
        **observed_binding.desired.digest_payload(),
        "revision": 3,
        "model": "gpt-ui-newer",
    }
    newer_desired = DesiredSpecRecord(
        **newer_payload,
        spec_digest=canonical_digest(newer_payload),
        source_kind="cus_ui",
        source_ref="user:later",
    )
    upsert_runtime_binding(
        path,
        observed_binding.model_copy(update={"desired": newer_desired}),
    )
    event = {
        "event_id": proposal.expected_event_id,
        "kind": "OBSERVED_SPEC_PROPOSAL",
        "binding_id": proposal.binding_id,
        "payload": proposal.model_dump(),
        "payload_digest": payload_digest,
        "attempt": 2,
    }

    class Connector:
        def __init__(self) -> None:
            self.acks: list[tuple[str, str]] = []

        async def pull_outbox(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
            return (event,)

        async def apply_binding(
            self,
            _binding: RuntimeBindingRecord,
            *,
            request_id: str | None = None,
        ) -> dict[str, Any]:
            raise AssertionError("A superseded observed revision must not be re-applied")

        async def acknowledge_outbox(self, event_id: str, ack_state: str) -> None:
            self.acks.append((event_id, ack_state))

    connector = Connector()

    async def scenario() -> None:
        report = await reconcile_runtime_outbox_once(
            store_path=path,
            connector=connector,  # type: ignore[arg-type]
            policy_allows=lambda _binding, _proposal: True,
        )
        assert report.accepted == 1

    asyncio.run(scenario())
    assert connector.acks == [(proposal.expected_event_id, "ACKED")]
    current = read_runtime_control_store(path).bindings["binding-1"].desired
    assert current.revision == 3
    assert current.model == "gpt-ui-newer"


def test_runtime_event_cannot_cross_agent_instance_namespace(tmp_path: Path) -> None:
    path = tmp_path / "runtime-control.json"
    binding = _prepare_store(path)
    proposal = _proposal(binding, sequence=1)
    event = {
        "event_id": proposal.expected_event_id,
        "kind": "OBSERVED_SPEC_PROPOSAL",
        "binding_id": proposal.binding_id,
        "payload": proposal.model_dump(),
        "payload_digest": canonical_digest(proposal.model_dump()),
        "attempt": 1,
    }

    class WrongNamespaceConnector:
        async def pull_outbox(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
            return (event,)

        async def acknowledge_outbox(self, event_id: str, ack_state: str) -> None:
            raise AssertionError("Cross-namespace event must not be acknowledged")

    async def scenario() -> None:
        with pytest.raises(RuntimeConnectorError) as failure:
            await reconcile_runtime_outbox_once(
                store_path=path,
                connector=WrongNamespaceConnector(),  # type: ignore[arg-type]
                policy_allows=lambda _binding, _proposal: True,
                agent_instance_id="agent-2",
            )
        assert failure.value.code == "RUNTIME_EVENT_NAMESPACE_MISMATCH"
        assert failure.value.retryable is False

    asyncio.run(scenario())
    assert read_runtime_control_store(path).bindings["binding-1"].desired.revision == 1


def _notification_event() -> dict[str, Any]:
    payload = {
        "schema": "ai-agent-runtime.user-notification.v1",
        "notification_code": "RESUME_BINDING_MISMATCH",
        "agent_instance_id": "agent-1",
        "binding_id": "binding-1",
        "chat_id": -1001,
        "thread_id": 42,
    }
    return {
        "event_id": "notice:" + "a" * 64,
        "kind": "USER_NOTIFICATION_REQUIRED",
        "binding_id": "binding-1",
        "payload": payload,
        "payload_digest": canonical_digest(payload),
        "attempt": 1,
    }


def test_runtime_notification_is_delivered_once_and_replay_is_only_acked(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime-control.json"
    _prepare_store(path)
    event = _notification_event()
    sent: list[RuntimeUserNotificationRecord] = []

    class FakeConnector:
        def __init__(self) -> None:
            self.acks: list[tuple[str, str]] = []

        async def pull_outbox(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
            return (event,)

        async def acknowledge_outbox(self, event_id: str, ack_state: str) -> None:
            self.acks.append((event_id, ack_state))

    async def notify(notification: RuntimeUserNotificationRecord) -> int:
        sent.append(notification)
        return 9001

    connector = FakeConnector()

    async def scenario() -> None:
        first = await reconcile_runtime_outbox_once(
            store_path=path,
            connector=connector,  # type: ignore[arg-type]
            policy_allows=lambda _binding, _proposal: True,
            agent_instance_id="agent-1",
            notification_sender=notify,
        )
        assert first.notifications_delivered == 1
        replay = await reconcile_runtime_outbox_once(
            store_path=path,
            connector=connector,  # type: ignore[arg-type]
            policy_allows=lambda _binding, _proposal: True,
            agent_instance_id="agent-1",
            notification_sender=notify,
        )
        assert replay.notifications_delivered == 1

    asyncio.run(scenario())
    assert len(sent) == 1
    assert connector.acks == 2 * [(event["event_id"], "ACKED")]
    receipt = read_runtime_control_store(path).runtime_notification_receipts[event["event_id"]]
    assert receipt.state == "DELIVERED"
    assert receipt.message_id == 9001


def test_ambiguous_notification_delivery_is_not_retried_or_duplicated(tmp_path: Path) -> None:
    path = tmp_path / "runtime-control.json"
    _prepare_store(path)
    event = _notification_event()
    sends = 0

    class FakeConnector:
        def __init__(self) -> None:
            self.acks = 0

        async def pull_outbox(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
            return (event,)

        async def acknowledge_outbox(self, event_id: str, ack_state: str) -> None:
            self.acks += 1

    async def ambiguous(_notification: RuntimeUserNotificationRecord) -> int:
        nonlocal sends
        sends += 1
        raise CusBotDeliveryError(
            "CUS_BOT_DELIVERY_UNAVAILABLE",
            "delivery result is unknown",
            retryable=True,
            delivery_unknown=True,
        )

    connector = FakeConnector()

    async def scenario() -> None:
        first = await reconcile_runtime_outbox_once(
            store_path=path,
            connector=connector,  # type: ignore[arg-type]
            policy_allows=lambda _binding, _proposal: True,
            notification_sender=ambiguous,
        )
        assert first.notifications_delivery_unknown == 1
        replay = await reconcile_runtime_outbox_once(
            store_path=path,
            connector=connector,  # type: ignore[arg-type]
            policy_allows=lambda _binding, _proposal: True,
            notification_sender=ambiguous,
        )
        assert replay.notifications_delivery_unknown == 1

    asyncio.run(scenario())
    assert sends == 1
    assert connector.acks == 2
    receipt = read_runtime_control_store(path).runtime_notification_receipts[event["event_id"]]
    assert receipt.state == "DELIVERY_UNKNOWN"
