from __future__ import annotations

import json
import os
import stat
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.core.project_trust import ProjectTrustSyncResult
from ai_account_manager.schemas.claude_code import (
    ClaudeCodeAccountProfile,
    ClaudeCodeProfileDiscoveryResponse,
    DeepSeekBalanceItem,
    DeepSeekBalanceSnapshot,
)
from ai_account_manager.schemas.codex import (
    CodexCapabilitiesSnapshot,
    CodexCapabilityAccountSection,
    CodexCapabilityModelItem,
    CodexCapabilityModelsSection,
    CodexCapabilityRateLimitsSection,
    CodexCapabilityUsageSection,
)
from ai_account_manager.schemas.sessions import ProjectSettingsValidationResponse

ACCOUNT_SLUG = "deepseek_default"
ACCOUNT_LABEL = "DeepSeek API"


class DeepSeekProviderError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def reconcile_deepseek_project_trust(
    project_paths: tuple[Path, ...],
    *,
    settings: AppSettings,
    profile_path: Path | None = None,
) -> ProjectTrustSyncResult:
    """Claude is launched in the Telegram Agent's approved bypass mode.

    DeepSeek credentials are provider authentication, not a filesystem trust
    store, so there is intentionally no profile mutation here.
    """

    if profile_path is not None:
        raise DeepSeekProviderError(
            "project_trust_not_supported", "Profile override is unsupported"
        )
    read_api_key(settings)
    canonical = {path.resolve(strict=True) for path in project_paths}
    if any(not path.is_dir() for path in canonical):
        raise DeepSeekProviderError("project_missing", "Project directory does not exist")
    return ProjectTrustSyncResult(
        provider_id="claude_code",
        project_count=len(canonical),
        profile_count=1,
        changed_profiles=(),
    )


def _utc_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _credential_path(settings: AppSettings) -> Path:
    path = settings.deepseek_api_key_path.expanduser()
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise DeepSeekProviderError("invalid_key_file", "DeepSeek credential path is invalid")
    return path


def read_api_key(settings: AppSettings | None = None) -> str:
    settings = settings or get_settings()
    path = _credential_path(settings)
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise DeepSeekProviderError("missing_key", "DeepSeek API key is not configured") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DeepSeekProviderError("invalid_key_file", "DeepSeek API key path is unsafe")
    if metadata.st_uid != os.getuid() or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise DeepSeekProviderError("invalid_key_file", "DeepSeek API key file must be owner-only")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DeepSeekProviderError("invalid_key_file", "DeepSeek API key is unreadable") from exc
    if not token or len(token) > 4096 or any(character.isspace() for character in token):
        raise DeepSeekProviderError("invalid_key", "DeepSeek API key has invalid format")
    return token


def discover_claude_code_profiles(
    settings: AppSettings | None = None,
) -> ClaudeCodeProfileDiscoveryResponse:
    settings = settings or get_settings()
    path = _credential_path(settings)
    try:
        read_api_key(settings)
    except DeepSeekProviderError as exc:
        status = exc.code
        ready = False
    else:
        status = "readable"
        ready = True
    account = ClaudeCodeAccountProfile(
        account_slug=ACCOUNT_SLUG,
        label=ACCOUNT_LABEL,
        profile_path=str(path.parent),
        profile_path_exists=path.parent.is_dir(),
        has_api_key=path.is_file() and not path.is_symlink(),
        auth_ready=ready,
        status=status,
    )
    return ClaudeCodeProfileDiscoveryResponse(account_count=1, accounts=[account])


def deepseek_capabilities(
    account_slug: str, settings: AppSettings | None = None
) -> CodexCapabilitiesSnapshot:
    settings = settings or get_settings()
    if account_slug != ACCOUNT_SLUG:
        raise DeepSeekProviderError("profile_missing", "Unknown Claude Code profile")
    read_api_key(settings)
    now = _utc_iso()
    return CodexCapabilitiesSnapshot(
        provider_id="claude_code",
        account_profile=ACCOUNT_SLUG,
        profile_path=str(_credential_path(settings).parent),
        source="deepseek_official_catalog",
        status="ok",
        refreshed_at=now,
        last_success_at=now,
        profile_integrity_status="unchanged",
        account=CodexCapabilityAccountSection(
            status="ok",
            authorized=True,
            account_type="api_key",
            plan_type="prepaid",
            safe_identifier=ACCOUNT_SLUG,
            discovered_at=now,
        ),
        models=CodexCapabilityModelsSection(
            status="ok",
            discovered_at=now,
            items=[
                CodexCapabilityModelItem(
                    model_id="deepseek-flash",
                    display_name="DeepSeek V4.1 Flash · 1M",
                    is_default=True,
                    hidden=False,
                    default_reasoning_effort="max",
                    supported_reasoning_efforts=["max"],
                    input_modalities=["text", "image"],
                ),
                CodexCapabilityModelItem(
                    model_id="deepseek-v4-pro",
                    display_name="DeepSeek V4 Pro",
                    is_default=False,
                    hidden=False,
                    default_reasoning_effort="max",
                    supported_reasoning_efforts=["max"],
                    input_modalities=["text"],
                ),
            ],
        ),
        rate_limits=CodexCapabilityRateLimitsSection(
            status="not_refreshed",
            error_code="not_supported",
            error_message="DeepSeek does not expose account rate-limit windows via this API.",
        ),
        usage=CodexCapabilityUsageSection(
            status="not_refreshed",
            error_code="not_supported",
            error_message="DeepSeek usage history is not exposed by the balance endpoint.",
        ),
    )


def validate_deepseek_settings(
    *,
    account_profile: str,
    model: str,
    reasoning_effort: str,
    settings: AppSettings | None = None,
) -> ProjectSettingsValidationResponse:
    snapshot = deepseek_capabilities(account_profile, settings=settings)
    selected = next((item for item in snapshot.models.items if item.model_id == model), None)
    if selected is None:
        raise DeepSeekProviderError("model_not_available", "Requested model is not available")
    if reasoning_effort not in selected.supported_reasoning_efforts:
        raise DeepSeekProviderError(
            "reasoning_not_supported", "Requested reasoning effort is not supported"
        )
    return ProjectSettingsValidationResponse(
        status="valid",
        provider_id="claude_code",
        account_profile=account_profile,
        model=model,
        reasoning_effort=reasoning_effort,
        capability_source="current",
        capabilities_discovered_at=snapshot.models.discovered_at,
        model_display_name=selected.display_name,
        supported_reasoning_efforts=selected.supported_reasoning_efforts,
        warnings=[],
        errors=[],
    )


def _cache_payload(snapshot: DeepSeekBalanceSnapshot) -> dict[str, Any]:
    return snapshot.model_dump(mode="json")


def _write_cache(path: Path, snapshot: DeepSeekBalanceSnapshot) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(_cache_payload(snapshot), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def read_cached_balance(
    account_slug: str, settings: AppSettings | None = None
) -> DeepSeekBalanceSnapshot:
    settings = settings or get_settings()
    if account_slug != ACCOUNT_SLUG:
        raise DeepSeekProviderError("profile_missing", "Unknown Claude Code profile")
    path = settings.deepseek_balance_cache_path.expanduser()
    if path.is_symlink():
        raise DeepSeekProviderError("cache_error", "DeepSeek balance cache path is unsafe")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return DeepSeekBalanceSnapshot.model_validate(raw)
    except FileNotFoundError:
        return DeepSeekBalanceSnapshot(account_slug=ACCOUNT_SLUG, status="not_refreshed")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise DeepSeekProviderError("cache_error", "DeepSeek balance cache is invalid") from exc


def _balance_item(raw: object) -> DeepSeekBalanceItem:
    if not isinstance(raw, dict):
        raise DeepSeekProviderError("invalid_response", "DeepSeek balance item is invalid")
    values = {
        key: raw.get(key)
        for key in ("currency", "total_balance", "granted_balance", "topped_up_balance")
    }
    if not all(isinstance(value, str) and value for value in values.values()):
        raise DeepSeekProviderError("invalid_response", "DeepSeek balance fields are invalid")
    return DeepSeekBalanceItem(**values)  # type: ignore[arg-type]


def refresh_balance(
    account_slug: str, settings: AppSettings | None = None
) -> DeepSeekBalanceSnapshot:
    settings = settings or get_settings()
    if account_slug != ACCOUNT_SLUG:
        raise DeepSeekProviderError("profile_missing", "Unknown Claude Code profile")
    token = read_api_key(settings)
    endpoint = settings.deepseek_api_base_url.rstrip("/") + "/user/balance"
    request = urllib.request.Request(
        endpoint,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        code = "auth_failed" if exc.code == 401 else "request_failed"
        raise DeepSeekProviderError(code, f"DeepSeek balance request failed ({exc.code})") from exc
    except (OSError, TimeoutError, json.JSONDecodeError) as exc:
        raise DeepSeekProviderError("request_failed", "DeepSeek balance request failed") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("is_available"), bool):
        raise DeepSeekProviderError("invalid_response", "DeepSeek balance response is invalid")
    infos = payload.get("balance_infos")
    if not isinstance(infos, list):
        raise DeepSeekProviderError("invalid_response", "DeepSeek balance response is invalid")
    snapshot = DeepSeekBalanceSnapshot(
        account_slug=ACCOUNT_SLUG,
        status="ok",
        fetched_at=_utc_iso(),
        source_endpoint=endpoint,
        is_available=payload["is_available"],
        balance_infos=[_balance_item(item) for item in infos],
    )
    _write_cache(settings.deepseek_balance_cache_path, snapshot)
    return snapshot
