from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from contextlib import contextmanager
import fcntl
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, cast

from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.core.launcher import LauncherResolveError, build_project_launch_spec
from ai_account_manager.core.project_settings import (
    ProjectSettingsError,
    canonical_project_path,
    restore_project_settings_record,
    upsert_project_settings_with_snapshot,
    validate_project_id,
    validate_provider_project_settings,
)
from ai_account_manager.core.project_inventory import (
    inventory_id_for_config_topic,
    inventory_id_for_topic,
    ProjectInventoryError,
    ProjectInventoryRecord,
    read_project_inventory,
)
from ai_account_manager.core.project_onboarding import (
    OnboardingState,
    ProjectOnboardingError,
    onboarding_missing_fields,
    validate_onboarding_project_path,
)
from ai_account_manager.core.project_settings import (
    project_id_for_path,
    read_project_settings_store,
)
from ai_account_manager.core.runtime_launch_specs import (
    RuntimeLaunchSpecStoreError,
    get_runtime_launch_spec,
)
from ai_account_manager.integrations.bot_control import BotControlError, request_bot_control
from ai_account_manager.integrations.runtime_selection import (
    apply_cached_runtime_selections,
    clear_runtime_selection_cache,
    refresh_runtime_selections as refresh_runtime_selection_cache,
)
from ai_account_manager.providers.codex.quota import CodexQuotaError, get_cached_codex_quota
from ai_account_manager.schemas.sessions import (
    ObservedProjectSession,
    ObservedProjectSessionsResponse,
    ProjectAccountSwitchResponse,
    ProjectSettingsApplyResponse,
    ProjectSettingsRecord,
    ProjectOnboardingApplyResponse,
    ProjectOnboardingPathBindResponse,
    ProjectBinding,
    ProjectBindingsResponse,
    RuntimeIntegrationStatus,
    SessionQuotaSummary,
    SessionQuotaWindowSummary,
)

_SAFE_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_BINDINGS_VERSION = 1
_MAX_STATE_BYTES = 2 * 1024 * 1024


class SessionRuntimeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class _PaneInfo:
    session_name: str
    pane_pid: int | None
    pane_path: str | None
    pane_command: str | None


@dataclass(frozen=True)
class _PersistedTopicIdentity:
    project_id: str
    chat_id: int
    thread_id: int
    onboarding_state: OnboardingState
    project_path: str | None


def _utc_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        return {}
    if path.exists() and path.stat().st_size > _MAX_STATE_BYTES:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink():
        raise SessionRuntimeError("invalid_state_path", "Runtime state path is invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    parent_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _runtime_lock_path(settings: AppSettings) -> Path:
    return settings.project_bindings_path.parent / "runtime_apply_state.lock"


def _runtime_journal_path(settings: AppSettings) -> Path:
    return settings.project_bindings_path.parent / "runtime_apply_transaction_v1.json"


@contextmanager
def _binding_state_lock(settings: AppSettings) -> Iterator[None]:
    lock = _runtime_lock_path(settings)
    if lock.is_symlink() or settings.project_bindings_path.is_symlink():
        raise SessionRuntimeError("invalid_state_path", "Runtime state path is invalid")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(lock.parent, 0o700)
    except OSError:
        pass
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        if settings.project_bindings_path.is_symlink():
            raise SessionRuntimeError("invalid_state_path", "Runtime state path is invalid")
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _pending_runtime_journal(settings: AppSettings) -> bool:
    journal = _runtime_journal_path(settings)
    if journal.is_symlink():
        raise SessionRuntimeError("invalid_state_path", "Runtime state path is invalid")
    return journal.exists()


def _runtime_spec_exists_for_project(project_path: str, settings: AppSettings) -> bool:
    path = settings.runtime_launch_specs_path
    if path.is_symlink() or not path.exists():
        return False
    if not path.is_file() or path.stat().st_size > _MAX_STATE_BYTES:
        raise SessionRuntimeError("invalid_runtime_state", "Runtime settings state is invalid")
    raw = _read_json(path)
    projects = raw.get("projects") if isinstance(raw, dict) else None
    if raw.get("version") != 1 or not isinstance(projects, dict):
        raise SessionRuntimeError("invalid_runtime_state", "Runtime settings state is invalid")
    from ai_account_manager.core.project_settings import project_id_for_path

    project_id = project_id_for_path(project_path)
    return project_id in projects


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_project_path(project_path: str, settings: AppSettings) -> Path:
    raw = Path(project_path).expanduser()
    if not raw.is_absolute():
        raise SessionRuntimeError("project_path_not_absolute", "Project path must be absolute")
    resolved = raw.resolve(strict=False)
    roots = [root.expanduser().resolve(strict=False) for root in settings.ai_project_roots]
    if not any(resolved == root or _is_relative_to(resolved, root) for root in roots):
        raise SessionRuntimeError(
            "project_outside_allowed_roots", "Project path is outside AI_PROJECT_ROOTS"
        )
    if not resolved.is_dir():
        raise SessionRuntimeError("project_missing", "Project path does not exist")
    return resolved


def _validate_slug(account_slug: str) -> str:
    value = account_slug.strip()
    if not _SAFE_SLUG_RE.fullmatch(value):
        raise SessionRuntimeError("invalid_account_slug", "Invalid account slug")
    return value


def _binding_payload(settings: AppSettings) -> dict[str, Any]:
    raw = _read_json(settings.project_bindings_path)
    bindings = raw.get("bindings")
    if not isinstance(bindings, dict):
        bindings = {}
    return {"version": _BINDINGS_VERSION, "bindings": bindings}


def list_project_bindings(settings: AppSettings | None = None) -> ProjectBindingsResponse:
    settings = settings or get_settings()
    with _binding_state_lock(settings):
        payload = _binding_payload(settings)
    result: list[ProjectBinding] = []
    for project_path, value in payload["bindings"].items():
        if not isinstance(project_path, str) or not isinstance(value, dict):
            continue
        provider_id = value.get("provider_id")
        account_slug = value.get("account_slug")
        profile_path = value.get("profile_path")
        updated_at = value.get("updated_at")
        if not all(
            isinstance(item, str) and item
            for item in (provider_id, account_slug, profile_path, updated_at)
        ):
            continue
        result.append(
            ProjectBinding(
                project_path=project_path,
                provider_id=provider_id,
                account_slug=account_slug,
                profile_path=profile_path,
                updated_at=updated_at,
            )
        )
    result.sort(key=lambda item: item.project_path)
    return ProjectBindingsResponse(binding_count=len(result), bindings=result)


def upsert_project_binding(
    *,
    project_path: str,
    provider_id: str,
    account_slug: str,
    settings: AppSettings | None = None,
) -> ProjectBinding:
    settings = settings or get_settings()
    resolved_project = _validate_project_path(project_path, settings)
    provider = provider_id.strip().lower()
    if provider != "codex":
        raise SessionRuntimeError("provider_not_supported", "Only Codex bindings are enabled")
    slug = _validate_slug(account_slug)
    profile_path = (settings.codex_profiles_root / slug).resolve(strict=False)
    if not profile_path.is_dir():
        raise SessionRuntimeError("profile_missing", "Codex profile directory does not exist")
    if not (profile_path / "auth.json").is_file():
        raise SessionRuntimeError("auth_missing", "Codex profile auth.json does not exist")

    with _binding_state_lock(settings):
        if _pending_runtime_journal(settings):
            raise SessionRuntimeError(
                "runtime_apply_recovery_required",
                "Runtime apply recovery is required before changing bindings",
            )
        payload = _binding_payload(settings)
        record = ProjectBinding(
            project_path=str(resolved_project),
            provider_id=provider,
            account_slug=slug,
            profile_path=str(profile_path),
            updated_at=_utc_iso(),
        )
        payload["bindings"][str(resolved_project)] = record.model_dump()
        _atomic_write_json(settings.project_bindings_path, payload)
        return record


def _run_text(argv: list[str], *, cwd: Path | None = None) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, "", ""
    return result.returncode, result.stdout or "", result.stderr or ""


def integration_status(
    settings: AppSettings | None = None,
    *,
    runner: Callable[[list[str], Path | None], tuple[int, str, str]] | None = None,
) -> RuntimeIntegrationStatus:
    settings = settings or get_settings()
    bot_root = settings.telegram_ai_agent_root
    manifest = _read_json(settings.telegram_ai_agent_compatibility_manifest)
    supported_commit = manifest.get("supported_base_commit")
    target_files = manifest.get("target_files")
    if not isinstance(supported_commit, str) or not isinstance(target_files, dict):
        return RuntimeIntegrationStatus(
            status="invalid_manifest",
            bot_root=str(bot_root),
            switch_actions_allowed=False,
            reason="Compatibility manifest is missing or invalid",
        )
    if not bot_root.is_dir():
        return RuntimeIntegrationStatus(
            status="unavailable",
            bot_root=str(bot_root),
            supported_commit=supported_commit,
            switch_actions_allowed=False,
            reason="telegram-ai-agent root does not exist",
        )

    invoke = runner or (lambda argv, cwd=None: _run_text(argv, cwd=cwd))
    rc, stdout, _ = invoke(["git", "rev-parse", "HEAD"], bot_root)
    bot_commit = stdout.strip() if rc == 0 else None
    hashes_match = True
    for rel, expected in target_files.items():
        if not isinstance(rel, str) or not isinstance(expected, str):
            hashes_match = False
            break
        path = bot_root / rel
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            hashes_match = False
            break
        if actual != expected:
            hashes_match = False
            break

    compatible = bot_commit == supported_commit and hashes_match
    return RuntimeIntegrationStatus(
        status="compatible" if compatible else "incompatible",
        bot_root=str(bot_root),
        bot_commit=bot_commit,
        supported_commit=supported_commit,
        target_files_match=hashes_match,
        switch_actions_allowed=compatible,
        reason=None if compatible else "Bot commit or integration target files are incompatible",
    )


def _pane_map() -> dict[str, _PaneInfo]:
    rc, stdout, _ = _run_text(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{session_name}|#{pane_pid}|#{pane_current_path}|#{pane_current_command}",
        ]
    )
    if rc != 0:
        return {}
    result: dict[str, _PaneInfo] = {}
    for line in stdout.splitlines():
        parts = line.split("|", 3)
        if len(parts) != 4 or not parts[0]:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            pid = None
        result.setdefault(
            parts[0],
            _PaneInfo(parts[0], pid, parts[2] or None, parts[3] or None),
        )
    return result


def _read_process_env(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    result: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        result[key.decode(errors="ignore")] = value.decode(errors="ignore")
    return result


def _child_pids(pid: int) -> list[int]:
    try:
        raw = Path(f"/proc/{pid}/task/{pid}/children").read_text().strip()
    except OSError:
        return []
    result: list[int] = []
    for item in raw.split():
        try:
            result.append(int(item))
        except ValueError:
            continue
    return result


def _find_codex_home(pid: int | None) -> Path | None:
    if pid is None:
        return None
    queue = [pid]
    seen: set[int] = set()
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        env = _read_process_env(current)
        raw = env.get("CODEX_HOME")
        if raw:
            return Path(raw).expanduser().resolve(strict=False)
        queue.extend(_child_pids(current))
    return None


def _home_from_transcript(transcript_path: object) -> Path | None:
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    path = Path(transcript_path).expanduser().resolve(strict=False)
    parts = path.parts
    try:
        idx = parts.index("sessions")
    except ValueError:
        return None
    if idx == 0:
        return None
    return Path(*parts[:idx])


def _account_for_home(home: Path | None, settings: AppSettings) -> tuple[str | None, str | None]:
    if home is None:
        return None, None
    resolved = home.resolve(strict=False)
    root = settings.codex_profiles_root
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if child.is_dir() and child.resolve(strict=False) == resolved:
                return child.name, child.name
    if resolved == (Path.home() / ".codex").resolve(strict=False):
        return None, "Стандартный профиль ~/.codex"
    return None, resolved.name


def _window_label(minutes: int | None) -> str:
    if minutes == 60:
        return "1-часовое окно"
    if minutes == 300:
        return "5-часовое окно"
    if minutes == 1440:
        return "24-часовое окно"
    if minutes == 10080:
        return "7-дневное окно"
    if minutes and minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days}-дневное окно"
    if minutes and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours}-часовое окно"
    return f"Окно {minutes} мин" if minutes else "Окно неизвестной длительности"


_SPARK_MODEL_SUFFIX = "-spark"
_SPARK_LIMIT_IDS = {"codex_spark", "gpt-5.3-codex-spark"}
_SPARK_LIMIT_NAMES = {"gpt-5.3-codex-spark"}
_STANDARD_LIMIT_IDS = {"codex"}
_STANDARD_LIMIT_NAMES = {"codex"}


def _quota_bucket_kind(model: str | None) -> Literal["standard", "spark", "unknown"]:
    if not isinstance(model, str) or not model.strip():
        return "unknown"
    return "spark" if model.strip().lower().endswith(_SPARK_MODEL_SUFFIX) else "standard"


def _bucket_matches_model(bucket: object, kind: str) -> bool:
    limit_id = str(getattr(bucket, "limit_id", "") or "").strip().lower()
    limit_name = str(getattr(bucket, "limit_name", "") or "").strip().lower()
    if kind == "spark":
        return limit_id in _SPARK_LIMIT_IDS or limit_name in _SPARK_LIMIT_NAMES
    if kind == "standard":
        return limit_id in _STANDARD_LIMIT_IDS or limit_name in _STANDARD_LIMIT_NAMES
    return False


def _quota_summary(
    account_slug: str | None, model: str | None, settings: AppSettings
) -> SessionQuotaSummary:
    if not account_slug:
        return SessionQuotaSummary(
            status="account_unregistered",
            model_id=model,
            unavailable_reason="Стандартный профиль не зарегистрирован как управляемый аккаунт ЦУС",
        )
    try:
        snapshot = get_cached_codex_quota(account_slug, settings=settings)
    except CodexQuotaError:
        return SessionQuotaSummary(
            status="cache_invalid",
            account_slug=account_slug,
            model_id=model,
            bucket_kind=_quota_bucket_kind(model),
            unavailable_reason="Кэш лимитов аккаунта повреждён",
        )
    kind = _quota_bucket_kind(model)
    summary = SessionQuotaSummary(
        status=snapshot.status,
        account_slug=account_slug,
        model_id=model,
        bucket_kind=kind,
    )
    if kind == "unknown":
        summary.unavailable_reason = "Модель проекта не определена — лимит не сопоставлен"
        return summary
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
    summary.bucket_label = (
        bucket.limit_name or bucket.limit_id or ("Spark" if kind == "spark" else "Codex")
    )
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
    if not summary.windows:
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


def _composite_key(chat_id: int | None, thread_id: int) -> str | None:
    return f"{chat_id}:{thread_id}" if chat_id is not None else None


def _read_project_settings_by_path(settings: AppSettings) -> dict[str, Any]:
    try:
        store, _ = read_project_settings_store(settings)
    except ProjectSettingsError:
        return {}
    projects = store.get("projects")
    if not isinstance(projects, dict):
        return {}
    result = {}
    for raw in projects.values():
        if not isinstance(raw, dict):
            continue
        path = raw.get("project_path")
        if isinstance(path, str):
            result[path] = raw
    return result


def _read_runtime_specs_by_id(settings: AppSettings) -> dict[str, Any]:
    raw = _read_json(settings.runtime_launch_specs_path)
    projects = raw.get("projects") if raw.get("version") == 1 else None
    if not isinstance(projects, dict):
        return {}
    return {key: value for key, value in projects.items() if isinstance(value, dict)}


def _apply_project_quotas(projects: list[ObservedProjectSession], settings: AppSettings) -> None:
    settings_by_path = _read_project_settings_by_path(settings)
    for project in projects:
        if project.agent != "codex":
            project.quota = SessionQuotaSummary(
                status="not_available",
                unavailable_reason="Лимиты для этого провайдера не поддерживаются",
            )
            continue
        model: str | None = None
        if project.tmux_session_running:
            if project.runtime_selection_status == "current":
                model = project.current_model
        elif project.project_path:
            configured = settings_by_path.get(project.project_path)
            configured_model = configured.get("model") if isinstance(configured, dict) else None
            if isinstance(configured_model, str) and configured_model.strip():
                model = configured_model.strip()
        quota = _quota_summary(project.account_slug, model, settings)
        quota.account_label = project.account_label
        if project.account_slug is None and project.account_source == "unknown":
            quota.unavailable_reason = "Аккаунт процесса не определён"
        project.quota = quota


def _activity_observation(
    projects: list[dict[str, int | str | None]],
    settings: AppSettings,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    candidates = [
        {
            "chat_id": item["chat_id"],
            "thread_id": item["thread_id"],
            "project_path": item.get("project_path"),
            "tmux_session_name": item.get("tmux_session_name"),
        }
        for item in projects
        if isinstance(item.get("chat_id"), int) and isinstance(item.get("thread_id"), int)
    ]
    if not candidates:
        return {}, None
    try:
        response = request_bot_control(
            settings.telegram_ai_agent_control_socket,
            {"version": 3, "action": "observe_project_activity", "projects": candidates},
            timeout_seconds=2.0,
        )
    except BotControlError as exc:
        return {}, exc.code
    raw_items = response.get("projects")
    if not isinstance(raw_items, list):
        return {}, "control_response_invalid"
    result: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        chat_id = raw.get("chat_id")
        thread_id = raw.get("thread_id")
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            continue
        if isinstance(thread_id, bool) or not isinstance(thread_id, int):
            continue
        key = _composite_key(chat_id, thread_id)
        if key is None:
            continue
        pending = raw.get("pending_count")
        if isinstance(pending, bool) or not isinstance(pending, int) or pending < 0:
            pending = 0
        result[key] = {
            "session_running": raw.get("session_running")
            if isinstance(raw.get("session_running"), bool)
            else None,
            "queue_processing": raw.get("queue_processing")
            if isinstance(raw.get("queue_processing"), bool)
            else None,
            "agent_processing": raw.get("agent_processing")
            if isinstance(raw.get("agent_processing"), bool)
            else None,
            "is_processing": raw.get("is_processing")
            if isinstance(raw.get("is_processing"), bool)
            else None,
            "queue_busy": raw.get("queue_busy")
            if isinstance(raw.get("queue_busy"), bool)
            else None,
            "pending_count": pending,
            "task_started_at": raw.get("task_started_at")
            if isinstance(raw.get("task_started_at"), str)
            else None,
            "last_task_completed_at": raw.get("last_task_completed_at")
            if isinstance(raw.get("last_task_completed_at"), str)
            else None,
            "last_task_outcome": raw.get("last_task_outcome")
            if isinstance(raw.get("last_task_outcome"), str)
            else None,
            "observed_at": raw.get("observed_at")
            if isinstance(raw.get("observed_at"), str)
            else None,
        }
    return result, None


def _activity_status(*, running: bool, activity: dict[str, Any] | None) -> str:
    if not running:
        return "session_stopped"
    if activity is None:
        return "unknown"
    if activity.get("is_processing") is True:
        return "working"
    if activity.get("is_processing") is None:
        return "unknown"
    if activity.get("pending_count", 0) > 0:
        return "queued"
    return "ready"


def _int_or_none_field(value: Any) -> int | None:
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


def _activity_chat_ids_by_thread(settings: AppSettings) -> dict[int, set[int]]:
    """Recover exact historical ChannelKey identity without mutating runtime state."""
    raw = _read_json(settings.project_activity_path)
    projects = raw.get("projects")
    if not isinstance(projects, dict):
        return {}
    result: dict[int, set[int]] = {}
    for raw_key, record in projects.items():
        if not isinstance(raw_key, str) or not isinstance(record, dict):
            continue
        try:
            chat_raw, thread_raw = raw_key.split(":", 1)
            chat_id = int(chat_raw)
            thread_id = int(thread_raw)
        except (TypeError, ValueError):
            continue
        result.setdefault(thread_id, set()).add(chat_id)
    return result


def _thread_has_live_state(
    thread_states: list[tuple[int, dict[str, Any]]], panes: dict[str, _PaneInfo]
) -> bool:
    for _chat_id, state in thread_states:
        raw_name = state.get("session_name")
        if isinstance(raw_name, str) and raw_name and raw_name in panes:
            return True
    return False


def _persisted_topic_identity(
    project_id: str,
    settings: AppSettings,
) -> tuple[_PersistedTopicIdentity | None, bool]:
    """Resolve onboarding identity from topic config only.

    The boolean result reports that the requested legacy config entry exists
    but lacks an exact persisted ``chat_id + thread_id`` pair. Runtime and tmux
    state are deliberately not consulted here.
    """
    raw = _read_json(settings.telegram_ai_agent_topic_config_path)
    topics = raw.get("topics")
    if not isinstance(topics, dict):
        return None, False
    identity_missing = False
    for raw_thread_id, value in topics.items():
        if not isinstance(raw_thread_id, str) or not isinstance(value, dict):
            continue
        try:
            key_thread_id = int(raw_thread_id)
        except ValueError:
            continue
        if value.get("type") != "project":
            continue
        config_entry_id = inventory_id_for_config_topic(
            settings.telegram_ai_agent_topic_config_path,
            raw_thread_id,
        )
        chat_id = _int_or_none_field(value.get("chat_id"))
        stored_thread_id = _int_or_none_field(value.get("thread_id"))
        if chat_id is None or stored_thread_id != key_thread_id:
            if project_id == config_entry_id:
                identity_missing = True
            continue
        stable_project_id = inventory_id_for_topic(chat_id, stored_thread_id)
        if stable_project_id != project_id:
            continue
        raw_state = value.get("onboarding_state", "LEGACY")
        onboarding_state: OnboardingState = (
            cast(OnboardingState, raw_state)
            if raw_state in {"LEGACY", "UNCONFIGURED", "CONFIGURED"}
            else "UNCONFIGURED"
        )
        raw_path = value.get("cwd")
        project_path = raw_path if isinstance(raw_path, str) and raw_path.strip() else None
        return (
            _PersistedTopicIdentity(
                project_id=stable_project_id,
                chat_id=chat_id,
                thread_id=stored_thread_id,
                onboarding_state=onboarding_state,
                project_path=project_path,
            ),
            False,
        )
    return None, identity_missing


def observe_project_sessions(
    settings: AppSettings | None = None,
) -> ObservedProjectSessionsResponse:
    settings = settings or get_settings()
    integration = integration_status(settings)
    topic_raw = _read_json(settings.telegram_ai_agent_topic_config_path)
    state_raw = _read_json(settings.telegram_ai_agent_tmux_state_path)
    bindings = {
        str(Path(item.project_path).expanduser().resolve(strict=False)): item
        for item in list_project_bindings(settings).bindings
    }
    panes = _pane_map()
    warnings: list[str] = []
    try:
        inventory = read_project_inventory(settings)
    except ProjectInventoryError as exc:
        inventory = {}
        warnings.append(exc.code)
    settings_by_path = _read_project_settings_by_path(settings)
    runtime_specs_by_id = _read_runtime_specs_by_id(settings)
    activity_chat_ids = _activity_chat_ids_by_thread(settings)

    state_by_key: dict[tuple[int, int], dict[str, Any]] = {}
    states_by_thread: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for raw_key, value in state_raw.items():
        if not isinstance(raw_key, str) or not isinstance(value, dict):
            continue
        try:
            chat_raw, thread_raw = raw_key.split(":", 1)
            thread_id = int(thread_raw)
            state_chat_id = int(chat_raw)
        except (ValueError, TypeError):
            continue
        state_by_key[(state_chat_id, thread_id)] = value
        states_by_thread.setdefault(thread_id, []).append((state_chat_id, value))
    unique_state_chat_id: int | None = None
    state_chat_ids = {chat_id for chat_id, _thread_id in state_by_key}
    if len(state_chat_ids) == 1:
        unique_state_chat_id = next(iter(state_chat_ids))

    projects: list[ObservedProjectSession] = []
    activity_candidates: list[dict[str, int | str | None]] = []
    matched_state_keys: set[tuple[int, int]] = set()
    topics = topic_raw.get("topics")
    if not isinstance(topics, dict):
        topics = {}
    for raw_thread, value in topics.items():
        if not isinstance(value, dict):
            continue
        try:
            thread_id = int(raw_thread)
        except (ValueError, TypeError):
            continue
        topic_type = value.get("type")
        if topic_type != "project":
            continue
        project_path_raw = value.get("cwd")
        project_path = (
            str(project_path_raw)
            if isinstance(project_path_raw, str) and project_path_raw.strip()
            else None
        )
        project_dir = (
            Path(project_path).expanduser().resolve(strict=False) if project_path else None
        )
        canonical_path = str(project_dir) if project_dir is not None else None
        path_exists = project_dir.is_dir() if project_dir is not None else False
        project_id = project_id_for_path(canonical_path) if canonical_path else None
        inventory_identity_kind = "project_path" if canonical_path else None
        raw_onboarding_state = value.get("onboarding_state", "LEGACY")
        onboarding_state: OnboardingState = (
            cast(OnboardingState, raw_onboarding_state)
            if raw_onboarding_state in {"LEGACY", "UNCONFIGURED", "CONFIGURED"}
            else "UNCONFIGURED"
        )
        if onboarding_state == "LEGACY" and canonical_path is None:
            onboarding_state = "UNCONFIGURED"

        topic_name = str(value.get("name") or f"Topic {thread_id}")
        agent = str(value.get("engine") or "unknown")
        chat_id: int | None = None
        state = None
        archive_unavailable_reason: str | None = None
        thread_states = states_by_thread.get(thread_id, [])
        explicit_chat_id = _int_or_none_field(value.get("chat_id"))
        explicit_thread_id = _int_or_none_field(value.get("thread_id"))
        if explicit_chat_id is not None and explicit_thread_id == thread_id:
            chat_id = explicit_chat_id
            state = state_by_key.get((chat_id, thread_id))
            if state is not None:
                matched_state_keys.add((chat_id, thread_id))
        elif canonical_path and len(thread_states) == 1:
            chat_id, state = thread_states[0]
            matched_state_keys.add((chat_id, thread_id))
        elif (
            canonical_path
            and not thread_states
            and len(activity_chat_ids.get(thread_id, set())) == 1
        ):
            chat_id = next(iter(activity_chat_ids[thread_id]))
        elif canonical_path and not thread_states and unique_state_chat_id is not None:
            chat_id = unique_state_chat_id
        elif not canonical_path:
            archive_unavailable_reason = "Архив недоступен: точный Telegram topic не определён."
        if not canonical_path and chat_id is not None:
            project_id = inventory_id_for_topic(chat_id, thread_id)
            inventory_identity_kind = "telegram_topic"
        if not canonical_path and chat_id is None:
            project_id = inventory_id_for_config_topic(
                settings.telegram_ai_agent_topic_config_path,
                raw_thread,
            )
            inventory_identity_kind = "topic_config_entry"
            if _thread_has_live_state(thread_states, panes):
                archive_unavailable_reason = (
                    "Архив недоступен: найдено потенциально активное состояние для этого topic."
                )
            else:
                archive_unavailable_reason = None
        settings_record = settings_by_path.get(canonical_path) if canonical_path else None
        runtime_record = runtime_specs_by_id.get(project_id) if project_id else None
        missing_launch_fields = (
            []
            if onboarding_state == "LEGACY"
            else onboarding_missing_fields(
                project_path=canonical_path,
                settings_record=settings_record,
                runtime_record=runtime_record,
            )
        )
        if onboarding_state == "CONFIGURED":
            expected_spec = runtime_record.get("spec_sha256") if runtime_record else None
            if value.get("runtime_spec_sha256") != expected_spec:
                if "runtime_launch_spec" not in missing_launch_fields:
                    missing_launch_fields.append("runtime_launch_spec")
            if missing_launch_fields:
                onboarding_state = "UNCONFIGURED"
        inventory_record: ProjectInventoryRecord | None = (
            inventory.get(project_id) if project_id else None
        )
        archived = bool(inventory_record.archived) if inventory_record is not None else False
        session_name = None
        running = False
        account_slug = None
        account_label = None
        account_profile_path = None
        account_source = "unknown"

        if isinstance(state, dict):
            raw_name = state.get("session_name")
            session_name = raw_name if isinstance(raw_name, str) and raw_name else None
        if session_name:
            running = session_name in panes

        codex_home: Path | None = None
        if agent == "codex" and session_name and session_name in panes:
            codex_home = _find_codex_home(panes[session_name].pane_pid)
            if codex_home is not None:
                account_source = "process_env"
        if codex_home is None and isinstance(state, dict):
            raw_home = state.get("codex_home")
            if isinstance(raw_home, str) and raw_home:
                codex_home = Path(raw_home).expanduser().resolve(strict=False)
                account_source = "state"
        if codex_home is None and isinstance(state, dict):
            codex_home = _home_from_transcript(state.get("transcript_path"))
            if codex_home is not None:
                account_source = "transcript"
        if codex_home is None and canonical_path and canonical_path in bindings:
            binding = bindings[canonical_path]
            codex_home = Path(binding.profile_path)
            account_slug = binding.account_slug
            account_label = binding.account_slug
            account_source = "binding"

        if codex_home is not None:
            account_profile_path = str(codex_home)
            detected_slug, detected_label = _account_for_home(codex_home, settings)
            account_slug = account_slug or detected_slug
            account_label = account_label or detected_label
            if account_source == "unknown":
                account_source = "default_profile"

        quota = SessionQuotaSummary(status="model_pending", account_slug=account_slug)
        if account_slug is None and account_source == "unknown":
            quota.unavailable_reason = "Аккаунт процесса не определён"
        management_status = "not_configured"
        if canonical_path and canonical_path in settings_by_path:
            management_status = "managed"
        elif running:
            management_status = "legacy_unmanaged"
        session_status = "running" if running else "stopped"
        if session_name and not running:
            session_status = "stale_state"
        if canonical_path is not None and not path_exists:
            session_status = "stale_state"
        project = ObservedProjectSession(
            chat_id=chat_id,
            thread_id=thread_id,
            composite_key=_composite_key(chat_id, thread_id),
            topic_name=topic_name,
            project_path=canonical_path or project_path,
            project_id=project_id,
            inventory_identity_kind=inventory_identity_kind,
            topic_config_key=raw_thread
            if inventory_identity_kind == "topic_config_entry"
            else None,
            archive_available=bool(
                project_id
                and (
                    canonical_path
                    or inventory_identity_kind == "telegram_topic"
                    or (
                        inventory_identity_kind == "topic_config_entry"
                        and archive_unavailable_reason is None
                    )
                )
            ),
            archive_unavailable_reason=archive_unavailable_reason,
            tmux_session_name=session_name,
            tmux_session_running=running,
            agent=agent,
            account_slug=account_slug,
            account_label=account_label,
            account_profile_path=account_profile_path,
            account_source=account_source,
            quota=quota,
            topic_status=(
                "archived"
                if archived
                else "unconfigured"
                if onboarding_state == "UNCONFIGURED"
                else "configured"
            ),
            session_status=session_status,
            management_status=management_status,
            activity_status="session_stopped" if not running else "unknown",
            archived=archived,
            archive_revision=inventory_record.revision if inventory_record else None,
            inventory_reason=inventory_record.reason if inventory_record else None,
            path_exists=path_exists,
            onboarding_state=onboarding_state,
            missing_launch_fields=missing_launch_fields,
            setup_action_enabled=onboarding_state == "UNCONFIGURED",
        )
        projects.append(project)
        activity_candidates.append(
            {
                "chat_id": chat_id,
                "thread_id": thread_id,
                "project_path": canonical_path,
                "tmux_session_name": session_name,
            }
        )

    for (chat_id, thread_id), state in state_by_key.items():
        if (chat_id, thread_id) in matched_state_keys:
            continue
        session_name_raw = state.get("session_name")
        session_name = (
            session_name_raw if isinstance(session_name_raw, str) and session_name_raw else None
        )
        if not session_name or session_name not in panes:
            continue
        cwd_raw = state.get("cwd")
        project_path = (
            str(Path(cwd_raw).expanduser().resolve(strict=False))
            if isinstance(cwd_raw, str) and cwd_raw
            else None
        )
        project_id = project_id_for_path(project_path) if project_path else None
        inventory_record = inventory.get(project_id) if project_id else None
        codex_home = (
            _find_codex_home(panes[session_name].pane_pid) if session_name in panes else None
        )
        account_slug, account_label = _account_for_home(codex_home, settings)
        project = ObservedProjectSession(
            chat_id=chat_id,
            thread_id=thread_id,
            composite_key=_composite_key(chat_id, thread_id),
            topic_name=f"Topic {thread_id}",
            project_path=project_path,
            project_id=project_id,
            inventory_identity_kind="project_path" if project_id else None,
            archive_available=bool(project_id),
            tmux_session_name=session_name,
            tmux_session_running=True,
            agent=str(state.get("provider") or "unknown"),
            account_slug=account_slug,
            account_label=account_label,
            account_profile_path=str(codex_home) if codex_home is not None else None,
            account_source="process_env" if codex_home is not None else "unknown",
            quota=SessionQuotaSummary(status="model_pending", account_slug=account_slug),
            topic_status="unknown",
            session_status="orphan_tmux",
            management_status="legacy_unmanaged",
            activity_status="unknown",
            archived=bool(inventory_record.archived) if inventory_record else False,
            archive_revision=inventory_record.revision if inventory_record else None,
            inventory_reason=inventory_record.reason if inventory_record else None,
            path_exists=Path(project_path).is_dir() if project_path else None,
        )
        projects.append(project)
        activity_candidates.append(
            {
                "chat_id": chat_id,
                "thread_id": thread_id,
                "project_path": project_path,
                "tmux_session_name": session_name,
            }
        )

    activity, activity_warning = _activity_observation(activity_candidates, settings)
    if activity_warning:
        warnings.append(activity_warning)
    for project in projects:
        data = activity.get(project.composite_key or "")
        project.activity_status = _activity_status(
            running=project.tmux_session_running, activity=data
        )
        if data is not None:
            project.session_running = data.get("session_running")
            project.queue_processing = data.get("queue_processing")
            project.agent_processing = data.get("agent_processing")
            project.is_processing = data.get("is_processing")
            project.queue_busy = data.get("queue_busy")
            project.pending_count = data.get("pending_count")
            project.task_started_at = (
                data.get("task_started_at") if project.activity_status == "working" else None
            )
            project.last_task_completed_at = data.get("last_task_completed_at")
            project.last_task_outcome = data.get("last_task_outcome")
            project.observed_at = data.get("observed_at")

    apply_cached_runtime_selections(projects, state_raw, panes)
    _apply_project_quotas(projects, settings)
    projects.sort(
        key=lambda item: (item.archived, item.topic_name.lower(), item.thread_id, item.chat_id or 0)
    )
    return ObservedProjectSessionsResponse(
        integration=integration,
        project_count=len(projects),
        projects=projects,
        warnings=warnings,
    )


async def refresh_project_runtime_selections(
    settings: AppSettings | None = None,
) -> ObservedProjectSessionsResponse:
    settings = settings or get_settings()
    response = observe_project_sessions(settings)
    state_raw = _read_json(settings.telegram_ai_agent_tmux_state_path)
    panes = _pane_map()
    await refresh_runtime_selection_cache(response.projects, state_raw, panes)
    _apply_project_quotas(response.projects, settings)
    return response


def switch_project_codex_account(
    *,
    project_path: str,
    account_slug: str,
    confirm_interrupt: bool,
    settings: AppSettings | None = None,
) -> ProjectAccountSwitchResponse:
    settings = settings or get_settings()
    integration = integration_status(settings)
    if not integration.switch_actions_allowed:
        raise SessionRuntimeError(
            "integration_incompatible",
            integration.reason or "Интеграция с тмукс-ботом несовместима.",
        )

    resolved_project = _validate_project_path(project_path, settings)
    with _binding_state_lock(settings):
        if _pending_runtime_journal(settings):
            raise SessionRuntimeError(
                "runtime_apply_recovery_required",
                "Runtime apply recovery is required before switching accounts",
            )
        if _runtime_spec_exists_for_project(str(resolved_project), settings):
            raise SessionRuntimeError(
                "runtime_settings_managed",
                "Project account is managed by runtime project settings",
            )
    target_slug = _validate_slug(account_slug)
    target_profile = settings.codex_profiles_root / target_slug
    if not target_profile.is_dir():
        raise SessionRuntimeError("profile_missing", "Профиль Codex не найден.")
    if not (target_profile / "auth.json").is_file():
        raise SessionRuntimeError("auth_missing", "Аккаунт Codex не авторизован.")

    observed = observe_project_sessions(settings)
    project = next(
        (item for item in observed.projects if item.project_path == str(resolved_project)),
        None,
    )
    if project is None:
        raise SessionRuntimeError("project_not_configured", "Проект не найден в тмукс-боте.")
    if project.agent != "codex":
        raise SessionRuntimeError(
            "project_agent_not_codex",
            "Смена аккаунта доступна только для проекта с агентом Codex.",
        )
    if not project.tmux_session_running or not project.tmux_session_name or project.chat_id is None:
        raise SessionRuntimeError(
            "session_not_running",
            "Сначала запустите сессию проекта через тмукс-бота.",
        )
    if not project.account_slug or not project.account_profile_path:
        raise SessionRuntimeError(
            "current_account_unknown",
            "Не удалось определить текущий аккаунт проекта.",
        )
    if project.account_slug == target_slug:
        return ProjectAccountSwitchResponse(
            status="no_change",
            project_path=str(resolved_project),
            topic_name=project.topic_name,
            thread_id=project.thread_id,
            tmux_session_name=project.tmux_session_name,
            previous_account_slug=project.account_slug,
            account_slug=target_slug,
            session_restarted=False,
        )
    if not confirm_interrupt:
        raise SessionRuntimeError(
            "confirmation_required",
            "Переключение требует явного подтверждения потери контекста сессии.",
        )

    payload = {
        "version": 1,
        "action": "switch_codex_account",
        "chat_id": project.chat_id,
        "thread_id": project.thread_id,
        "topic_name": project.topic_name,
        "project_path": str(resolved_project),
        "tmux_session_name": project.tmux_session_name,
        "previous_account_slug": project.account_slug,
        "previous_profile_path": project.account_profile_path,
        "account_slug": target_slug,
        "profile_path": str(target_profile.resolve(strict=False)),
        "confirm_interrupt": True,
    }
    try:
        result = request_bot_control(settings.telegram_ai_agent_control_socket, payload)
    except BotControlError as exc:
        raise SessionRuntimeError(exc.code, exc.message) from exc

    return ProjectAccountSwitchResponse(
        status="switched",
        project_path=str(resolved_project),
        topic_name=project.topic_name,
        thread_id=project.thread_id,
        tmux_session_name=project.tmux_session_name,
        previous_account_slug=project.account_slug,
        account_slug=target_slug,
        session_restarted=bool(result.get("session_restarted")),
        rollback_performed=bool(result.get("rollback_performed")),
    )


def _session_error(
    exc: ProjectSettingsError | LauncherResolveError | RuntimeLaunchSpecStoreError,
) -> SessionRuntimeError:
    return SessionRuntimeError(exc.code, exc.message)


def _matching_project(
    observed: ObservedProjectSessionsResponse,
    project_path: str,
) -> ObservedProjectSession | None:
    for project in observed.projects:
        if not project.project_path:
            continue
        canonical = str(Path(project.project_path).expanduser().resolve(strict=False))
        if canonical == project_path:
            return project
    return None


def _runtime_record_matches_spec(runtime_record: dict[str, Any], launch_spec: Any) -> bool:
    return (
        runtime_record.get("project_id") == launch_spec.project_id
        and runtime_record.get("project_path") == launch_spec.project_path
        and runtime_record.get("provider_id") == launch_spec.provider_id
        and runtime_record.get("account_profile") == launch_spec.account_profile
        and runtime_record.get("model") == launch_spec.model
        and runtime_record.get("reasoning_effort") == launch_spec.reasoning_effort
        and runtime_record.get("settings_revision") == launch_spec.settings_revision
        and runtime_record.get("spec_sha256") == launch_spec.spec_sha256
    )


def _runtime_revision(record: dict[str, Any] | None) -> int | None:
    if record is None:
        return None
    value = record.get("runtime_revision")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _valid_apply_response(
    result: dict[str, Any],
    *,
    expected_model: str,
    expected_reasoning_effort: str,
) -> tuple[bool, int, bool]:
    if result.get("ok") is not True or result.get("status") != "applied":
        raise SessionRuntimeError("control_response_invalid", "Runtime apply response is invalid")
    session_restarted = result.get("session_restarted")
    runtime_revision = result.get("runtime_revision")
    rollback_performed = result.get("rollback_performed")
    session_id = result.get("session_id")
    if (
        not isinstance(session_restarted, bool)
        or isinstance(runtime_revision, bool)
        or not isinstance(runtime_revision, int)
        or runtime_revision < 1
        or rollback_performed is not False
        or not isinstance(session_id, str)
        or not session_id
        or not _SESSION_ID_RE.fullmatch(session_id)
        or result.get("observed_model") != expected_model
        or result.get("observed_reasoning_effort") != expected_reasoning_effort
    ):
        raise SessionRuntimeError("control_response_invalid", "Runtime apply response is invalid")
    return session_restarted, runtime_revision, rollback_performed


def _expected_tmux_session_name(chat_id: int, thread_id: int) -> str:
    chat_part = f"n{-chat_id}" if chat_id < 0 else str(chat_id)
    return f"cc-{chat_part}-{thread_id}"


def bind_project_onboarding_path(
    *,
    project_id: str,
    project_path: str,
    settings: AppSettings | None = None,
) -> ProjectOnboardingPathBindResponse:
    """Bind one observed Telegram topic to a validated owner directory."""
    settings = settings or get_settings()
    integration = integration_status(settings)
    if not integration.switch_actions_allowed:
        raise SessionRuntimeError(
            "integration_incompatible",
            integration.reason or "Runtime integration is incompatible.",
        )
    try:
        canonical = validate_onboarding_project_path(project_path, settings)
    except ProjectOnboardingError as exc:
        raise SessionRuntimeError(exc.code, exc.message) from exc
    project, identity_missing = _persisted_topic_identity(project_id, settings)
    if identity_missing:
        raise SessionRuntimeError(
            "PROJECT_IDENTITY_MISSING",
            "Telegram-идентичность проекта не сохранена. Требуется безопасная миграция topic.",
        )
    if (
        project is None
        or project.onboarding_state != "UNCONFIGURED"
        or project.project_path is not None
    ):
        raise SessionRuntimeError(
            "PROJECT_NOT_CONFIGURED",
            "Новый Telegram-проект не найден или уже привязан.",
        )
    payload = {
        "version": 5,
        "action": "bind_project_topic_path",
        "project_id": project.project_id,
        "chat_id": project.chat_id,
        "thread_id": project.thread_id,
        "project_path": canonical,
    }
    try:
        result = request_bot_control(settings.telegram_ai_agent_control_socket, payload)
    except BotControlError as exc:
        raise SessionRuntimeError(exc.code, exc.message) from exc
    if (
        result.get("status") != "bound"
        or result.get("project_path") != canonical
        or result.get("session_started") is not False
    ):
        raise SessionRuntimeError("control_response_invalid", "Path binding response is invalid.")
    refreshed = observe_project_sessions(settings)
    bound = next(
        (
            item
            for item in refreshed.projects
            if item.chat_id == project.chat_id
            and item.thread_id == project.thread_id
            and item.project_path == canonical
        ),
        None,
    )
    if bound is None or bound.onboarding_state != "UNCONFIGURED" or bound.tmux_session_running:
        raise SessionRuntimeError(
            "post_apply_verification_failed", "Path binding was not observed."
        )
    if bound.project_id is None:
        raise SessionRuntimeError("control_response_invalid", "Bound project identity is missing.")
    return ProjectOnboardingPathBindResponse(
        previous_project_id=project_id,
        project_id=bound.project_id,
        chat_id=project.chat_id,
        thread_id=project.thread_id,
        project_path=canonical,
        missing_launch_fields=bound.missing_launch_fields,
    )


def apply_project_onboarding_selection(
    *,
    project_path: str,
    provider_id: str,
    account_profile: str,
    model: str,
    reasoning_effort: str,
    expected_settings_revision: int | None,
    confirm: bool,
    settings: AppSettings | None = None,
) -> ProjectOnboardingApplyResponse:
    """Prepare one exact first launch without starting Codex or tmux."""
    settings = settings or get_settings()
    if not confirm:
        raise SessionRuntimeError("confirmation_required", "Onboarding requires confirmation.")
    if not account_profile.strip():
        raise SessionRuntimeError("ACCOUNT_REQUIRED", "Выберите авторизованный аккаунт.")
    if not model.strip():
        raise SessionRuntimeError("MODEL_REQUIRED", "Выберите модель.")
    if not reasoning_effort.strip():
        raise SessionRuntimeError("RUNTIME_SPEC_INVALID", "Выберите reasoning.")
    integration = integration_status(settings)
    if not integration.switch_actions_allowed:
        raise SessionRuntimeError(
            "integration_incompatible",
            integration.reason or "Runtime integration is incompatible.",
        )
    try:
        canonical = validate_onboarding_project_path(project_path, settings)
        normalized_provider = provider_id.strip().lower()
        validation = validate_provider_project_settings(
            provider_id=normalized_provider,
            account_profile=account_profile,
            model=model,
            reasoning_effort=reasoning_effort,
            settings=settings,
        )
    except ProjectOnboardingError as exc:
        raise SessionRuntimeError(exc.code, exc.message) from exc
    except ProjectSettingsError as exc:
        raise _session_error(exc) from exc
    if normalized_provider != "codex":
        raise SessionRuntimeError(
            "provider_settings_not_supported",
            "Provider onboarding is not implemented.",
        )
    if validation.capability_source != "current":
        raise SessionRuntimeError(
            "capabilities_current_required",
            "Current provider capabilities are required.",
        )
    observed = observe_project_sessions(settings)
    project = _matching_project(observed, canonical)
    if (
        project is None
        or project.onboarding_state != "UNCONFIGURED"
        or project.chat_id is None
        or project.topic_status == "archived"
    ):
        raise SessionRuntimeError(
            "PROJECT_NOT_CONFIGURED",
            "Telegram-проект не ожидает завершения настройки.",
        )
    project_id = project_id_for_path(canonical)
    try:
        store, _migration_required = read_project_settings_store(settings)
        current_raw = store.get("projects", {}).get(project_id)
        current_record = (
            None if current_raw is None else ProjectSettingsRecord.model_validate(current_raw)
        )
    except (ProjectSettingsError, ValueError) as exc:
        raise SessionRuntimeError(
            "settings_store_invalid", "Project settings are invalid."
        ) from exc
    if current_record is None:
        if expected_settings_revision is not None:
            raise SessionRuntimeError("revision_conflict", "Project settings revision conflict")
    elif expected_settings_revision != current_record.revision:
        raise SessionRuntimeError("revision_conflict", "Project settings revision conflict")

    temporary_record: ProjectSettingsRecord | None = None
    previous_record = current_record
    selection_already_desired = current_record is not None and _selection_matches_record(
        current_record,
        project_path=canonical,
        provider_id=normalized_provider,
        account_profile=account_profile,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    target_record: ProjectSettingsRecord
    if selection_already_desired and current_record is not None:
        target_record = current_record
    else:
        try:
            target_record, previous_record = upsert_project_settings_with_snapshot(
                project_path=canonical,
                provider_id=normalized_provider,
                account_profile=account_profile,
                model=model,
                reasoning_effort=reasoning_effort,
                expected_revision=expected_settings_revision,
                settings=settings,
            )
            temporary_record = target_record
        except ProjectSettingsError as exc:
            raise _session_error(exc) from exc

    def restore_desired() -> None:
        if temporary_record is None:
            return
        try:
            restore_project_settings_record(
                project_id=temporary_record.project_id,
                project_path=temporary_record.project_path,
                expected_current=temporary_record,
                previous_record=previous_record,
                settings=settings,
            )
        except ProjectSettingsError as exc:
            raise SessionRuntimeError(
                "settings_apply_rollback_failed",
                "Runtime recovered but desired settings could not be restored.",
            ) from exc

    try:
        launch_spec = build_project_launch_spec(
            project_id=target_record.project_id,
            expected_settings_revision=target_record.revision,
            allow_last_known_good=False,
            settings=settings,
        )
        current_runtime = get_runtime_launch_spec(
            settings.runtime_launch_specs_path,
            target_record.project_id,
        )
    except (ProjectSettingsError, LauncherResolveError, RuntimeLaunchSpecStoreError) as exc:
        restore_desired()
        raise SessionRuntimeError(
            "RUNTIME_SPEC_INVALID", "Runtime launch spec is invalid."
        ) from exc
    current_runtime_revision = _runtime_revision(current_runtime)
    payload = {
        "version": 6,
        "action": "configure_project_onboarding",
        "chat_id": project.chat_id,
        "thread_id": project.thread_id,
        "project_id": launch_spec.project_id,
        "project_path": launch_spec.project_path,
        "settings_revision": launch_spec.settings_revision,
        "expected_runtime_revision": current_runtime_revision,
        "account_profile": launch_spec.account_profile,
        "model": launch_spec.model,
        "reasoning_effort": launch_spec.reasoning_effort,
        "spec_sha256": launch_spec.spec_sha256,
        "expected_tmux_session_name": _expected_tmux_session_name(
            project.chat_id, project.thread_id
        ),
        "confirm": True,
    }
    try:
        result = request_bot_control(
            settings.telegram_ai_agent_control_socket,
            payload,
            timeout_seconds=50.0,
        )
    except BotControlError as exc:
        if exc.code not in {"runtime_apply_recovery_required", "rollback_failed"}:
            restore_desired()
        raise SessionRuntimeError(exc.code, exc.message) from exc
    runtime_revision = result.get("runtime_revision")
    if (
        result.get("status") != "configured"
        or result.get("session_started") is not False
        or isinstance(runtime_revision, bool)
        or not isinstance(runtime_revision, int)
        or runtime_revision < 1
        or result.get("spec_sha256") != launch_spec.spec_sha256
    ):
        raise SessionRuntimeError("control_response_invalid", "Onboarding response is invalid.")
    try:
        applied_runtime = get_runtime_launch_spec(
            settings.runtime_launch_specs_path,
            target_record.project_id,
        )
    except RuntimeLaunchSpecStoreError as exc:
        raise SessionRuntimeError(
            "RUNTIME_SPEC_INVALID", "Runtime launch spec is invalid."
        ) from exc
    if (
        applied_runtime is None
        or not _runtime_record_matches_spec(applied_runtime, launch_spec)
        or applied_runtime.get("runtime_revision") != runtime_revision
    ):
        raise SessionRuntimeError("RUNTIME_SPEC_INVALID", "Runtime launch spec was not persisted.")
    post = observe_project_sessions(settings)
    configured = _matching_project(post, launch_spec.project_path)
    if (
        configured is None
        or configured.onboarding_state != "CONFIGURED"
        or configured.tmux_session_running
        or configured.account_slug != launch_spec.account_profile
    ):
        raise SessionRuntimeError(
            "post_apply_verification_failed",
            "Configured first-launch state was not observed.",
        )
    return ProjectOnboardingApplyResponse(
        project_id=launch_spec.project_id,
        project_path=launch_spec.project_path,
        chat_id=project.chat_id,
        thread_id=project.thread_id,
        account_profile=launch_spec.account_profile,
        model=launch_spec.model,
        reasoning_effort=launch_spec.reasoning_effort,
        settings_revision=launch_spec.settings_revision,
        runtime_revision=runtime_revision,
        spec_sha256=launch_spec.spec_sha256,
    )


def _selection_matches_record(
    record: ProjectSettingsRecord,
    *,
    project_path: str,
    provider_id: str,
    account_profile: str,
    model: str,
    reasoning_effort: str,
) -> bool:
    return (
        record.project_path == project_path
        and record.provider_id == provider_id
        and record.account_profile == account_profile
        and record.model == model
        and record.reasoning_effort == reasoning_effort
    )


def _valid_selection_apply_response(
    result: dict[str, Any],
    *,
    expected_session_running: bool,
    expected_model: str,
    expected_reasoning_effort: str,
) -> tuple[str, str, int, bool, bool, bool]:
    status_value = result.get("status")
    action = result.get("session_action")
    session_started = result.get("session_started")
    session_restarted = result.get("session_restarted")
    rollback_performed = result.get("rollback_performed")
    runtime_revision = result.get("runtime_revision")
    session_id = result.get("session_id")
    if (
        result.get("ok") is not True
        or status_value not in {"applied", "no_change"}
        or action not in {"started", "restarted", "unchanged"}
        or not isinstance(session_started, bool)
        or not isinstance(session_restarted, bool)
        or not isinstance(rollback_performed, bool)
        or isinstance(runtime_revision, bool)
        or not isinstance(runtime_revision, int)
        or runtime_revision < 1
        or not isinstance(session_id, str)
        or not session_id
        or not _SESSION_ID_RE.fullmatch(session_id)
        or result.get("observed_model") != expected_model
        or result.get("observed_reasoning_effort") != expected_reasoning_effort
    ):
        raise SessionRuntimeError("control_response_invalid", "Runtime apply response is invalid")
    if rollback_performed:
        raise SessionRuntimeError("control_response_invalid", "Runtime apply response is invalid")
    if status_value == "no_change" and not expected_session_running:
        raise SessionRuntimeError("control_response_invalid", "Stopped sessions must be started")
    expected = (
        ("no_change", "unchanged", False, False)
        if status_value == "no_change"
        else (
            ("applied", "restarted", False, True)
            if expected_session_running
            else ("applied", "started", True, False)
        )
    )
    if (status_value, action, session_started, session_restarted) != expected:
        raise SessionRuntimeError("control_response_invalid", "Runtime apply response is invalid")
    return (
        status_value,
        action,
        runtime_revision,
        session_started,
        session_restarted,
        rollback_performed,
    )


def apply_project_codex_selection(
    *,
    project_path: str,
    provider_id: str,
    account_profile: str,
    model: str,
    reasoning_effort: str,
    expected_settings_revision: int | None,
    confirm_interrupt: bool,
    allow_last_known_good: bool,
    settings: AppSettings | None = None,
) -> ProjectSettingsApplyResponse:
    """Atomically persist a Codex selection and start or restart its exact topic."""
    settings = settings or get_settings()
    if not confirm_interrupt:
        raise SessionRuntimeError("confirmation_required", "Runtime apply requires confirmation.")
    if allow_last_known_good:
        raise SessionRuntimeError(
            "invalid_request", "Last-known-good capabilities are not allowed."
        )

    integration = integration_status(settings)
    if not integration.switch_actions_allowed:
        raise SessionRuntimeError(
            "integration_incompatible",
            integration.reason or "Runtime integration is incompatible.",
        )

    try:
        canonical = str(canonical_project_path(project_path, settings))
        normalized_provider = provider_id.strip().lower()
        validation = validate_provider_project_settings(
            provider_id=normalized_provider,
            account_profile=account_profile,
            model=model,
            reasoning_effort=reasoning_effort,
            settings=settings,
        )
    except ProjectSettingsError as exc:
        raise _session_error(exc) from exc
    if normalized_provider != "codex":
        raise SessionRuntimeError(
            "provider_settings_not_supported", "Provider runtime apply is not supported."
        )
    if validation.capability_source != "current":
        raise SessionRuntimeError(
            "capabilities_current_required", "Current Codex capabilities are required."
        )

    with _binding_state_lock(settings):
        if _pending_runtime_journal(settings):
            raise SessionRuntimeError(
                "runtime_apply_recovery_required",
                "Runtime apply recovery is required before applying settings",
            )

    observed = observe_project_sessions(settings)
    project = _matching_project(observed, canonical)
    expected_project_id = project_id_for_path(canonical)
    if (
        project is None
        or project.project_id != expected_project_id
        or project.topic_status != "configured"
    ):
        raise SessionRuntimeError(
            "project_not_configured", "Project is not an exact configured Telegram topic."
        )
    if project.agent != "codex":
        raise SessionRuntimeError(
            "project_agent_not_codex", "Runtime apply is available only for Codex projects."
        )
    if project.chat_id is None:
        raise SessionRuntimeError(
            "session_state_mismatch", "Telegram topic identity is incomplete."
        )
    expected_tmux_name = _expected_tmux_session_name(project.chat_id, project.thread_id)
    expected_running = project.tmux_session_running
    if expected_running and project.tmux_session_name != expected_tmux_name:
        raise SessionRuntimeError("session_mismatch", "Project session identity changed.")

    try:
        store, _migration_required = read_project_settings_store(settings)
        current_raw = store.get("projects", {}).get(expected_project_id)
        current_record = (
            None if current_raw is None else ProjectSettingsRecord.model_validate(current_raw)
        )
    except (ProjectSettingsError, ValueError) as exc:
        if isinstance(exc, ProjectSettingsError):
            raise _session_error(exc) from exc
        raise SessionRuntimeError(
            "settings_store_invalid", "Project settings are invalid."
        ) from exc
    if current_record is None:
        if expected_settings_revision is not None:
            raise SessionRuntimeError("revision_conflict", "Project settings revision conflict")
    elif expected_settings_revision != current_record.revision:
        raise SessionRuntimeError("revision_conflict", "Project settings revision conflict")

    temporary_record: ProjectSettingsRecord | None = None
    previous_record: ProjectSettingsRecord | None = current_record
    selection_already_desired = current_record is not None and _selection_matches_record(
        current_record,
        project_path=canonical,
        provider_id=normalized_provider,
        account_profile=account_profile,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    if selection_already_desired:
        target_record = current_record
    else:
        try:
            target_record, previous_record = upsert_project_settings_with_snapshot(
                project_path=canonical,
                provider_id=normalized_provider,
                account_profile=account_profile,
                model=model,
                reasoning_effort=reasoning_effort,
                expected_revision=expected_settings_revision,
                settings=settings,
            )
            temporary_record = target_record
        except ProjectSettingsError as exc:
            raise _session_error(exc) from exc

    def restore_temporary_desired() -> None:
        if temporary_record is None:
            return
        try:
            restore_project_settings_record(
                project_id=temporary_record.project_id,
                project_path=temporary_record.project_path,
                expected_current=temporary_record,
                previous_record=previous_record,
                settings=settings,
            )
        except ProjectSettingsError as exc:
            raise SessionRuntimeError(
                "settings_apply_rollback_failed",
                "Runtime recovered but desired settings could not be restored.",
            ) from exc

    try:
        launch_spec = build_project_launch_spec(
            project_id=target_record.project_id,
            expected_settings_revision=target_record.revision,
            allow_last_known_good=False,
            settings=settings,
        )
        current_runtime = get_runtime_launch_spec(
            settings.runtime_launch_specs_path,
            target_record.project_id,
        )
    except (ProjectSettingsError, LauncherResolveError, RuntimeLaunchSpecStoreError) as exc:
        restore_temporary_desired()
        raise _session_error(exc) from exc

    current_runtime_revision = _runtime_revision(current_runtime)
    previous_account = (
        current_runtime.get("account_profile") if isinstance(current_runtime, dict) else None
    )
    previous_model = current_runtime.get("model") if isinstance(current_runtime, dict) else None
    previous_reasoning = (
        current_runtime.get("reasoning_effort") if isinstance(current_runtime, dict) else None
    )
    payload = {
        "version": 4,
        "action": "apply_codex_project_selection",
        "chat_id": project.chat_id,
        "thread_id": project.thread_id,
        "project_id": launch_spec.project_id,
        "project_path": launch_spec.project_path,
        "settings_revision": launch_spec.settings_revision,
        "expected_runtime_revision": current_runtime_revision,
        "account_profile": launch_spec.account_profile,
        "model": launch_spec.model,
        "reasoning_effort": launch_spec.reasoning_effort,
        "spec_sha256": launch_spec.spec_sha256,
        "expected_session_running": expected_running,
        "expected_tmux_session_name": expected_tmux_name,
        "confirm_interrupt": True,
    }
    try:
        result = request_bot_control(
            settings.telegram_ai_agent_control_socket,
            payload,
            timeout_seconds=50.0,
        )
    except BotControlError as exc:
        critical_codes = {
            "apply_failed_rollback_failed",
            "rollback_failed",
            "runtime_apply_recovery_required",
        }
        journal_pending = _pending_runtime_journal(settings)
        if exc.code not in critical_codes and not journal_pending:
            restore_temporary_desired()
        raise SessionRuntimeError(exc.code, exc.message) from exc

    (
        apply_status,
        session_action,
        runtime_revision,
        session_started,
        session_restarted,
        rollback_performed,
    ) = _valid_selection_apply_response(
        result,
        expected_session_running=expected_running,
        expected_model=launch_spec.model,
        expected_reasoning_effort=launch_spec.reasoning_effort,
    )
    clear_runtime_selection_cache()

    try:
        applied_runtime = get_runtime_launch_spec(
            settings.runtime_launch_specs_path,
            target_record.project_id,
        )
    except RuntimeLaunchSpecStoreError as exc:
        raise _session_error(exc) from exc
    if (
        applied_runtime is None
        or not _runtime_record_matches_spec(applied_runtime, launch_spec)
        or applied_runtime.get("runtime_revision") != runtime_revision
    ):
        raise SessionRuntimeError(
            "post_apply_verification_failed",
            "Runtime apply postcondition verification failed.",
        )

    post_observed = observe_project_sessions(settings)
    post_project = _matching_project(post_observed, launch_spec.project_path)
    if (
        post_project is None
        or not post_project.tmux_session_running
        or post_project.tmux_session_name != expected_tmux_name
        or post_project.account_slug != launch_spec.account_profile
    ):
        raise SessionRuntimeError(
            "post_apply_verification_failed",
            "Runtime process verification failed after apply.",
        )

    return ProjectSettingsApplyResponse(
        status=apply_status,
        project_id=launch_spec.project_id,
        project_path=launch_spec.project_path,
        topic_name=post_project.topic_name,
        thread_id=post_project.thread_id,
        tmux_session_name=expected_tmux_name,
        settings_revision=launch_spec.settings_revision,
        runtime_revision=runtime_revision,
        account_profile=launch_spec.account_profile,
        model=launch_spec.model,
        reasoning_effort=launch_spec.reasoning_effort,
        spec_sha256=launch_spec.spec_sha256,
        session_action=session_action,
        session_started=session_started,
        session_restarted=session_restarted,
        rollback_performed=rollback_performed,
        previous_account_profile=previous_account if isinstance(previous_account, str) else None,
        previous_model=previous_model if isinstance(previous_model, str) else None,
        previous_reasoning_effort=previous_reasoning
        if isinstance(previous_reasoning, str)
        else None,
        warnings=list(launch_spec.warnings),
    )


def apply_project_codex_settings(
    *,
    project_id: str,
    expected_settings_revision: int,
    confirm_interrupt: bool,
    allow_last_known_good: bool = False,
    settings: AppSettings | None = None,
) -> ProjectSettingsApplyResponse:
    settings = settings or get_settings()
    try:
        normalized_project_id = validate_project_id(project_id)
    except ProjectSettingsError as exc:
        raise _session_error(exc) from exc

    if not confirm_interrupt:
        raise SessionRuntimeError(
            "confirmation_required",
            "Runtime apply requires explicit confirmation.",
        )

    integration = integration_status(settings)
    if not integration.switch_actions_allowed:
        raise SessionRuntimeError(
            "integration_incompatible",
            integration.reason or "Runtime integration is incompatible.",
        )

    with _binding_state_lock(settings):
        if _pending_runtime_journal(settings):
            raise SessionRuntimeError(
                "runtime_apply_recovery_required",
                "Runtime apply recovery is required before applying settings",
            )

    try:
        launch_spec = build_project_launch_spec(
            project_id=normalized_project_id,
            expected_settings_revision=expected_settings_revision,
            allow_last_known_good=allow_last_known_good,
            settings=settings,
        )
    except (ProjectSettingsError, LauncherResolveError) as exc:
        raise _session_error(exc) from exc

    if launch_spec.provider_id != "codex":
        raise SessionRuntimeError(
            "provider_settings_not_supported",
            "Provider runtime apply is not supported.",
        )

    try:
        current_runtime = get_runtime_launch_spec(
            settings.runtime_launch_specs_path,
            normalized_project_id,
        )
    except RuntimeLaunchSpecStoreError as exc:
        raise _session_error(exc) from exc

    current_runtime_revision = _runtime_revision(current_runtime)
    previous_account = (
        current_runtime.get("account_profile") if isinstance(current_runtime, dict) else None
    )
    previous_model = current_runtime.get("model") if isinstance(current_runtime, dict) else None
    previous_reasoning = (
        current_runtime.get("reasoning_effort") if isinstance(current_runtime, dict) else None
    )

    observed = observe_project_sessions(settings)
    project = _matching_project(observed, launch_spec.project_path)
    if project is None:
        raise SessionRuntimeError(
            "project_not_configured", "Project is not configured in Telegram topics."
        )
    if project.agent != "codex":
        raise SessionRuntimeError(
            "project_agent_not_codex",
            "Runtime apply is available only for Codex projects.",
        )
    if not project.tmux_session_running:
        raise SessionRuntimeError("session_not_running", "Project session is not running.")
    if project.chat_id is None or not project.tmux_session_name:
        raise SessionRuntimeError("session_mismatch", "Project session metadata is incomplete.")
    if (
        not project.project_path
        or str(Path(project.project_path).expanduser().resolve(strict=False))
        != launch_spec.project_path
    ):
        raise SessionRuntimeError(
            "session_mismatch", "Project session path does not match settings."
        )

    payload = {
        "version": 2,
        "action": "apply_codex_project_settings",
        "chat_id": project.chat_id,
        "thread_id": project.thread_id,
        "project_id": launch_spec.project_id,
        "project_path": launch_spec.project_path,
        "tmux_session_name": project.tmux_session_name,
        "settings_revision": launch_spec.settings_revision,
        "expected_runtime_revision": current_runtime_revision,
        "account_profile": launch_spec.account_profile,
        "model": launch_spec.model,
        "reasoning_effort": launch_spec.reasoning_effort,
        "spec_sha256": launch_spec.spec_sha256,
        "confirm_interrupt": True,
    }
    try:
        result = request_bot_control(
            settings.telegram_ai_agent_control_socket,
            payload,
            timeout_seconds=50.0,
        )
    except BotControlError as exc:
        raise SessionRuntimeError(exc.code, exc.message) from exc

    session_restarted, runtime_revision, rollback_performed = _valid_apply_response(
        result,
        expected_model=launch_spec.model,
        expected_reasoning_effort=launch_spec.reasoning_effort,
    )
    clear_runtime_selection_cache()

    try:
        applied_runtime = get_runtime_launch_spec(
            settings.runtime_launch_specs_path,
            normalized_project_id,
        )
    except RuntimeLaunchSpecStoreError as exc:
        raise _session_error(exc) from exc
    if (
        applied_runtime is None
        or not _runtime_record_matches_spec(applied_runtime, launch_spec)
        or applied_runtime.get("runtime_revision") != runtime_revision
    ):
        raise SessionRuntimeError(
            "post_apply_verification_failed",
            "Runtime apply postcondition verification failed.",
        )

    post_observed = observe_project_sessions(settings)
    post_project = _matching_project(post_observed, launch_spec.project_path)
    if (
        post_project is None
        or not post_project.tmux_session_running
        or post_project.account_slug != launch_spec.account_profile
    ):
        raise SessionRuntimeError(
            "post_apply_verification_failed",
            "Runtime process verification failed after apply.",
        )

    return ProjectSettingsApplyResponse(
        status="applied",
        project_id=launch_spec.project_id,
        project_path=launch_spec.project_path,
        topic_name=post_project.topic_name,
        thread_id=post_project.thread_id,
        tmux_session_name=post_project.tmux_session_name or project.tmux_session_name,
        settings_revision=launch_spec.settings_revision,
        runtime_revision=runtime_revision,
        account_profile=launch_spec.account_profile,
        model=launch_spec.model,
        reasoning_effort=launch_spec.reasoning_effort,
        spec_sha256=launch_spec.spec_sha256,
        session_action="restarted",
        session_started=False,
        session_restarted=session_restarted,
        rollback_performed=rollback_performed,
        previous_account_profile=previous_account if isinstance(previous_account, str) else None,
        previous_model=previous_model if isinstance(previous_model, str) else None,
        previous_reasoning_effort=previous_reasoning
        if isinstance(previous_reasoning, str)
        else None,
        warnings=list(launch_spec.warnings),
    )
