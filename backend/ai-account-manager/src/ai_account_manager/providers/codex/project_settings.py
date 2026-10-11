from __future__ import annotations

from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.core.project_settings import ProjectSettingsError
from ai_account_manager.providers.codex.capabilities import (
    CodexCapabilitiesError,
    read_cached_codex_capabilities,
)
from ai_account_manager.schemas.sessions import ProjectSettingsValidationResponse

def _profile_dir(settings: AppSettings, account_profile: str) -> None:
    root = settings.codex_profiles_root.expanduser()
    profile = root / account_profile
    if not profile.is_dir():
        raise ProjectSettingsError("profile_missing", "Codex profile directory does not exist")
    auth = profile / "auth.json"
    if auth.is_symlink() or not auth.is_file():
        raise ProjectSettingsError("profile_missing", "Codex profile auth.json does not exist")


def validate_codex_settings(
    *,
    account_profile: str,
    model: str,
    reasoning_effort: str,
    settings: AppSettings | None = None,
) -> ProjectSettingsValidationResponse:
    settings = settings or get_settings()
    _profile_dir(settings, account_profile)
    try:
        snapshot = read_cached_codex_capabilities(account_profile, settings=settings)
    except CodexCapabilitiesError as exc:
        raise ProjectSettingsError(exc.code, exc.message) from exc

    items = snapshot.models.items
    # Model availability is independent from quota/usage probes. A successful
    # model-list response remains current even when an unrelated rate-limit
    # request makes the aggregate capability snapshot partial.
    if snapshot.models.status == "ok" and items:
        capability_source = "current"
        warnings: list[str] = []
    elif snapshot.models.status in {"error", "not_refreshed"} and items:
        capability_source = "last_known_good"
        warnings = ["last_known_good"]
    else:
        raise ProjectSettingsError(
            "capabilities_refresh_required",
            "Codex capabilities cache is missing or empty for this account",
        )

    selected = next((item for item in items if item.model_id == model), None)
    if selected is None:
        raise ProjectSettingsError("model_not_available", "Requested model is not available")

    supported = selected.supported_reasoning_efforts
    if supported:
        if reasoning_effort not in supported:
            raise ProjectSettingsError(
                "reasoning_not_supported", "Requested reasoning effort is not supported"
            )
    elif reasoning_effort != selected.default_reasoning_effort or not reasoning_effort:
        raise ProjectSettingsError(
            "reasoning_not_supported", "Requested reasoning effort is not supported"
        )

    return ProjectSettingsValidationResponse(
        status="valid",
        provider_id="codex",
        account_profile=account_profile,
        model=model,
        reasoning_effort=reasoning_effort,
        capability_source=capability_source,
        capabilities_discovered_at=snapshot.models.discovered_at or snapshot.last_success_at,
        model_display_name=selected.display_name,
        supported_reasoning_efforts=supported,
        warnings=warnings,
        errors=[],
    )
