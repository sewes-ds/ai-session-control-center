from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ai_account_manager.core.config import AppSettings, get_settings


class ProjectTrustError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ProjectTrustSyncResult:
    provider_id: str
    project_count: int
    profile_count: int
    changed_profiles: tuple[str, ...]


ProjectTrustAdapter = Callable[..., ProjectTrustSyncResult]


def _adapter(provider_id: str) -> ProjectTrustAdapter:
    if provider_id == "codex":
        from ai_account_manager.providers.codex.project_trust import (
            reconcile_codex_project_trust,
        )

        return reconcile_codex_project_trust
    raise ProjectTrustError(
        "project_trust_not_supported",
        f"Project trust is not implemented for provider {provider_id}",
    )


def configured_project_paths(
    provider_id: str,
    *,
    settings: AppSettings | None = None,
) -> tuple[Path, ...]:
    """Return the provider's centrally configured project identities.

    The project settings store is the source of truth. Provider profiles are
    projections only and never define whether a CUS project is trusted.
    """

    settings = settings or get_settings()
    from ai_account_manager.core.project_settings import (
        ProjectSettingsError,
        canonical_project_path,
        read_project_settings_store,
    )

    store, _migration_required = read_project_settings_store(settings)
    projects = store.get("projects", {})
    paths: set[Path] = set()
    for raw in projects.values():
        if not isinstance(raw, dict) or raw.get("provider_id") != provider_id:
            continue
        value = raw.get("project_path")
        if not isinstance(value, str):
            raise ProjectTrustError(
                "project_trust_source_invalid", "Configured project path is invalid"
            )
        try:
            paths.add(canonical_project_path(value, settings))
        except ProjectSettingsError as exc:
            raise ProjectTrustError(
                "project_trust_source_invalid", "Configured project identity is unavailable"
            ) from exc
    return tuple(sorted(paths, key=str))


def reconcile_project_trust(
    *,
    provider_id: str,
    project_paths: tuple[Path, ...] | list[Path],
    settings: AppSettings | None = None,
    profile_path: Path | None = None,
) -> ProjectTrustSyncResult:
    settings = settings or get_settings()
    adapter = _adapter(provider_id)
    return adapter(
        tuple(project_paths),
        settings=settings,
        profile_path=profile_path,
    )


def reconcile_configured_project_trust(
    *,
    provider_id: str,
    settings: AppSettings | None = None,
    profile_path: Path | None = None,
) -> ProjectTrustSyncResult:
    settings = settings or get_settings()
    return reconcile_project_trust(
        provider_id=provider_id,
        project_paths=configured_project_paths(provider_id, settings=settings),
        settings=settings,
        profile_path=profile_path,
    )
