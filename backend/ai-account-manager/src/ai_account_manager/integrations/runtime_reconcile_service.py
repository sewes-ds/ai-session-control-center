from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ai_account_manager.core.config import AppSettings
from ai_account_manager.core.project_settings import (
    ProjectSettingsError,
    validate_provider_project_settings,
)
from ai_account_manager.core.runtime_control import (
    acknowledge_project_relocation,
    read_runtime_control_store,
)
from ai_account_manager.core.runtime_onboarding import (
    RuntimeOnboardingError,
    renew_binding_onboarding_token,
)
from ai_account_manager.integrations.ai_agent_runtime import (
    RuntimeConnector,
    RuntimeConnectorError,
)
from ai_account_manager.integrations.cus_bot import (
    CusBotDeliveryError,
    send_topic_notification,
)
from ai_account_manager.integrations.cus_bot_onboarding import ensure_topic_setup_notice
from ai_account_manager.integrations.runtime_reconcile import (
    RuntimeReconcileReport,
    reconcile_runtime_outbox_once,
)
from ai_account_manager.schemas.runtime_control import (
    ObservedSpecProposalRecord,
    RuntimeBindingRecord,
    RuntimeUserNotificationRecord,
    canonical_digest,
)

logger = logging.getLogger(__name__)
notice_logger = logging.getLogger("uvicorn.error.cus_bot")

ConnectorFactory = Callable[..., RuntimeConnector]


@dataclass(frozen=True, slots=True)
class AgentReconcileResult:
    agent_instance_id: str
    status: str
    report: RuntimeReconcileReport | None = None
    error_code: str | None = None
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeReconcileCycle:
    agents: tuple[AgentReconcileResult, ...]

    @property
    def healthy(self) -> bool:
        return all(result.status == "OK" for result in self.agents)


async def reconcile_pending_project_relocations(
    *,
    store_path: Path,
    agent_instance_id: str,
    connector: RuntimeConnector,
) -> int:
    """Retry explicit full-snapshot relocations until each Runtime namespace ACKs."""

    store = read_runtime_control_store(store_path)
    pending = tuple(
        sorted(
            (
                relocation
                for relocation in store.project_relocations.values()
                if agent_instance_id in relocation.pending_agent_instance_ids
                and agent_instance_id not in relocation.acknowledged_agent_instance_ids
            ),
            key=lambda relocation: relocation.relocation_id,
        )
    )
    if not pending:
        return 0
    bindings = tuple(
        sorted(
            (
                binding
                for binding in store.bindings.values()
                if binding.agent_instance_id == agent_instance_id
            ),
            key=lambda binding: binding.binding_id,
        )
    )
    active_generations = {
        binding.desired.generation_id
        for binding in bindings
        if binding.lifecycle_status != "ARCHIVED"
    }
    if not bindings or len(active_generations) != 1:
        raise RuntimeConnectorError(
            "INVALID_CUS_STATE",
            "Pending project relocation requires one complete desired generation",
            retryable=False,
        )
    generation_id = next(iter(active_generations))
    request_material: dict[str, object] = {
        "agent_instance_id": agent_instance_id,
        "generation_id": generation_id,
        "relocation_ids": [relocation.relocation_id for relocation in pending],
        "binding_digests": [
            [binding.binding_id, binding.desired.spec_digest] for binding in bindings
        ],
    }
    request_id = f"relocation-apply:{canonical_digest(request_material)[7:]}"
    try:
        await connector.apply_snapshot(
            agent_instance_id=agent_instance_id,
            generation_id=generation_id,
            bindings=bindings,
            project_relocations=pending,
            request_id=request_id,
        )
    except RuntimeConnectorError as exc:
        if exc.code == "PROJECT_RELOCATION_REQUIRED":
            raise RuntimeConnectorError(exc.code, exc.message, retryable=True) from exc
        raise
    for relocation in pending:
        acknowledge_project_relocation(
            store_path,
            relocation_id=relocation.relocation_id,
            agent_instance_id=agent_instance_id,
        )
    return len(pending)


async def reconcile_desired_state_once(
    *,
    store_path: Path,
    agent_instance_id: str,
    connector: RuntimeConnector,
) -> int:
    """Repair a missed CUS push from sanitized Runtime status without using cwd identity."""

    store = read_runtime_control_store(store_path)
    bindings = tuple(
        sorted(
            (
                binding
                for binding in store.bindings.values()
                if binding.agent_instance_id == agent_instance_id
            ),
            key=lambda binding: binding.binding_id,
        )
    )
    if not bindings:
        return 0
    response = await connector.status()
    raw_status = response.get("status")
    if not isinstance(raw_status, dict) or raw_status.get("agent_instance_id") != agent_instance_id:
        raise RuntimeConnectorError(
            "INVALID_RUNTIME_RESPONSE",
            "Runtime status namespace does not match CUS AgentInstance",
            retryable=False,
        )
    raw_runtime_bindings = raw_status.get("bindings")
    if not isinstance(raw_runtime_bindings, list):
        raise RuntimeConnectorError(
            "INVALID_RUNTIME_RESPONSE",
            "Runtime status bindings are malformed",
            retryable=False,
        )
    runtime_by_id: dict[str, dict[str, Any]] = {}
    for raw_binding in raw_runtime_bindings:
        if not isinstance(raw_binding, dict):
            raise RuntimeConnectorError(
                "INVALID_RUNTIME_RESPONSE",
                "Runtime status binding is malformed",
                retryable=False,
            )
        binding_id = raw_binding.get("binding_id")
        if not isinstance(binding_id, str) or not binding_id or binding_id in runtime_by_id:
            raise RuntimeConnectorError(
                "INVALID_RUNTIME_RESPONSE",
                "Runtime status binding identity is missing or duplicated",
                retryable=False,
            )
        runtime_by_id[binding_id] = raw_binding
    cus_ids = {binding.binding_id for binding in bindings}
    if set(runtime_by_id) - cus_ids:
        raise RuntimeConnectorError(
            "RUNTIME_INVENTORY_DIVERGED",
            "Runtime contains a binding absent from the authoritative CUS inventory",
            retryable=False,
        )

    incremental: list[RuntimeBindingRecord] = []
    full_snapshot_required = False
    for binding in bindings:
        observed = runtime_by_id.get(binding.binding_id)
        if binding.lifecycle_status == "UNCONFIGURED":
            if observed is not None:
                raise RuntimeConnectorError(
                    "RUNTIME_INVENTORY_DIVERGED",
                    "Runtime contains an unconfigured CUS binding",
                    retryable=False,
                )
            continue
        if binding.lifecycle_status == "ARCHIVED":
            if observed is not None and observed.get("configured") is not False:
                full_snapshot_required = True
            continue
        expected_mode = "UNMANAGED" if binding.lifecycle_status == "UNMANAGED" else "MANAGED"
        raw_desired = observed.get("desired") if observed is not None else None
        observed_digest = raw_desired.get("spec_digest") if isinstance(raw_desired, dict) else None
        observed_generation = (
            raw_desired.get("generation_id") if isinstance(raw_desired, dict) else None
        )
        identity_matches = (
            observed is not None
            and observed.get("project_id") == binding.project_id
            and observed.get("canonical_cwd") == binding.canonical_cwd
        )
        state_matches = (
            observed is not None
            and observed.get("configured") is True
            and observed.get("management_mode") == expected_mode
            and observed_digest == binding.desired.spec_digest
        )
        if identity_matches and state_matches:
            continue
        if binding.lifecycle_status == "CONFIGURED" and (
            observed_generation in {None, binding.desired.generation_id}
        ):
            incremental.append(binding)
        else:
            full_snapshot_required = True

    if not incremental and not full_snapshot_required:
        return 0
    active_generations = {
        binding.desired.generation_id
        for binding in bindings
        if binding.lifecycle_status != "ARCHIVED"
    }
    if len(active_generations) != 1:
        raise RuntimeConnectorError(
            "INVALID_CUS_STATE",
            "Desired reconcile requires one complete AgentInstance generation",
            retryable=False,
        )
    if full_snapshot_required:
        generation_id = next(iter(active_generations))
        snapshot_bindings = tuple(
            binding for binding in bindings if binding.lifecycle_status != "UNCONFIGURED"
        )
        material: dict[str, object] = {
            "agent_instance_id": agent_instance_id,
            "generation_id": generation_id,
            "binding_digests": [
                [binding.binding_id, binding.desired.spec_digest] for binding in snapshot_bindings
            ],
        }
        await connector.apply_snapshot(
            agent_instance_id=agent_instance_id,
            generation_id=generation_id,
            bindings=snapshot_bindings,
            request_id=f"desired-snapshot:{canonical_digest(material)[7:]}",
        )
        return 1
    for binding in incremental:
        await connector.apply_binding(
            binding,
            request_id=f"desired-apply:{binding.binding_id}:{binding.desired.spec_digest[7:]}",
        )
    return len(incremental)


def observed_policy(
    settings: AppSettings,
) -> Callable[[RuntimeBindingRecord, ObservedSpecProposalRecord], bool]:
    """Use the provider adapter's cached capabilities as the policy authority."""

    def allows(
        binding: RuntimeBindingRecord,
        proposal: ObservedSpecProposalRecord,
    ) -> bool:
        try:
            validation = validate_provider_project_settings(
                provider_id=binding.desired.provider,
                account_profile=binding.desired.account_profile,
                model=proposal.model,
                reasoning_effort=proposal.reasoning_effort,
                settings=settings,
            )
        except ProjectSettingsError:
            return False
        return validation.status == "valid"

    return allows


def notification_sender(
    settings: AppSettings,
) -> Callable[[RuntimeUserNotificationRecord], Awaitable[int]]:
    async def send(notification: RuntimeUserNotificationRecord) -> int:
        reply_markup: dict[str, Any] | None = None
        if notification.notification_code == "ONBOARDING_REQUIRED":
            if not settings.cus_miniapp_deep_link_base or not settings.cus_onboarding_token_key:
                raise CusBotDeliveryError(
                    "CUS_BOT_NOT_CONFIGURED",
                    "CUS Bot onboarding is not configured",
                    retryable=True,
                )
            try:
                issued = renew_binding_onboarding_token(
                    settings.runtime_control_path,
                    binding_id=notification.binding_id,
                    ttl_seconds=settings.cus_onboarding_token_ttl_seconds,
                )
            except RuntimeOnboardingError as exc:
                raise CusBotDeliveryError(
                    exc.code,
                    exc.message,
                    retryable=False,
                ) from exc
            deep_link = f"{settings.cus_miniapp_deep_link_base}{quote(issued.token, safe='')}"
            text = "Проект ещё не настроен"
            reply_markup = {
                "inline_keyboard": [
                    [{"text": "Настроить в ЦУС", "url": deep_link}],
                ]
            }
        elif notification.notification_code == "RESUME_BINDING_MISMATCH":
            text = (
                "Эта сессия принадлежит другому Telegram topic и не была запущена. "
                "Выберите сессию, созданную в текущем topic."
            )
        else:
            text = (
                "Режим Codex subprocess не поддерживается. Codex не был запущен. "
                "Верните режим tmux."
            )
        return await send_topic_notification(
            bot_token=settings.cus_telegram_bot_token,
            chat_id=notification.chat_id,
            thread_id=notification.thread_id,
            text=text,
            reply_markup=reply_markup,
            timeout_seconds=settings.runtime_connector_timeout_seconds,
        )

    return send


async def reconcile_unbound_topics_once(
    settings: AppSettings,
    *,
    agent_instance_id: str,
    connector: RuntimeConnector,
) -> int:
    """Recover setup invitations when Telegram omits a topic-created webhook.

    The connector sees only Runtime's sanitized discovery result, never the
    upstream topic_config file. A webhook and this periodic path share the
    same durable onboarding record and send lock.
    """

    store = read_runtime_control_store(settings.runtime_control_path)
    agent = store.agent_instances.get(agent_instance_id)
    if (
        agent is None
        or not agent.active
        or agent.forum_chat_id not in settings.cus_allowed_forum_chat_ids
    ):
        raise RuntimeConnectorError(
            "AGENT_INSTANCE_UNAVAILABLE",
            "CUS AgentInstance is not active or its forum is not allowlisted",
            retryable=False,
        )
    response = await connector.status()
    raw_status = response.get("status")
    if not isinstance(raw_status, dict) or raw_status.get("agent_instance_id") != agent_instance_id:
        raise RuntimeConnectorError(
            "INVALID_RUNTIME_RESPONSE", "Runtime status namespace mismatch", retryable=False
        )
    discovery = raw_status.get("topic_discovery")
    if not isinstance(discovery, dict) or discovery.get("status") != "OK":
        raise RuntimeConnectorError(
            "TOPIC_DISCOVERY_UNAVAILABLE", "Runtime topic discovery is unavailable", retryable=True
        )
    raw_topics = discovery.get("unbound_topics")
    if not isinstance(raw_topics, list):
        raise RuntimeConnectorError(
            "INVALID_RUNTIME_RESPONSE", "Runtime topic candidates are malformed", retryable=False
        )
    seen: set[int] = set()
    sent = 0
    for raw_topic in raw_topics:
        if not isinstance(raw_topic, dict):
            raise RuntimeConnectorError(
                "INVALID_RUNTIME_RESPONSE", "Runtime topic candidate is malformed", retryable=False
            )
        thread_id = raw_topic.get("thread_id")
        topic_name = raw_topic.get("topic_name")
        if (
            not isinstance(thread_id, int)
            or isinstance(thread_id, bool)
            or thread_id <= 1
            or thread_id in seen
            or not isinstance(topic_name, str)
            or not topic_name.strip()
            or len(topic_name) > 256
        ):
            raise RuntimeConnectorError(
                "INVALID_RUNTIME_RESPONSE",
                "Runtime topic candidate identity is invalid",
                retryable=False,
            )
        seen.add(thread_id)
        try:
            message_id = await ensure_topic_setup_notice(
                settings,
                chat_id=agent.forum_chat_id,
                thread_id=thread_id,
                topic_name=topic_name,
            )
        except (CusBotDeliveryError, RuntimeOnboardingError) as exc:
            raise RuntimeConnectorError(
                exc.code,
                "CUS Bot setup notice could not be confirmed",
                retryable=getattr(exc, "retryable", False),
            ) from exc
        if message_id is not None:
            sent += 1
            notice_logger.info(
                "CUS Bot recovered setup notice agent_instance_id=%s thread_id=%d message_id=%d",
                agent_instance_id,
                thread_id,
                message_id,
            )
    return sent


async def reconcile_all_runtime_instances_once(
    settings: AppSettings,
    *,
    connector_factory: ConnectorFactory = RuntimeConnector,
) -> RuntimeReconcileCycle:
    store = read_runtime_control_store(settings.runtime_control_path)
    agents = tuple(
        sorted(
            (agent for agent in store.agent_instances.values() if agent.active),
            key=lambda agent: agent.agent_instance_id,
        )
    )
    policy = observed_policy(settings)

    async def reconcile_agent(agent_instance_id: str, socket_path: str) -> AgentReconcileResult:
        connector = connector_factory(
            Path(socket_path),
            timeout=settings.runtime_connector_timeout_seconds,
        )
        try:
            await reconcile_pending_project_relocations(
                store_path=settings.runtime_control_path,
                agent_instance_id=agent_instance_id,
                connector=connector,
            )
            await connector.control_plane_heartbeat(agent_instance_id)
            await reconcile_desired_state_once(
                store_path=settings.runtime_control_path,
                agent_instance_id=agent_instance_id,
                connector=connector,
            )
            report = await reconcile_runtime_outbox_once(
                store_path=settings.runtime_control_path,
                connector=connector,
                policy_allows=policy,
                limit=settings.runtime_reconcile_batch_size,
                agent_instance_id=agent_instance_id,
                notification_sender=notification_sender(settings),
            )
            if (
                settings.cus_telegram_bot_token
                and settings.cus_miniapp_deep_link_base
                and settings.cus_onboarding_token_key
            ):
                await reconcile_unbound_topics_once(
                    settings,
                    agent_instance_id=agent_instance_id,
                    connector=connector,
                )
        except RuntimeConnectorError as exc:
            return AgentReconcileResult(
                agent_instance_id=agent_instance_id,
                status="DEGRADED" if exc.retryable else "ERROR",
                error_code=exc.code,
                retryable=exc.retryable,
            )
        return AgentReconcileResult(
            agent_instance_id=agent_instance_id,
            status="OK",
            report=report,
        )

    results = await asyncio.gather(
        *(reconcile_agent(agent.agent_instance_id, agent.runtime_socket) for agent in agents)
    )
    return RuntimeReconcileCycle(agents=tuple(results))


async def runtime_reconcile_loop(
    settings: AppSettings,
    *,
    connector_factory: ConnectorFactory = RuntimeConnector,
) -> None:
    while True:
        try:
            cycle = await reconcile_all_runtime_instances_once(
                settings,
                connector_factory=connector_factory,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Runtime reconcile cycle failed")
            await asyncio.sleep(settings.runtime_reconcile_interval_seconds)
            continue
        for result in cycle.agents:
            if result.status == "OK":
                continue
            logger.warning(
                "Runtime reconcile degraded agent_instance_id=%s status=%s error_code=%s retryable=%s",
                result.agent_instance_id,
                result.status,
                result.error_code,
                result.retryable,
            )
        await asyncio.sleep(settings.runtime_reconcile_interval_seconds)
