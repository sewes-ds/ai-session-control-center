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
    if provider_id == "claude_code":
        from ai_account_manager.providers.claude_code.deepseek import (
            reconcile_deepseek_project_trust,
        )

        return reconcile_deepseek_project_trust
    raise ProjectTrustError(
        "project_trust_not_supported",
        f"Project trust is not implemented for provider {provider_id}",
    )


def configured_project_paths(
    provider_id: str,
    *,
    settings: AppSettings | None = None,
) -> tuple[Path, ...]:
    """Return project identities from the authoritative Runtime control store."""

    settings = settings or get_settings()
    from ai_account_manager.core.runtime_control import read_runtime_control_store

    store = read_runtime_control_store(settings.runtime_control_path)
    paths: set[Path] = set()
    for binding in store.bindings.values():
        if binding.lifecycle_status != "CONFIGURED" or binding.desired.provider != provider_id:
            continue
        path = Path(binding.canonical_cwd).expanduser().resolve(strict=False)
        if not path.is_dir():
            raise ProjectTrustError(
                "project_trust_source_invalid", "Configured project identity is unavailable"
            )
        paths.add(path)
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
