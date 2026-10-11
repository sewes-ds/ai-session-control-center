from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from pydantic import ValidationError

from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.providers.codex.app_server import (
    CodexAppServerError,
    CodexAppServerProbeResult,
    MethodResult,
    resolve_profile,
    run_codex_app_server_probe,
    validate_account_slug,
)
from ai_account_manager.schemas.codex import (
    CodexCapabilitiesSnapshot,
    CodexCapabilityAccountSection,
    CodexCapabilityModelItem,
    CodexCapabilityModelsSection,
    CodexCapabilityRateLimitBucket,
    CodexCapabilityRateLimitWindow,
    CodexCapabilityRateLimitsSection,
    CodexCapabilityResetCreditItem,
    CodexCapabilityResetCredits,
    CodexCapabilityUsageSection,
)

_TOKEN_LIKE_RE = re.compile(
    r"(Bearer\s+[A-Za-z0-9._~+/=-]{16,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
)


class CodexCapabilitiesError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


ProbeRunner = Callable[..., CodexAppServerProbeResult]


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _cache_root(settings: AppSettings) -> Path:
    root = settings.codex_capabilities_cache_root
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def cache_path(settings: AppSettings, account_slug: str) -> Path:
    return _cache_root(settings) / f"{validate_account_slug(account_slug)}.json"


def lock_path(settings: AppSettings, account_slug: str) -> Path:
    return _cache_root(settings) / f"{validate_account_slug(account_slug)}.lock"


@contextmanager
def _account_lock(settings: AppSettings, account_slug: str) -> Iterator[None]:
    path = lock_path(settings, account_slug)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        pass


def _empty_snapshot(
    account_slug: str,
    profile_path: Path,
    *,
    status: str = "not_refreshed",
    error_code: str | None = None,
    error_message: str | None = None,
) -> CodexCapabilitiesSnapshot:
    return CodexCapabilitiesSnapshot(
        account_profile=account_slug,
        profile_path=str(profile_path),
        status=status,
        profile_integrity_status="not_checked",
        account=CodexCapabilityAccountSection(status="not_refreshed"),
        models=CodexCapabilityModelsSection(status="not_refreshed"),
        rate_limits=CodexCapabilityRateLimitsSection(status="not_refreshed"),
        usage=CodexCapabilityUsageSection(status="not_refreshed"),
        error_code=error_code,
        error_message=error_message,
    )


def _snapshot_has_data(snapshot: CodexCapabilitiesSnapshot) -> bool:
    return bool(
        snapshot.account.authorized is not None
        or snapshot.models.items
        or snapshot.rate_limits.buckets
        or snapshot.rate_limits.reset_credits.available_count is not None
        or snapshot.usage.summary
        or snapshot.usage.daily_usage_buckets
    )


def _safe_message(message: str | None) -> str | None:
    if not message:
        return None
    return _TOKEN_LIKE_RE.sub("[REDACTED]", message)[:200]


def _quarantine_invalid(path: Path) -> None:
    if not path.exists():
        return
    stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.{stamp}.invalid")
    os.replace(path, target)
    os.chmod(target, 0o600)


def read_cached_codex_capabilities(
    account_slug: str,
    *,
    settings: AppSettings | None = None,
) -> CodexCapabilitiesSnapshot:
    settings = settings or get_settings()
    try:
        slug = validate_account_slug(account_slug)
        profile_dir = resolve_profile(settings, slug)
        path = cache_path(settings, slug)
        with _account_lock(settings, slug):
            return _read_cached_unlocked(slug, profile_dir, path)
    except CodexAppServerError as exc:
        raise CodexCapabilitiesError(exc.code, exc.message) from exc


def _read_cached_unlocked(
    account_slug: str,
    profile_dir: Path,
    path: Path,
) -> CodexCapabilitiesSnapshot:
    if not path.exists():
        return _empty_snapshot(account_slug, profile_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return CodexCapabilitiesSnapshot.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError):
        _quarantine_invalid(path)
        return _empty_snapshot(account_slug, profile_dir, status="not_refreshed")


def _write_snapshot(path: Path, snapshot: CodexCapabilitiesSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(snapshot.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _mask_email(email: str) -> str:
    local, domain = email.split("@", 1)
    masked_local = (local[:1] + "***" + local[-1:]) if len(local) > 1 else local[:1] + "***"
    parts = domain.split(".")
    if len(parts) > 1:
        first = parts[0]
        masked_domain = (first[:1] + "***" + first[-1:]) if len(first) > 1 else first[:1] + "***"
        return masked_local + "@" + masked_domain + "." + ".".join(parts[1:])
    return masked_local + "@***"


def _safe_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result = [item.strip() for item in values if isinstance(item, str) and item.strip()]
    return sorted(set(result))


def _json_safe_object(raw: Any) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None
    allowed: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            allowed[key] = value
    return allowed


def _normalize_account(method: MethodResult, discovered_at: str) -> CodexCapabilityAccountSection:
    if method.status != "ok" or not isinstance(method.result, dict):
        return CodexCapabilityAccountSection(
            status="error",
            error_code=method.error_code,
            error_message=_safe_message(method.error_message),
        )
    account = method.result.get("account")
    requires_auth = bool(method.result.get("requiresOpenaiAuth"))
    if not isinstance(account, dict):
        return CodexCapabilityAccountSection(
            status="ok",
            authorized=not requires_auth,
            discovered_at=discovered_at,
        )
    email = account.get("email")
    email_safe = email.strip() if isinstance(email, str) and "@" in email else None
    return CodexCapabilityAccountSection(
        status="ok",
        authorized=True,
        account_type=account.get("type") if isinstance(account.get("type"), str) else None,
        plan_type=account.get("planType") if isinstance(account.get("planType"), str) else None,
        safe_identifier=(
            "email_sha256:" + hashlib.sha256(email_safe.lower().encode()).hexdigest()[:12]
            if email_safe
            else None
        ),
        email_masked=_mask_email(email_safe) if email_safe else None,
        discovered_at=discovered_at,
    )


def _normalize_models(method: MethodResult, discovered_at: str) -> CodexCapabilityModelsSection:
    if method.status != "ok" or not isinstance(method.result, dict):
        return CodexCapabilityModelsSection(
            status="error",
            error_code=method.error_code,
            error_message=_safe_message(method.error_message),
        )
    raw_items = method.result.get("data")
    if not isinstance(raw_items, list):
        return CodexCapabilityModelsSection(status="error", error_code="model_response_invalid")
    items: list[CodexCapabilityModelItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        model_id = raw.get("model") or raw.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        efforts = []
        for item in raw.get("supportedReasoningEfforts") or []:
            if isinstance(item, dict):
                effort = item.get("reasoningEffort")
            else:
                effort = item
            if isinstance(effort, str) and effort.strip():
                efforts.append(effort.strip())
        items.append(
            CodexCapabilityModelItem(
                model_id=model_id.strip(),
                display_name=raw.get("displayName") if isinstance(raw.get("displayName"), str) else None,
                hidden=raw.get("hidden") if isinstance(raw.get("hidden"), bool) else None,
                is_default=raw.get("isDefault") if isinstance(raw.get("isDefault"), bool) else None,
                default_reasoning_effort=(
                    raw.get("defaultReasoningEffort")
                    if isinstance(raw.get("defaultReasoningEffort"), str)
                    else None
                ),
                supported_reasoning_efforts=sorted(set(efforts)),
                input_modalities=_safe_strings(raw.get("inputModalities")),
                supports_personality=(
                    raw.get("supportsPersonality")
                    if isinstance(raw.get("supportsPersonality"), bool)
                    else None
                ),
            )
        )
    if not items:
        return CodexCapabilityModelsSection(status="error", error_code="empty_models")
    return CodexCapabilityModelsSection(status="ok", discovered_at=discovered_at, items=items)


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _normalize_window(window_type: str, raw: Any) -> CodexCapabilityRateLimitWindow | None:
    if not isinstance(raw, dict):
        return None
    used = _float_or_none(raw.get("usedPercent"))
    remaining = None if used is None else max(0.0, min(100.0, 100.0 - used))
    return CodexCapabilityRateLimitWindow(
        window_type=window_type,
        used_percent=used,
        remaining_percent=remaining,
        window_duration_minutes=_int_or_none(raw.get("windowDurationMins")),
        resets_at=raw.get("resetsAt") if isinstance(raw.get("resetsAt"), (int, str)) else None,
    )


def _normalize_bucket(raw: Any, fallback_limit_id: str | None = None) -> CodexCapabilityRateLimitBucket | None:
    if not isinstance(raw, dict):
        return None
    windows = []
    for key in ("primary", "secondary"):
        window = _normalize_window(key, raw.get(key))
        if window is not None:
            windows.append(window)
    return CodexCapabilityRateLimitBucket(
        limit_id=raw.get("limitId") if isinstance(raw.get("limitId"), str) else fallback_limit_id,
        limit_name=raw.get("limitName") if isinstance(raw.get("limitName"), str) else None,
        plan_type=raw.get("planType") if isinstance(raw.get("planType"), str) else None,
        rate_limit_reached_type=(
            raw.get("rateLimitReachedType")
            if isinstance(raw.get("rateLimitReachedType"), str)
            else None
        ),
        windows=windows,
    )


def _normalize_rate_limits(method: MethodResult, discovered_at: str) -> CodexCapabilityRateLimitsSection:
    if method.status != "ok" or not isinstance(method.result, dict):
        return CodexCapabilityRateLimitsSection(
            status="error",
            error_code=method.error_code,
            error_message=_safe_message(method.error_message),
        )
    buckets: list[CodexCapabilityRateLimitBucket] = []
    by_id = method.result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        for key, value in sorted(by_id.items()):
            bucket = _normalize_bucket(value, str(key))
            if bucket is not None:
                buckets.append(bucket)
    else:
        bucket = _normalize_bucket(method.result.get("rateLimits"))
        if bucket is not None:
            buckets.append(bucket)
    reset_raw = method.result.get("rateLimitResetCredits")
    reset = CodexCapabilityResetCredits()
    if isinstance(reset_raw, dict):
        count = reset_raw.get("availableCount")
        reset.available_count = count if isinstance(count, int) else None
        credits = reset_raw.get("credits")
        if isinstance(credits, list):
            for credit in credits:
                if not isinstance(credit, dict):
                    continue
                raw_id = credit.get("id")
                reset.items.append(
                    CodexCapabilityResetCreditItem(
                        credit_id_sha256_12=(
                            hashlib.sha256(raw_id.encode()).hexdigest()[:12]
                            if isinstance(raw_id, str)
                            else None
                        ),
                        reset_type=credit.get("resetType") if isinstance(credit.get("resetType"), str) else None,
                        status=credit.get("status") if isinstance(credit.get("status"), str) else None,
                        granted_at=(
                            credit.get("grantedAt")
                            if isinstance(credit.get("grantedAt"), (int, str))
                            else None
                        ),
                        expires_at=(
                            credit.get("expiresAt")
                            if isinstance(credit.get("expiresAt"), (int, str))
                            else None
                        ),
                        title=credit.get("title") if isinstance(credit.get("title"), str) else None,
                        description=(
                            credit.get("description")
                            if isinstance(credit.get("description"), str)
                            else None
                        ),
                    )
                )
    credits_obj = None
    legacy = method.result.get("rateLimits")
    if isinstance(legacy, dict):
        credits_obj = _json_safe_object(legacy.get("credits"))
    return CodexCapabilityRateLimitsSection(
        status="ok",
        discovered_at=discovered_at,
        buckets=buckets,
        credits=credits_obj,
        reset_credits=reset,
    )


def _normalize_json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if isinstance(key, str) and not re.search("token|cookie|authorization|secret", key, re.I):
                normalized = _normalize_json_safe(item)
                if normalized is not None:
                    result[key] = normalized
        return result
    if isinstance(value, list):
        return [item for item in (_normalize_json_safe(item) for item in value) if item is not None]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def _normalize_usage(method: MethodResult, discovered_at: str) -> CodexCapabilityUsageSection:
    if method.status != "ok" or not isinstance(method.result, dict):
        return CodexCapabilityUsageSection(
            status="error",
            error_code=method.error_code,
            error_message=_safe_message(method.error_message),
        )
    summary = _normalize_json_safe(method.result.get("summary"))
    daily = _normalize_json_safe(method.result.get("dailyUsageBuckets"))
    return CodexCapabilityUsageSection(
        status="ok",
        discovered_at=discovered_at,
        summary=summary if isinstance(summary, dict) else None,
        daily_usage_buckets=daily if isinstance(daily, list) else [],
    )


def _merge_section(previous: Any, current: Any) -> Any:
    if current.status == "ok":
        return current
    if previous.status == "ok":
        payload = previous.model_dump()
        payload["status"] = "error"
        payload["error_code"] = current.error_code
        payload["error_message"] = current.error_message
        return previous.__class__.model_validate(payload)
    return current


def _top_status(snapshot: CodexCapabilitiesSnapshot) -> str:
    sections = [
        snapshot.account.status,
        snapshot.models.status,
        snapshot.rate_limits.status,
        snapshot.usage.status,
    ]
    if all(item == "ok" for item in sections):
        return "ok"
    if _snapshot_has_data(snapshot):
        return "partial"
    if all(item == "not_refreshed" for item in sections):
        return "not_refreshed"
    return "error"


def refresh_codex_capabilities(
    account_slug: str,
    *,
    settings: AppSettings | None = None,
    probe_runner: ProbeRunner = run_codex_app_server_probe,
) -> CodexCapabilitiesSnapshot:
    settings = settings or get_settings()
    try:
        slug = validate_account_slug(account_slug)
        profile_dir = resolve_profile(settings, slug)
        path = cache_path(settings, slug)
    except CodexAppServerError as exc:
        raise CodexCapabilitiesError(exc.code, exc.message) from exc
    with _account_lock(settings, slug):
        previous = _read_cached_unlocked(slug, profile_dir, path)
        try:
            probe = probe_runner(slug, settings=settings)
        except CodexAppServerError as exc:
            now = _iso(_utc_now())
            snapshot = previous.model_copy(deep=True)
            snapshot.refreshed_at = now
            snapshot.error_code = exc.code
            snapshot.error_message = _safe_message(exc.message)
            snapshot.status = _top_status(snapshot)
            if _snapshot_has_data(snapshot):
                _write_snapshot(path, snapshot)
            return snapshot
        now = _iso(_utc_now())
        if probe.profile_integrity_status == "changed":
            snapshot = previous.model_copy(deep=True)
            snapshot.profile_integrity_status = "changed"
            snapshot.error_code = "profile_integrity_changed"
            snapshot.error_message = "Codex profile auth metadata changed during probe"
            snapshot.status = _top_status(snapshot)
            return snapshot
        account = _merge_section(previous.account, _normalize_account(probe.methods.get("account", MethodResult("error", error_code="not_run")), now))
        models = _merge_section(previous.models, _normalize_models(probe.methods.get("models", MethodResult("error", error_code="not_run")), now))
        rate_limits = _merge_section(previous.rate_limits, _normalize_rate_limits(probe.methods.get("rate_limits", MethodResult("error", error_code="not_run")), now))
        usage = _merge_section(previous.usage, _normalize_usage(probe.methods.get("usage", MethodResult("error", error_code="not_run")), now))
        snapshot = CodexCapabilitiesSnapshot(
            account_profile=slug,
            profile_path=str(profile_dir),
            status="not_refreshed",
            refreshed_at=now,
            last_success_at=now if any(section.status == "ok" for section in (account, models, rate_limits, usage)) else previous.last_success_at,
            profile_integrity_status=probe.profile_integrity_status,
            account=account,
            models=models,
            rate_limits=rate_limits,
            usage=usage,
            error_code=probe.error_code,
            error_message=_safe_message(probe.error_message),
        )
        snapshot.status = _top_status(snapshot)
        if snapshot.status in {"ok", "partial"}:
            _write_snapshot(path, snapshot)
        return snapshot
