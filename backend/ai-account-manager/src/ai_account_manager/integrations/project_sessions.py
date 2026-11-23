from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.core.project_inventory import (
    ProjectInventoryError,
    ProjectInventoryRecord,
    inventory_id_for_config_topic,
    inventory_id_for_topic,
    read_project_inventory,
)
from ai_account_manager.core.runtime_control import (
    RuntimeControlStoreError,
    read_runtime_control_store,
)
from ai_account_manager.providers.codex.quota import CodexQuotaError, get_cached_codex_quota
from ai_account_manager.schemas.runtime_control import RuntimeBindingRecord
from ai_account_manager.schemas.sessions import (
    ObservedProjectSession,
    ObservedProjectSessionsResponse,
    RuntimeIntegrationStatus,
    SessionQuotaSummary,
    SessionQuotaWindowSummary,
)

_MAX_STATE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class _PaneInfo:
    session_name: str
    pane_pid: int | None


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        return {}
    try:
        if not path.exists() or path.stat().st_size > _MAX_STATE_BYTES:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _pane_map() -> dict[str, _PaneInfo]:
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name}|#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    panes: dict[str, _PaneInfo] = {}
    for line in result.stdout.splitlines():
        name, separator, raw_pid = line.partition("|")
        if not separator or not name:
            continue
        try:
            pid = int(raw_pid)
        except ValueError:
            pid = None
        panes.setdefault(name, _PaneInfo(session_name=name, pane_pid=pid))
    return panes


def _composite_key(chat_id: int | None, thread_id: int) -> str | None:
    return f"{chat_id}:{thread_id}" if chat_id is not None else None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _window_label(minutes: int | None) -> str:
    labels = {
        60: "1-часовое окно",
        300: "5-часовое окно",
        1440: "24-часовое окно",
        10080: "7-дневное окно",
    }
    if minutes in labels:
        return labels[minutes]
    if minutes and minutes % 1440 == 0:
        return f"{minutes // 1440}-дневное окно"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60}-часовое окно"
    return f"Окно {minutes} мин" if minutes else "Окно неизвестной длительности"


def _quota_bucket_kind(model: str | None) -> Literal["standard", "spark", "unknown"]:
    if not isinstance(model, str) or not model.strip():
        return "unknown"
    return "spark" if model.strip().lower().endswith("-spark") else "standard"


def _bucket_matches_model(bucket: object, kind: str) -> bool:
    limit_id = str(getattr(bucket, "limit_id", "") or "").strip().lower()
    limit_name = str(getattr(bucket, "limit_name", "") or "").strip().lower()
    if kind == "spark":
        return (
            limit_id in {"codex_spark", "gpt-5.3-codex-spark"}
            or limit_name == "gpt-5.3-codex-spark"
        )
    if kind == "standard":
        return limit_id == "codex" or limit_name == "codex"
    return False


def _quota_summary(
    account: str | None, model: str | None, settings: AppSettings
) -> SessionQuotaSummary:
    kind = _quota_bucket_kind(model)
    if not account:
        return SessionQuotaSummary(
            status="account_unregistered",
            model_id=model,
            bucket_kind=kind,
            unavailable_reason="Аккаунт проекта не определён",
        )
    try:
        snapshot = get_cached_codex_quota(account, settings=settings)
    except CodexQuotaError:
        return SessionQuotaSummary(
            status="cache_invalid",
            account_slug=account,
            account_label=account,
            model_id=model,
            bucket_kind=kind,
            unavailable_reason="Кэш лимитов аккаунта повреждён",
        )
    summary = SessionQuotaSummary(
        status=snapshot.status,
        account_slug=account,
        account_label=account,
        model_id=model,
        bucket_kind=kind,
    )
    matching = [bucket for bucket in snapshot.buckets if _bucket_matches_model(bucket, kind)]
    if len(matching) != 1:
        summary.bucket_kind = "unknown"
        summary.unavailable_reason = (
            "Лимиты аккаунта не обновлены"
            if snapshot.status != "ok"
            else "Бакет лимита для модели не определён"
        )
        return summary
    bucket = matching[0]
    summary.bucket_label = bucket.limit_name or bucket.limit_id
    summary.selected_limit_id = bucket.limit_id
    summary.selected_limit_name = bucket.limit_name
    summary.windows = [
        SessionQuotaWindowSummary(
            window_type=window.window_type or window.window_id,
            window_label=_window_label(window.window_duration_minutes),
            remaining_percent=window.remaining_percent,
            resets_at=window.resets_at,
        )
        for window in bucket.windows
    ]
    if not bucket.windows:
        summary.unavailable_reason = "В выбранном бакете нет окон лимитов"
        return summary
    first = bucket.windows[0]
    summary.selected_window_type = first.window_type or first.window_id
    summary.selected_window_label = _window_label(first.window_duration_minutes)
    summary.remaining_percent = first.remaining_percent
    summary.resets_at = first.resets_at
    by_type = {window.window_type or window.window_id: window for window in bucket.windows}
    primary = by_type.get("primary")
    secondary = by_type.get("secondary")
    summary.five_hour_used_percent = primary.used_percent if primary else None
    summary.weekly_used_percent = secondary.used_percent if secondary else None
    summary.five_hour_reset_at = primary.reset_at if primary else None
    summary.weekly_reset_at = secondary.reset_at if secondary else None
    return summary


def _state_index(
    raw: dict[str, Any],
) -> tuple[dict[tuple[int, int], dict[str, Any]], dict[int, list[tuple[int, dict[str, Any]]]]]:
    exact: dict[tuple[int, int], dict[str, Any]] = {}
    by_thread: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        chat_raw, separator, thread_raw = key.partition(":")
        if not separator:
            continue
        try:
            identity = (int(chat_raw), int(thread_raw))
        except ValueError:
            continue
        exact[identity] = value
        by_thread.setdefault(identity[1], []).append((identity[0], value))
    return exact, by_thread


def _bindings_by_thread(
    settings: AppSettings,
) -> tuple[dict[int, RuntimeBindingRecord], str | None]:
    try:
        store = read_runtime_control_store(settings.runtime_control_path)
    except RuntimeControlStoreError as exc:
        return {}, exc.code
    result: dict[int, RuntimeBindingRecord] = {}
    for binding in store.bindings.values():
        if binding.lifecycle_status == "ARCHIVED":
            continue
        if binding.thread_id in result:
            return {}, "runtime_binding_identity_ambiguous"
        result[binding.thread_id] = binding
    return result, None


def observe_project_sessions(
    settings: AppSettings | None = None,
) -> ObservedProjectSessionsResponse:
    settings = settings or get_settings()
    warnings: list[str] = []
    bindings, binding_warning = _bindings_by_thread(settings)
    if binding_warning:
        warnings.append(binding_warning)
    integration = RuntimeIntegrationStatus(
        status="compatible" if not binding_warning else "unavailable",
        bot_root=str(settings.telegram_ai_agent_root),
        switch_actions_allowed=not binding_warning,
        reason=None if not binding_warning else "Runtime control store is unavailable",
    )
    topic_raw = _read_json(settings.telegram_ai_agent_topic_config_path)
    state_raw = _read_json(settings.telegram_ai_agent_tmux_state_path)
    exact_states, states_by_thread = _state_index(state_raw)
    panes = _pane_map()
    try:
        inventory = read_project_inventory(settings)
    except ProjectInventoryError as exc:
        inventory = {}
        warnings.append(exc.code)

    projects: list[ObservedProjectSession] = []
    matched_states: set[tuple[int, int]] = set()
    topics = topic_raw.get("topics")
    if not isinstance(topics, dict):
        topics = {}
    for raw_thread, raw_topic in topics.items():
        if not isinstance(raw_topic, dict) or raw_topic.get("type") != "project":
            continue
        try:
            thread_id = int(raw_thread)
        except ValueError:
            continue
        binding = bindings.get(thread_id)
        configured = binding is not None and binding.lifecycle_status == "CONFIGURED"
        chat_id = binding.chat_id if binding else _int_or_none(raw_topic.get("chat_id"))
        project_path = binding.canonical_cwd if binding else raw_topic.get("cwd")
        if not isinstance(project_path, str) or not project_path.strip():
            project_path = None
        canonical_path = (
            str(Path(project_path).expanduser().resolve(strict=False)) if project_path else None
        )
        project_id = binding.project_id if binding else None
        identity_kind = (
            "project_path"
            if canonical_path
            else "telegram_topic"
            if chat_id is not None
            else "topic_config_entry"
        )
        if project_id is None:
            project_id = (
                inventory_id_for_topic(chat_id, thread_id)
                if chat_id is not None
                else inventory_id_for_config_topic(
                    settings.telegram_ai_agent_topic_config_path, raw_thread
                )
            )
        state = exact_states.get((chat_id, thread_id)) if chat_id is not None else None
        if state is not None and chat_id is not None:
            matched_states.add((chat_id, thread_id))
        elif len(states_by_thread.get(thread_id, [])) == 1:
            state_chat, state = states_by_thread[thread_id][0]
            matched_states.add((state_chat, thread_id))
            if chat_id is None:
                chat_id = state_chat
        session_name = state.get("session_name") if isinstance(state, dict) else None
        if not isinstance(session_name, str) or not session_name:
            session_name = None
        running = session_name in panes if session_name else False
        inventory_record: ProjectInventoryRecord | None = inventory.get(project_id)
        archived = bool(inventory_record.archived) if inventory_record else False
        provider = (
            binding.desired.provider if binding else str(raw_topic.get("engine") or "unknown")
        )
        account = binding.desired.account_profile if binding else None
        model = binding.desired.model if binding else None
        quota = (
            _quota_summary(account, model, settings)
            if provider == "codex"
            else SessionQuotaSummary(
                status="not_available",
                account_slug=account,
                account_label=account,
                model_id=model,
                unavailable_reason="Лимиты для этого провайдера не поддерживаются",
            )
        )
        path_exists = Path(canonical_path).is_dir() if canonical_path else None
        session_status = "running" if running else "stale_state" if session_name else "stopped"
        if path_exists is False:
            session_status = "stale_state"
        project = ObservedProjectSession(
            chat_id=chat_id,
            thread_id=thread_id,
            composite_key=_composite_key(chat_id, thread_id),
            topic_name=binding.topic_name
            if binding
            else str(raw_topic.get("name") or f"Topic {thread_id}"),
            project_path=canonical_path,
            project_id=project_id,
            inventory_identity_kind=identity_kind,
            topic_config_key=raw_thread if identity_kind == "topic_config_entry" else None,
            archive_available=bool(project_id),
            tmux_session_name=session_name,
            tmux_session_running=running,
            agent=provider,
            account_slug=account,
            account_label=account,
            account_source="runtime_control" if binding else "unknown",
            quota=quota,
            topic_status="archived" if archived else "configured" if configured else "unconfigured",
            session_status=session_status,
            management_status="managed" if configured else "not_configured",
            activity_status="unknown" if running else "session_stopped",
            archived=archived,
            archive_revision=inventory_record.revision if inventory_record else None,
            inventory_reason=inventory_record.reason if inventory_record else None,
            path_exists=path_exists,
            onboarding_state="CONFIGURED" if configured else "UNCONFIGURED",
            missing_launch_fields=[] if configured else ["runtime_binding"],
            setup_action_enabled=not configured,
        )
        projects.append(project)

    for (chat_id, thread_id), state in exact_states.items():
        if (chat_id, thread_id) in matched_states or thread_id in bindings:
            continue
        session_name = state.get("session_name")
        if not isinstance(session_name, str) or session_name not in panes:
            continue
        warnings.append(f"unregistered_session:{chat_id}:{thread_id}")

    projects.sort(key=lambda item: (item.archived, item.topic_name.lower(), item.thread_id))
    return ObservedProjectSessionsResponse(
        integration=integration,
        project_count=len(projects),
        projects=projects,
        warnings=warnings,
    )
