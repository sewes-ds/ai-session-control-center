from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.schemas.codex import CodexAuthStatus

_SAFE_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}$")


class CodexAuthStatusError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_slug(account_slug: str) -> None:
    if not _SAFE_SLUG_RE.fullmatch(account_slug):
        raise CodexAuthStatusError("invalid_slug", "Invalid Codex account slug")


def _profile_dir(settings: AppSettings, account_slug: str) -> Path:
    _validate_slug(account_slug)
    return settings.codex_profiles_root / account_slug


def _parse_datetime(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _auth_json_mtime(auth_json_path: Path) -> str | None:
    try:
        return _iso(datetime.fromtimestamp(auth_json_path.stat().st_mtime, tz=UTC))
    except OSError:
        return None


def _safe_auth_mode(raw: Any) -> str | None:
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:80]
    return None


def get_codex_auth_status(
    account_slug: str,
    *,
    settings: AppSettings | None = None,
    now: datetime | None = None,
) -> CodexAuthStatus:
    """Return safe, non-secret auth status for a Codex profile.

    This function may read auth.json structure, but it must never return token values,
    id tokens, cookies, bearer strings, or raw credential material.
    """
    settings = settings or get_settings()
    now = now or _utc_now()
    profile_dir = _profile_dir(settings, account_slug)
    auth_json_path = profile_dir / "auth.json"

    if not profile_dir.is_dir():
        return CodexAuthStatus(
            account_slug=account_slug,
            profile_path=str(profile_dir),
            profile_path_exists=False,
            has_auth_json=False,
            status="profile_missing",
            recommendation="create_or_restore_profile",
        )

    if not auth_json_path.is_file():
        return CodexAuthStatus(
            account_slug=account_slug,
            profile_path=str(profile_dir),
            profile_path_exists=True,
            has_auth_json=False,
            status="auth_missing",
            recommendation="create_or_restore_auth_json",
        )

    try:
        payload = json.loads(auth_json_path.read_text(encoding="utf-8"))
    except Exception:
        return CodexAuthStatus(
            account_slug=account_slug,
            profile_path=str(profile_dir),
            profile_path_exists=True,
            has_auth_json=True,
            auth_json_mtime=_auth_json_mtime(auth_json_path),
            status="auth_invalid",
            recommendation="recreate_auth_json_with_codex_login",
        )

    if not isinstance(payload, dict):
        return CodexAuthStatus(
            account_slug=account_slug,
            profile_path=str(profile_dir),
            profile_path_exists=True,
            has_auth_json=True,
            auth_json_mtime=_auth_json_mtime(auth_json_path),
            status="auth_invalid",
            recommendation="recreate_auth_json_with_codex_login",
        )

    tokens = payload.get("tokens")
    has_tokens_object = isinstance(tokens, dict)
    token_payload = tokens if isinstance(tokens, dict) else {}

    has_access_token = bool(
        isinstance(token_payload.get("access_token"), str)
        and token_payload.get("access_token", "").strip()
    ) or bool(isinstance(payload.get("access_token"), str) and payload.get("access_token", "").strip())

    has_refresh_token = bool(
        isinstance(token_payload.get("refresh_token"), str)
        and token_payload.get("refresh_token", "").strip()
    ) or bool(isinstance(payload.get("refresh_token"), str) and payload.get("refresh_token", "").strip())

    has_account_id = bool(
        isinstance(token_payload.get("account_id"), str) and token_payload.get("account_id", "").strip()
    ) or bool(isinstance(payload.get("account_id"), str) and payload.get("account_id", "").strip())

    raw_last_refresh = payload.get("last_refresh") or token_payload.get("last_refresh")
    parsed_last_refresh = _parse_datetime(raw_last_refresh)
    last_refresh_age_seconds: int | None = None
    is_stale_by_last_refresh = False
    if parsed_last_refresh is not None:
        last_refresh_age_seconds = max(0, int((now - parsed_last_refresh).total_seconds()))
        is_stale_by_last_refresh = last_refresh_age_seconds >= settings.codex_auth_stale_after_seconds

    status = "ready"
    recommendation = "none"
    if not has_tokens_object:
        status = "tokens_missing"
        recommendation = "recreate_auth_json_with_codex_login"
    elif not has_access_token:
        status = "access_missing"
        recommendation = "run_codex_to_refresh_or_relogin"
    elif is_stale_by_last_refresh:
        status = "stale_by_last_refresh"
        recommendation = "let_codex_refresh_auth_on_next_trusted_run"

    return CodexAuthStatus(
        account_slug=account_slug,
        profile_path=str(profile_dir),
        profile_path_exists=True,
        has_auth_json=True,
        auth_json_mtime=_auth_json_mtime(auth_json_path),
        auth_mode=_safe_auth_mode(payload.get("auth_mode")),
        has_tokens_object=has_tokens_object,
        has_access_token=has_access_token,
        has_refresh_token=has_refresh_token,
        has_account_id=has_account_id,
        last_refresh=str(raw_last_refresh) if isinstance(raw_last_refresh, str) else None,
        last_refresh_age_seconds=last_refresh_age_seconds,
        stale_after_seconds=settings.codex_auth_stale_after_seconds,
        is_stale_by_last_refresh=is_stale_by_last_refresh,
        status=status,
        recommendation=recommendation,
    )
