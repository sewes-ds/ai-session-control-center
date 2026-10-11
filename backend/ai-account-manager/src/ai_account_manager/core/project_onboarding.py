from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

from ai_account_manager.core.config import AppSettings, get_settings

OnboardingState = Literal["LEGACY", "UNCONFIGURED", "CONFIGURED"]
REQUIRED_LAUNCH_FIELDS = (
    "project_path",
    "account_profile",
    "model",
    "reasoning_effort",
    "runtime_launch_spec",
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ProjectOnboardingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_onboarding_project_path(
    project_path: str,
    settings: AppSettings | None = None,
) -> str:
    """Return one existing owner-accessible canonical directory.

    The value is data only: it is never interpolated into a command. Symlink
    resolution happens before the configured owner-root boundary check, which
    rejects every escape while allowing the canonical target to be persisted.
    """
    settings = settings or get_settings()
    if not isinstance(project_path, str) or not project_path.strip():
        raise ProjectOnboardingError("PATH_REQUIRED", "Укажите путь проекта.")
    value = project_path.strip()
    if len(value) > 4096 or _CONTROL_RE.search(value):
        raise ProjectOnboardingError("PATH_INVALID", "Путь проекта некорректен.")
    raw = Path(value).expanduser()
    if not raw.is_absolute() or any(part == ".." for part in raw.parts):
        raise ProjectOnboardingError("PATH_INVALID", "Путь проекта должен быть абсолютным.")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise ProjectOnboardingError("PATH_INVALID", "Каталог проекта не найден.") from exc
    if not resolved.is_dir():
        raise ProjectOnboardingError("PATH_INVALID", "Путь проекта не является каталогом.")
    roots: list[Path] = []
    for configured in settings.ai_project_roots:
        try:
            root = configured.expanduser().resolve(strict=True)
        except OSError:
            continue
        if root.is_dir():
            roots.append(root)
    if not roots or not any(resolved == root or _is_relative_to(resolved, root) for root in roots):
        raise ProjectOnboardingError("PATH_NOT_ALLOWED", "Каталог вне разрешённых owner roots.")
    try:
        metadata = resolved.stat()
    except OSError as exc:
        raise ProjectOnboardingError("PATH_INVALID", "Каталог проекта недоступен.") from exc
    if metadata.st_uid != os.geteuid() or not os.access(resolved, os.R_OK | os.X_OK):
        raise ProjectOnboardingError(
            "PATH_NOT_ALLOWED",
            "Каталог недоступен владельцу сервиса.",
        )
    return str(resolved)


def onboarding_missing_fields(
    *,
    project_path: str | None,
    settings_record: dict[str, Any] | None,
    runtime_record: dict[str, Any] | None,
) -> list[str]:
    missing: list[str] = []
    if not project_path:
        missing.append("project_path")
    account = settings_record.get("account_profile") if settings_record else None
    model = settings_record.get("model") if settings_record else None
    reasoning = settings_record.get("reasoning_effort") if settings_record else None
    if not isinstance(account, str) or not account:
        missing.append("account_profile")
    if not isinstance(model, str) or not model:
        missing.append("model")
    if not isinstance(reasoning, str) or not reasoning:
        missing.append("reasoning_effort")
    runtime_matches = bool(
        runtime_record
        and settings_record
        and runtime_record.get("project_path") == project_path
        and runtime_record.get("provider_id") == settings_record.get("provider_id")
        and runtime_record.get("account_profile") == account
        and runtime_record.get("model") == model
        and runtime_record.get("reasoning_effort") == reasoning
        and runtime_record.get("settings_revision") == settings_record.get("revision")
        and isinstance(runtime_record.get("spec_sha256"), str)
    )
    if not runtime_matches:
        missing.append("runtime_launch_spec")
    return missing
