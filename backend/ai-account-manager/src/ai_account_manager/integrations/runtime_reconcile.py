from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ai_account_manager.core.runtime_control import (
    commit_observed_proposal,
    finish_runtime_notification,
    read_runtime_control_store,
    release_runtime_notification_reservation,
    reserve_runtime_notification,
)
from ai_account_manager.integrations.ai_agent_runtime import (
    RuntimeConnector,
    RuntimeConnectorError,
)
from ai_account_manager.integrations.cus_bot import CusBotDeliveryError
from ai_account_manager.schemas.runtime_control import (
    ObservedSpecProposalRecord,
    RuntimeBindingRecord,
    RuntimeUserNotificationRecord,
)

ObservedPolicy = Callable[[RuntimeBindingRecord, ObservedSpecProposalRecord], bool]
NotificationSender = Callable[[RuntimeUserNotificationRecord], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class RuntimeReconcileReport:
    claimed: int
    accepted: int
    stale: int
    policy_rejected: int
    notifications_delivered: int
    notifications_delivery_unknown: int
    notifications_rejected: int
    deferred_other_events: int


async def reconcile_runtime_outbox_once(
    *,
    store_path: Path,
    connector: RuntimeConnector,
    policy_allows: ObservedPolicy,
    limit: int = 50,
    agent_instance_id: str | None = None,
    notification_sender: NotificationSender | None = None,
) -> RuntimeReconcileReport:
    """Reconcile observed proposals and binding-scoped CUS Bot notifications."""

    events = await connector.pull_outbox(limit=limit)
    accepted = 0
    stale = 0
    rejected = 0
    notified = 0
    notification_unknown = 0
    notification_rejected = 0
    deferred = 0
    for event in events:
        kind = event.get("kind")
        if kind == "USER_NOTIFICATION_REQUIRED":
            if notification_sender is None:
                deferred += 1
                continue
            outcome = await _process_user_notification(
                store_path=store_path,
                connector=connector,
                event=event,
                sender=notification_sender,
                agent_instance_id=agent_instance_id,
            )
            if outcome == "DELIVERED":
                notified += 1
            elif outcome == "DELIVERY_UNKNOWN":
                notification_unknown += 1
            elif outcome == "REJECTED":
                notification_rejected += 1
            continue
        if kind != "OBSERVED_SPEC_PROPOSAL":
            deferred += 1
            continue
        event_id = _required_event_string(event, "event_id")
        payload_digest = _required_event_string(event, "payload_digest")
        raw_payload = event.get("payload")
        if not isinstance(raw_payload, dict):
            raise RuntimeConnectorError(
                "INVALID_RUNTIME_EVENT",
                "Runtime observed event payload must be an object",
                retryable=False,
            )
        try:
            proposal = ObservedSpecProposalRecord.model_validate(raw_payload)
        except ValidationError as exc:
            raise RuntimeConnectorError(
                "INVALID_RUNTIME_EVENT",
                "Runtime observed event payload is invalid",
                retryable=False,
            ) from exc
        if agent_instance_id is not None:
            binding = read_runtime_control_store(store_path).bindings.get(proposal.binding_id)
            if binding is None or binding.agent_instance_id != agent_instance_id:
                raise RuntimeConnectorError(
                    "RUNTIME_EVENT_NAMESPACE_MISMATCH",
                    "Runtime event does not belong to this AgentInstance",
                    retryable=False,
                )
        ack_state, binding = commit_observed_proposal(
            store_path,
            event_id=event_id,
            payload_digest=payload_digest,
            proposal=proposal,
            policy_allows=policy_allows,
        )
        if ack_state == "ACKED":
            if binding is not None:
                await connector.apply_binding(
                    binding,
                    request_id=f"observed-apply:{event_id}",
                )
            accepted += 1
        elif ack_state == "STALE_OBSERVED":
            stale += 1
        else:
            rejected += 1
        await connector.acknowledge_outbox(event_id, ack_state)
    return RuntimeReconcileReport(
        claimed=len(events),
        accepted=accepted,
        stale=stale,
        policy_rejected=rejected,
        notifications_delivered=notified,
        notifications_delivery_unknown=notification_unknown,
        notifications_rejected=notification_rejected,
        deferred_other_events=deferred,
    )


async def _process_user_notification(
    *,
    store_path: Path,
    connector: RuntimeConnector,
    event: dict[str, Any],
    sender: NotificationSender,
    agent_instance_id: str | None,
) -> str:
    event_id = _required_event_string(event, "event_id")
    payload_digest = _required_event_string(event, "payload_digest")
    raw_payload = event.get("payload")
    if not isinstance(raw_payload, dict):
        raise RuntimeConnectorError(
            "INVALID_RUNTIME_EVENT",
            "Runtime notification payload must be an object",
            retryable=False,
        )
    try:
        notification = RuntimeUserNotificationRecord.model_validate(raw_payload)
    except ValidationError as exc:
        raise RuntimeConnectorError(
            "INVALID_RUNTIME_EVENT",
            "Runtime notification payload is invalid",
            retryable=False,
        ) from exc
    if agent_instance_id is not None and notification.agent_instance_id != agent_instance_id:
        raise RuntimeConnectorError(
            "RUNTIME_EVENT_NAMESPACE_MISMATCH",
            "Runtime notification does not belong to this AgentInstance",
            retryable=False,
        )
    receipt, _binding, reserved = reserve_runtime_notification(
        store_path,
        event_id=event_id,
        payload_digest=payload_digest,
        notification=notification,
    )
    if not reserved:
        if receipt.state == "RESERVED":
            receipt = finish_runtime_notification(
                store_path,
                event_id=event_id,
                state="DELIVERY_UNKNOWN",
            )
        await connector.acknowledge_outbox(event_id, "ACKED")
        return receipt.state
    try:
        message_id = await sender(notification)
    except CusBotDeliveryError as exc:
        if exc.delivery_unknown:
            finish_runtime_notification(
                store_path,
                event_id=event_id,
                state="DELIVERY_UNKNOWN",
            )
            await connector.acknowledge_outbox(event_id, "ACKED")
            return "DELIVERY_UNKNOWN"
        if exc.retryable:
            release_runtime_notification_reservation(store_path, event_id=event_id)
            raise RuntimeConnectorError(exc.code, exc.message, retryable=True) from exc
        finish_runtime_notification(
            store_path,
            event_id=event_id,
            state="REJECTED",
        )
        await connector.acknowledge_outbox(event_id, "ACKED")
        return "REJECTED"
    finish_runtime_notification(
        store_path,
        event_id=event_id,
        state="DELIVERED",
        message_id=message_id,
    )
    await connector.acknowledge_outbox(event_id, "ACKED")
    return "DELIVERED"


def _required_event_string(event: dict[str, Any], key: str) -> str:
    value = event.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeConnectorError(
            "INVALID_RUNTIME_EVENT",
            f"Runtime event {key} must be a non-empty string",
            retryable=False,
        )
    return value
