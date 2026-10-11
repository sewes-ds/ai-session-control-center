from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from pydantic import ValidationError

from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.providers.codex.app_server import (
    CodexAppServerError,
    call_codex_app_server_account_method,
    resolve_profile,
    validate_account_slug,
)
from ai_account_manager.schemas.codex import (
    CodexQuotaBucket,
    CodexQuotaResetCreditItem,
    CodexQuotaResetCredits,
    CodexQuotaSnapshot,
    CodexQuotaWindow,
    CodexRateLimitResetCreditConsumeResponse,
)

_SAFE_CREDIT_ID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,512}$")


class CodexQuotaError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _snapshot_path(settings: AppSettings, account_slug: str) -> Path:
    return settings.codex_quota_cache_root / f"{validate_account_slug(account_slug)}.json"


def _lock_path(settings: AppSettings, account_slug: str) -> Path:
    return settings.codex_quota_cache_root / f"{validate_account_slug(account_slug)}.lock"


@contextmanager
def _quota_lock(settings: AppSettings, account_slug: str) -> Iterator[None]:
    root = settings.codex_quota_cache_root
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    path = _lock_path(settings, account_slug)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _write_snapshot(settings: AppSettings, snapshot: CodexQuotaSnapshot) -> None:
    path = _snapshot_path(settings, snapshot.account_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


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


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _bounded_text(value: Any, *, max_length: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return text[:max_length] or None


def _window_sort_key(window: CodexQuotaWindow) -> tuple[int, int]:
    order = {"primary": 0, "secondary": 1}
    return (order.get(window.window_type or window.window_id, 9), window.window_duration_minutes or 0)


def _parse_reset(value: Any) -> str | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        return value.strip()[:80]
    return None


def _normalize_window(window_type: str, payload: Any) -> CodexQuotaWindow | None:
    if not isinstance(payload, dict):
        return None
    used = _float_or_none(payload.get("usedPercent"))
    used = _clamp(used) if used is not None else None
    remaining = None if used is None else _clamp(100.0 - used)
    duration = _int_or_none(payload.get("windowDurationMins"))
    resets_at = _parse_reset(payload.get("resetsAt"))
    if used is None and remaining is None and duration is None and resets_at is None:
        return None
    return CodexQuotaWindow(
        window_id=window_type,
        window_type=window_type,
        used_percent=used,
        remaining_percent=remaining,
        window_duration_minutes=duration,
        resets_at=resets_at,
        reset_at=str(resets_at) if resets_at is not None else None,
        limit_window_seconds=duration * 60 if duration is not None else None,
    )


def _normalize_bucket(payload: Any, fallback_limit_id: str | None = None) -> CodexQuotaBucket | None:
    if not isinstance(payload, dict):
        return None
    limit_id = _bounded_text(payload.get("limitId"), max_length=120) or fallback_limit_id
    bucket = CodexQuotaBucket(
        limit_id=limit_id,
        limit_name=_bounded_text(payload.get("limitName"), max_length=160),
        plan_type=_bounded_text(payload.get("planType"), max_length=80),
        rate_limit_reached_type=_bounded_text(payload.get("rateLimitReachedType"), max_length=80),
    )
    for window_type in ("primary", "secondary"):
        window = _normalize_window(window_type, payload.get(window_type))
        if window is not None:
            bucket.windows.append(window)
    bucket.windows.sort(key=_window_sort_key)
    if bucket.limit_id is None and bucket.limit_name is None and not bucket.windows:
        return None
    return bucket


def _bucket_sort_key(bucket: CodexQuotaBucket) -> tuple[int, str, str]:
    limit_id = bucket.limit_id or ""
    return (0 if limit_id == "codex" else 1, bucket.limit_name or "", limit_id)


def _normalize_reset_credits(payload: Any) -> CodexQuotaResetCredits:
    if not isinstance(payload, dict):
        return CodexQuotaResetCredits()
    count = payload.get("availableCount")
    available = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None
    raw_credits = payload.get("credits")
    if raw_credits is None:
        credits = None
    elif isinstance(raw_credits, list):
        credits = []
        for item in raw_credits[:20]:
            if not isinstance(item, dict):
                continue
            credit_id = item.get("id")
            credits.append(
                CodexQuotaResetCreditItem(
                    credit_id=credit_id if isinstance(credit_id, str) and _SAFE_CREDIT_ID_RE.fullmatch(credit_id) else None,
                    title=_bounded_text(item.get("title"), max_length=120),
                    expires_at=_parse_reset(item.get("expiresAt")),
                )
            )
    else:
        credits = None
    return CodexQuotaResetCredits(available_count=available, credits=credits)


def normalize_codex_rate_limits_response(
    *, account_slug: str, rate_limits_payload: dict[str, Any], fetched_at: datetime | None = None
) -> CodexQuotaSnapshot:
    fetched_at = fetched_at or _utc_now()
    buckets: list[CodexQuotaBucket] = []
    seen_ids: set[str] = set()
    by_id = rate_limits_payload.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        for key, value in by_id.items():
            fallback = str(key) if isinstance(key, str) else None
            bucket = _normalize_bucket(value, fallback)
            if bucket is None:
                continue
            bucket_id = bucket.limit_id or fallback
            if bucket_id:
                seen_ids.add(bucket_id)
            buckets.append(bucket)
    fallback = _normalize_bucket(rate_limits_payload.get("rateLimits"))
    if fallback is not None and (not buckets or not fallback.limit_id or fallback.limit_id not in seen_ids):
        buckets.append(fallback)
    buckets.sort(key=_bucket_sort_key)

    compat_windows: list[CodexQuotaWindow] = []
    for bucket in buckets:
        for window in bucket.windows:
            if window.window_duration_minutes == 300 and not any(item.window_id == "primary" for item in compat_windows):
                compat_windows.append(window.model_copy(update={"window_id": "primary"}))
            if window.window_duration_minutes == 10080 and not any(item.window_id == "secondary" for item in compat_windows):
                compat_windows.append(window.model_copy(update={"window_id": "secondary"}))

    return CodexQuotaSnapshot(
        account_slug=account_slug,
        status="ok",
        fetched_at=_iso(fetched_at),
        source_method="account/rateLimits/read",
        buckets=buckets,
        reset_credits=_normalize_reset_credits(rate_limits_payload.get("rateLimitResetCredits")),
        windows=compat_windows,
    )


def parse_codex_usage_response(
    *, account_slug: str, endpoint: str, usage_payload: dict[str, Any], fetched_at: datetime | None = None
) -> CodexQuotaSnapshot:
    fetched_at = fetched_at or _utc_now()
    rate_limit = usage_payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        raise CodexQuotaError("response_invalid", "Codex usage response does not contain rate_limit")
    payload = {
        "rateLimits": {
            "limitId": "codex",
            "primary": _legacy_usage_window(rate_limit.get("primary_window"), fetched_at),
            "secondary": _legacy_usage_window(rate_limit.get("secondary_window"), fetched_at),
        }
    }
    snapshot = normalize_codex_rate_limits_response(
        account_slug=account_slug,
        rate_limits_payload=payload,
        fetched_at=fetched_at,
    )
    snapshot.source_endpoint = endpoint
    return snapshot


def _legacy_usage_window(payload: Any, fetched_at: datetime) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    result: dict[str, Any] = {}
    if "used_percent" in payload:
        result["usedPercent"] = payload.get("used_percent")
    seconds = _int_or_none(payload.get("limit_window_seconds"))
    if seconds is not None:
        result["windowDurationMins"] = seconds // 60
    reset_at = payload.get("reset_at")
    if reset_at is not None:
        result["resetsAt"] = reset_at
    else:
        reset_after = _int_or_none(payload.get("reset_after_seconds"))
        if reset_after is not None:
            result["resetsAt"] = _iso(fetched_at + timedelta(seconds=reset_after))
    return result or None


def get_cached_codex_quota(
    account_slug: str, settings: AppSettings | None = None
) -> CodexQuotaSnapshot:
    settings = settings or get_settings()
    slug = validate_account_slug(account_slug)
    path = _snapshot_path(settings, slug)
    if not path.is_file():
        return CodexQuotaSnapshot(account_slug=slug, status="not_refreshed")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return CodexQuotaSnapshot.model_validate(data)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise CodexQuotaError("cache_invalid", "Cached Codex quota snapshot is invalid") from exc


RateLimitsFetcher = Callable[[str], dict[str, Any]]


def refresh_codex_quota(
    account_slug: str,
    *,
    settings: AppSettings | None = None,
    usage_fetcher: Callable[[str, str | None, str], dict[str, Any]] | None = None,
    rate_limits_fetcher: RateLimitsFetcher | None = None,
) -> CodexQuotaSnapshot:
    settings = settings or get_settings()
    try:
        slug = validate_account_slug(account_slug)
        resolve_profile(settings, slug)
    except CodexAppServerError as exc:
        raise CodexQuotaError(exc.code, exc.message) from exc
    with _quota_lock(settings, slug):
        try:
            if rate_limits_fetcher is not None:
                raw = rate_limits_fetcher(slug)
            elif usage_fetcher is not None:
                raw = usage_fetcher("", None, settings.codex_usage_endpoint)
                snapshot = parse_codex_usage_response(
                    account_slug=slug,
                    endpoint=settings.codex_usage_endpoint,
                    usage_payload=raw,
                )
                _write_snapshot(settings, snapshot)
                return snapshot
            else:
                raw = call_codex_app_server_account_method(
                    slug,
                    method="account/rateLimits/read",
                    include_params=False,
                    settings=settings,
                )
        except CodexAppServerError as exc:
            raise CodexQuotaError(exc.code, exc.message) from exc
        if not isinstance(raw, dict):
            raise CodexQuotaError("response_invalid", "Codex rate-limit response is invalid")
        snapshot = normalize_codex_rate_limits_response(account_slug=slug, rate_limits_payload=raw)
        _write_snapshot(settings, snapshot)
        return snapshot


def consume_rate_limit_reset_credit(
    account_slug: str,
    *,
    idempotency_key: str,
    credit_id: str | None = None,
    settings: AppSettings | None = None,
    consume_caller: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
    refresh_after: Callable[[], CodexQuotaSnapshot] | None = None,
) -> CodexRateLimitResetCreditConsumeResponse:
    settings = settings or get_settings()
    try:
        slug = validate_account_slug(account_slug)
        uuid.UUID(idempotency_key)
        resolve_profile(settings, slug)
    except ValueError as exc:
        raise CodexQuotaError("invalid_idempotency_key", "Reset idempotency key must be a UUID") from exc
    except CodexAppServerError as exc:
        raise CodexQuotaError(exc.code, exc.message) from exc
    params: dict[str, str] = {"idempotencyKey": idempotency_key}
    if credit_id is not None:
        if not _SAFE_CREDIT_ID_RE.fullmatch(credit_id):
            raise CodexQuotaError("invalid_credit_id", "Reset credit id is invalid")
        params["creditId"] = credit_id
    try:
        raw = (
            consume_caller(slug, params)
            if consume_caller is not None
            else call_codex_app_server_account_method(
                slug,
                method="account/rateLimitResetCredit/consume",
                params=params,
                include_params=True,
                settings=settings,
            )
        )
    except CodexAppServerError as exc:
        raise CodexQuotaError(exc.code, exc.message) from exc
    if not isinstance(raw, dict):
        raise CodexQuotaError("response_invalid", "Codex reset-credit response is invalid")
    outcome = raw.get("outcome") or raw.get("status")
    if outcome not in {"reset", "alreadyRedeemed", "nothingToReset", "noCredit"}:
        raise CodexQuotaError("unexpected_outcome", "Codex reset-credit outcome is unexpected")
    quota = None
    if outcome in {"reset", "alreadyRedeemed"}:
        quota = refresh_after() if refresh_after is not None else refresh_codex_quota(slug, settings=settings)
    return CodexRateLimitResetCreditConsumeResponse(
        account_slug=slug,
        outcome=outcome,
        success=outcome in {"reset", "alreadyRedeemed"},
        quota=quota,
    )
