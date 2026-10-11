from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ai_account_manager.core.runtime_control import read_runtime_control_store
from ai_account_manager.integrations.ai_agent_runtime import (
    RuntimeConnector,
    RuntimeConnectorError,
)
from ai_account_manager.schemas.runtime_control import AgentInstanceRecord, RuntimeBindingRecord
from ai_account_manager.schemas.runtime_health import (
    RuntimeAgentHealthPublic,
    RuntimeBindingHealthPublic,
    RuntimeBindingHealthResponse,
    RuntimeDesiredPublic,
    RuntimeHealthCode,
    RuntimeRevisionPublic,
)

ConnectorFactory = Callable[[Path, float], RuntimeConnector]

_RUNTIME_HEALTH_CODES = {
    "RUNTIME_HEALTHY",
    "DEGRADED_CUS_SYNC",
    "PENDING_CHANGE",
    "OUT_OF_SYNC",
    "UNMANAGED",
    "UNSUPPORTED_CODEX_SUBPROCESS",
    "OBSERVED_PENDING_SYNC",
    "OBSERVED_POLICY_REJECTED",
    "CONFIG_DRIFT",
    "CORRELATION_MISMATCH",
    "AMBIGUOUS",
    "ERROR",
}


def _connector(path: Path, timeout: float) -> RuntimeConnector:
    return RuntimeConnector(path, timeout=timeout)


def _revision(value: object) -> RuntimeRevisionPublic | None:
    if not isinstance(value, dict):
        return None
    generation = value.get("generation_id")
    revision = value.get("revision")
    digest = value.get("spec_digest")
    if (
        not isinstance(generation, str)
        or not generation
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or not isinstance(digest, str)
    ):
        return None
    try:
        return RuntimeRevisionPublic(
            generation_id=generation,
            revision=revision,
            spec_digest=digest,
        )
    except ValueError:
        return None


def _runtime_bindings(
    response: dict[str, Any], agent_instance_id: str
) -> dict[str, dict[str, Any]]:
    status = response.get("status")
    if not isinstance(status, dict) or status.get("agent_instance_id") != agent_instance_id:
        raise ValueError("Runtime status namespace mismatch")
    raw_bindings = status.get("bindings")
    if not isinstance(raw_bindings, list):
        raise ValueError("Runtime bindings status is malformed")
    indexed: dict[str, dict[str, Any]] = {}
    for raw in raw_bindings:
        if not isinstance(raw, dict):
            raise ValueError("Runtime binding status is malformed")
        binding_id = raw.get("binding_id")
        if not isinstance(binding_id, str) or not binding_id or binding_id in indexed:
            raise ValueError("Runtime binding status identity is invalid")
        source_ref = raw.get("source_ref")
        if (
            not isinstance(source_ref, dict)
            or source_ref.get("agent_instance_id") != agent_instance_id
        ):
            raise ValueError("Runtime binding source namespace mismatch")
        indexed[binding_id] = raw
    return indexed


def _public_binding(
    binding: RuntimeBindingRecord,
    *,
    runtime: dict[str, Any] | None,
    runtime_available: bool,
    diagnostic_code: str | None,
) -> RuntimeBindingHealthPublic:
    desired = RuntimeDesiredPublic(
        generation_id=binding.desired.generation_id,
        revision=binding.desired.revision,
        spec_digest=binding.desired.spec_digest,
        provider=binding.desired.provider,
        account_profile=binding.desired.account_profile,
        model=binding.desired.model,
        reasoning_effort=binding.desired.reasoning_effort,
    )
    if binding.lifecycle_status == "ARCHIVED":
        health: RuntimeHealthCode = "ARCHIVED"
    elif binding.lifecycle_status == "UNCONFIGURED":
        health = "UNCONFIGURED"
    elif binding.lifecycle_status == "UNMANAGED":
        health = "UNMANAGED"
    elif not runtime_available:
        health = "RUNTIME_UNAVAILABLE"
    elif runtime is None:
        health = "OUT_OF_SYNC"
        diagnostic_code = "RUNTIME_BINDING_MISSING"
    else:
        raw_health = runtime.get("health")
        health = raw_health if raw_health in _RUNTIME_HEALTH_CODES else "ERROR"
        if health == "ERROR" and diagnostic_code is None:
            diagnostic_code = "RUNTIME_HEALTH_INVALID"

    runtime_desired = _revision(runtime.get("desired")) if runtime is not None else None
    active_instances = 0
    active_lifecycles: list[str] = []
    applied_by_digest: dict[str, RuntimeRevisionPublic] = {}
    raw_instances = runtime.get("instances") if runtime is not None else None
    if isinstance(raw_instances, list):
        for instance in raw_instances:
            if not isinstance(instance, dict) or instance.get("lifecycle") in {
                "EXITED",
                "RETIRED_FOR_RESTART",
            }:
                continue
            active_instances += 1
            lifecycle = instance.get("lifecycle")
            if isinstance(lifecycle, str):
                active_lifecycles.append(lifecycle)
            applied = _revision(instance.get("applied"))
            if applied is not None:
                applied_by_digest[applied.spec_digest] = applied
    management_mode = runtime.get("management_mode") if runtime is not None else None
    if not isinstance(management_mode, str):
        management_mode = None
    if active_instances == 0:
        activity = "STOPPED"
    elif "BUSY" in active_lifecycles:
        activity = "BUSY"
    elif len(active_lifecycles) == active_instances and all(
        lifecycle == "IDLE" for lifecycle in active_lifecycles
    ):
        activity = "IDLE"
    else:
        activity = "UNKNOWN"
    return RuntimeBindingHealthPublic(
        binding_id=binding.binding_id,
        agent_instance_id=binding.agent_instance_id,
        chat_id=binding.chat_id,
        thread_id=binding.thread_id,
        topic_name=binding.topic_name,
        project_id=binding.project_id,
        lifecycle_status=binding.lifecycle_status,
        health=health,
        management_mode=management_mode,
        runtime_available=runtime_available,
        desired=desired,
        runtime_desired=runtime_desired,
        applied=tuple(sorted(applied_by_digest.values(), key=lambda item: item.revision)),
        active_instance_count=active_instances,
        activity=activity,
        diagnostic_code=diagnostic_code,
    )


async def _read_agent_status(
    agent: AgentInstanceRecord,
    *,
    timeout: float,
    connector_factory: ConnectorFactory,
) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    try:
        response = await connector_factory(Path(agent.runtime_socket), timeout).status()
        return _runtime_bindings(response, agent.agent_instance_id), None
    except RuntimeConnectorError as exc:
        return None, exc.code
    except (OSError, ValueError, TypeError):
        return None, "INVALID_RUNTIME_RESPONSE"


async def collect_runtime_binding_health(
    *,
    store_path: Path,
    timeout: float,
    connector_factory: ConnectorFactory = _connector,
) -> RuntimeBindingHealthResponse:
    """Return a sanitized binding view without exposing Runtime process internals."""

    store = read_runtime_control_store(store_path)
    active_agents = {
        agent_id: agent for agent_id, agent in store.agent_instances.items() if agent.active
    }
    results = await asyncio.gather(
        *(
            _read_agent_status(
                agent,
                timeout=timeout,
                connector_factory=connector_factory,
            )
            for agent in active_agents.values()
        )
    )
    by_agent = dict(zip(active_agents, results, strict=True))
    public_agents: list[RuntimeAgentHealthPublic] = []
    for agent_id in sorted(active_agents):
        runtime_bindings, diagnostic = by_agent[agent_id]
        known_ids = {
            binding.binding_id
            for binding in store.bindings.values()
            if binding.agent_instance_id == agent_id
        }
        runtime_ids = set(runtime_bindings or {})
        public_agents.append(
            RuntimeAgentHealthPublic(
                agent_instance_id=agent_id,
                available=runtime_bindings is not None,
                diagnostic_code=diagnostic,
                unrecognized_binding_count=len(runtime_ids - known_ids),
            )
        )

    public_bindings: list[RuntimeBindingHealthPublic] = []
    for binding in sorted(
        store.bindings.values(),
        key=lambda item: (item.agent_instance_id, item.chat_id, item.thread_id),
    ):
        result = by_agent.get(binding.agent_instance_id)
        runtime_bindings, diagnostic = result if result is not None else (None, "AGENT_INACTIVE")
        public_bindings.append(
            _public_binding(
                binding,
                runtime=(runtime_bindings or {}).get(binding.binding_id),
                runtime_available=runtime_bindings is not None,
                diagnostic_code=diagnostic,
            )
        )
    return RuntimeBindingHealthResponse(
        bindings=tuple(public_bindings),
        agent_instances=tuple(public_agents),
    )
