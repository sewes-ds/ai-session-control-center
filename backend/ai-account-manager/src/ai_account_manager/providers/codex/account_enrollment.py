from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.schemas.codex import CodexAccountEnrollment

_SAFE_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}$")
_SAFE_ENROLLMENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_CODE_RE = re.compile(r"\b[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+\b")
_URL_RE = re.compile(r"https://[^\s<>\"']+")
_ALLOWED_DEVICE_HOSTS = {"auth.openai.com", "chatgpt.com"}
_ACTIVE_STATUSES = {"starting", "waiting_for_user"}
_FINAL_STATUSES = {"completed", "failed", "cancelled", "expired"}


class CodexAccountEnrollmentError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    value = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _validate_slug(account_slug: str) -> str:
    value = account_slug.strip()
    if not _SAFE_SLUG_RE.fullmatch(value):
        raise CodexAccountEnrollmentError("invalid_slug", "Invalid Codex account slug")
    return value


def _validate_enrollment_id(enrollment_id: str) -> str:
    value = enrollment_id.strip().lower()
    if not _SAFE_ENROLLMENT_ID_RE.fullmatch(value):
        raise CodexAccountEnrollmentError("invalid_enrollment_id", "Invalid enrollment id")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _enrollment_dir(settings: AppSettings, enrollment_id: str) -> Path:
    return settings.codex_auth_enrollment_root / enrollment_id


def _metadata_path(settings: AppSettings, enrollment_id: str) -> Path:
    return _enrollment_dir(settings, enrollment_id) / "enrollment.json"


def _log_path(settings: AppSettings, enrollment_id: str) -> Path:
    return _enrollment_dir(settings, enrollment_id) / "device_auth.log"


def _staging_profile_path(settings: AppSettings, enrollment_id: str) -> Path:
    return _enrollment_dir(settings, enrollment_id) / "profile"


def _resolve_codex_binary(settings: AppSettings) -> str:
    configured = str(settings.codex_binary).strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        if path.name == configured and (found := shutil.which(configured)):
            return found
    if found := shutil.which("codex"):
        return found
    fallback = Path.home() / ".npm-global" / "bin" / "codex"
    if fallback.is_file() and os.access(fallback, os.X_OK):
        return str(fallback)
    raise CodexAccountEnrollmentError("codex_binary_missing", "Codex CLI binary not found")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _safe_device_fields(log_path: Path) -> tuple[str | None, str | None]:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")[-12000:]
    except OSError:
        return None, None

    verification_url: str | None = None
    for raw_url in _URL_RE.findall(text):
        candidate = raw_url.rstrip(".,);]")
        parsed = urlparse(candidate)
        if parsed.scheme == "https" and parsed.hostname in _ALLOWED_DEVICE_HOSTS:
            verification_url = candidate
            break

    match = _CODE_RE.search(text.upper())
    user_code = match.group(0) if match else None
    return verification_url, user_code


def _terminate_process_group(pid: int) -> None:
    if not _pid_alive(pid):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise CodexAccountEnrollmentError(
            "process_stop_forbidden", "Cannot stop Codex authorization process"
        ) from exc

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _to_model(payload: dict[str, Any]) -> CodexAccountEnrollment:
    return CodexAccountEnrollment(
        enrollment_id=str(payload.get("enrollment_id") or ""),
        provider_id="codex",
        account_slug=str(payload.get("account_slug") or ""),
        label=str(payload.get("label") or payload.get("account_slug") or ""),
        status=str(payload.get("status") or "failed"),
        created_at=str(payload.get("created_at") or ""),
        updated_at=str(payload.get("updated_at") or ""),
        expires_at=str(payload.get("expires_at") or ""),
        verification_url=(
            str(payload["verification_url"]) if payload.get("verification_url") else None
        ),
        user_code=str(payload["user_code"]) if payload.get("user_code") else None,
        profile_path=str(payload["profile_path"]) if payload.get("profile_path") else None,
        error_code=str(payload["error_code"]) if payload.get("error_code") else None,
        error_message=str(payload["error_message"]) if payload.get("error_message") else None,
    )


def _find_active_for_slug(
    settings: AppSettings, account_slug: str
) -> CodexAccountEnrollment | None:
    root = settings.codex_auth_enrollment_root
    if not root.is_dir():
        return None
    for child in root.iterdir():
        if not child.is_dir() or not _SAFE_ENROLLMENT_ID_RE.fullmatch(child.name):
            continue
        payload = _read_json(child / "enrollment.json")
        if (
            payload.get("account_slug") == account_slug
            and payload.get("status") in _ACTIVE_STATUSES
        ):
            return _to_model(payload)
    return None


def start_codex_account_enrollment(
    account_slug: str,
    *,
    label: str | None = None,
    settings: AppSettings | None = None,
    popen_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
    now: datetime | None = None,
) -> CodexAccountEnrollment:
    settings = settings or get_settings()
    now = now or _utc_now()
    slug = _validate_slug(account_slug)
    display_label = (label or slug).strip()[:120] or slug
    final_profile = settings.codex_profiles_root / slug

    if final_profile.exists() or final_profile.is_symlink():
        raise CodexAccountEnrollmentError("account_exists", "Codex account profile already exists")
    active = _find_active_for_slug(settings, slug)
    if active is not None:
        raise CodexAccountEnrollmentError(
            "enrollment_in_progress", "Authorization is already in progress for this account"
        )

    binary = _resolve_codex_binary(settings)
    enrollment_id = uuid.uuid4().hex
    enrollment_dir = _enrollment_dir(settings, enrollment_id)
    staging_profile = _staging_profile_path(settings, enrollment_id)
    enrollment_dir.mkdir(parents=True, mode=0o700)
    os.chmod(enrollment_dir, 0o700)
    staging_profile.mkdir(mode=0o700)
    log_path = _log_path(settings, enrollment_id)
    log_handle = log_path.open("ab", buffering=0)
    os.chmod(log_path, 0o600)

    env = os.environ.copy()
    env["CODEX_HOME"] = str(staging_profile)
    try:
        process = popen_factory(
            [binary, "login", "--device-auth"],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:
        log_handle.close()
        shutil.rmtree(enrollment_dir, ignore_errors=True)
        raise CodexAccountEnrollmentError(
            "auth_start_failed", "Failed to start Codex device authorization"
        ) from exc
    finally:
        if not log_handle.closed:
            log_handle.close()

    expires_at = now + timedelta(seconds=settings.codex_auth_enrollment_ttl_seconds)
    payload: dict[str, Any] = {
        "enrollment_id": enrollment_id,
        "account_slug": slug,
        "label": display_label,
        "status": "starting",
        "created_at": _iso(now),
        "updated_at": _iso(now),
        "expires_at": _iso(expires_at),
        "pid": int(process.pid),
        "profile_path": None,
        "verification_url": None,
        "user_code": None,
        "error_code": None,
        "error_message": None,
    }
    _atomic_write_json(_metadata_path(settings, enrollment_id), payload)
    threading.Thread(target=process.wait, daemon=True).start()
    return _to_model(payload)


def _finalize_success(
    payload: dict[str, Any], *, settings: AppSettings, now: datetime
) -> dict[str, Any]:
    enrollment_id = str(payload["enrollment_id"])
    slug = str(payload["account_slug"])
    staging_profile = _staging_profile_path(settings, enrollment_id)
    final_profile = settings.codex_profiles_root / slug

    if final_profile.exists() or final_profile.is_symlink():
        payload.update(
            status="failed",
            updated_at=_iso(now),
            error_code="account_exists",
            error_message="Codex account profile already exists",
        )
        return payload

    settings.codex_profiles_root.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.codex_profiles_root, 0o700)
    profile_json = staging_profile / "profile.json"
    profile_json.write_text(
        json.dumps({"label": payload.get("label") or slug}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(profile_json, 0o600)
    # A Codex account is an authentication/profile projection. Project trust
    # is sourced centrally from CUS and must be present before the profile is
    # atomically published.
    from ai_account_manager.core.project_trust import (
        ProjectTrustError,
        reconcile_configured_project_trust,
    )
    from ai_account_manager.providers.codex.openviking_defaults import (
        OpenVikingDefaultsError,
        provision_approved_openviking,
    )

    try:
        reconcile_configured_project_trust(
            provider_id="codex",
            settings=settings,
            profile_path=staging_profile,
        )
    except ProjectTrustError as exc:
        payload.update(
            status="failed",
            updated_at=_iso(now),
            error_code=exc.code,
            error_message="Failed to synchronize shared project trust",
        )
        return payload
    try:
        provision_approved_openviking(staging_profile, settings=settings)
    except OpenVikingDefaultsError as exc:
        payload.update(
            status="failed",
            updated_at=_iso(now),
            error_code=exc.code,
            error_message="Failed to install the approved OpenViking profile defaults",
        )
        return payload
    os.replace(staging_profile, final_profile)
    payload.update(
        status="completed",
        updated_at=_iso(now),
        profile_path=str(final_profile),
        error_code=None,
        error_message=None,
    )
    return payload


def get_codex_account_enrollment(
    enrollment_id: str,
    *,
    settings: AppSettings | None = None,
    now: datetime | None = None,
    pid_alive: Callable[[int], bool] = _pid_alive,
) -> CodexAccountEnrollment:
    settings = settings or get_settings()
    now = now or _utc_now()
    enrollment_id = _validate_enrollment_id(enrollment_id)
    metadata_path = _metadata_path(settings, enrollment_id)
    payload = _read_json(metadata_path)
    if not payload:
        raise CodexAccountEnrollmentError("enrollment_missing", "Enrollment not found")

    status = str(payload.get("status") or "failed")
    if status in _FINAL_STATUSES:
        return _to_model(payload)

    expires_at = _parse_iso(payload.get("expires_at"))
    pid = payload.get("pid")
    process_alive = isinstance(pid, int) and pid_alive(pid)
    verification_url, user_code = _safe_device_fields(_log_path(settings, enrollment_id))
    if verification_url:
        payload["verification_url"] = verification_url
    if user_code:
        payload["user_code"] = user_code
    if verification_url or user_code:
        payload["status"] = "waiting_for_user"

    staging_profile = _staging_profile_path(settings, enrollment_id)
    auth_json = staging_profile / "auth.json"
    if auth_json.is_file() and not process_alive:
        payload = _finalize_success(payload, settings=settings, now=now)
    elif expires_at is not None and now >= expires_at:
        if process_alive and isinstance(pid, int):
            _terminate_process_group(pid)
        shutil.rmtree(staging_profile, ignore_errors=True)
        payload.update(
            status="expired",
            updated_at=_iso(now),
            error_code="authorization_expired",
            error_message="Codex device authorization expired",
        )
    elif not process_alive:
        shutil.rmtree(staging_profile, ignore_errors=True)
        payload.update(
            status="failed",
            updated_at=_iso(now),
            error_code="authorization_failed",
            error_message="Codex device authorization did not complete",
        )
    else:
        payload["updated_at"] = _iso(now)

    _atomic_write_json(metadata_path, payload)
    return _to_model(payload)


def cancel_codex_account_enrollment(
    enrollment_id: str,
    *,
    settings: AppSettings | None = None,
    now: datetime | None = None,
) -> CodexAccountEnrollment:
    settings = settings or get_settings()
    now = now or _utc_now()
    enrollment_id = _validate_enrollment_id(enrollment_id)
    metadata_path = _metadata_path(settings, enrollment_id)
    payload = _read_json(metadata_path)
    if not payload:
        raise CodexAccountEnrollmentError("enrollment_missing", "Enrollment not found")

    status = str(payload.get("status") or "failed")
    if status == "completed":
        raise CodexAccountEnrollmentError(
            "enrollment_completed", "Completed enrollment cannot be cancelled"
        )
    if status in {"cancelled", "failed", "expired"}:
        return _to_model(payload)

    pid = payload.get("pid")
    if isinstance(pid, int):
        _terminate_process_group(pid)
    shutil.rmtree(_staging_profile_path(settings, enrollment_id), ignore_errors=True)
    payload.update(
        status="cancelled",
        updated_at=_iso(now),
        verification_url=None,
        user_code=None,
        error_code=None,
        error_message=None,
    )
    _atomic_write_json(metadata_path, payload)
    return _to_model(payload)
