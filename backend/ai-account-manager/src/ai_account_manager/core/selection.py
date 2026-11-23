from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.schemas.selection import ProviderSelectionValidationResponse

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PROJECT_ID_RE = re.compile(r"^project_[0-9a-f]{20}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class SelectionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def project_id_for_path(project_path: str | Path) -> str:
    canonical = str(Path(project_path).expanduser().resolve(strict=False))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return f"project_{digest}"


def validate_project_id(project_id: str) -> str:
    value = project_id.strip()
    if not PROJECT_ID_RE.fullmatch(value):
        raise SelectionError("invalid_project_id", "Invalid project id")
    return value


def validate_safe_identifier(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not SAFE_ID_RE.fullmatch(normalized):
        raise SelectionError(f"invalid_{field_name}", f"Invalid {field_name}")
    return normalized


def validate_provider_value(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 200 or _CONTROL_RE.search(normalized):
        raise SelectionError(f"invalid_{field_name}", f"Invalid {field_name}")
    return normalized


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def canonical_project_path(project_path: str, settings: AppSettings | None = None) -> Path:
    settings = settings or get_settings()
    raw = Path(project_path).expanduser()
    if not raw.is_absolute():
        raise SelectionError("project_path_not_absolute", "Project path must be absolute")
    resolved = raw.resolve(strict=False)
    roots = [root.expanduser().resolve(strict=False) for root in settings.ai_project_roots]
    if not any(resolved == root or _is_relative_to(resolved, root) for root in roots):
        raise SelectionError(
            "project_outside_allowed_roots", "Project path is outside AI_PROJECT_ROOTS"
        )
    if not resolved.is_dir():
        raise SelectionError("project_missing", "Project path does not exist")
    return resolved


def validate_provider_selection(
    *,
    provider_id: str,
    account_profile: str,
    model: str,
    reasoning_effort: str,
    settings: AppSettings | None = None,
) -> ProviderSelectionValidationResponse:
    provider = validate_safe_identifier(provider_id, field_name="provider_id").lower()
    account = validate_safe_identifier(account_profile, field_name="account_profile")
    model_value = validate_provider_value(model, field_name="model")
    effort_value = validate_provider_value(reasoning_effort, field_name="reasoning_effort")
    if provider == "codex":
        from ai_account_manager.providers.codex.selection import validate_codex_selection

        return validate_codex_selection(
            account_profile=account,
            model=model_value,
            reasoning_effort=effort_value,
            settings=settings,
        )
    if provider == "claude_code":
        from ai_account_manager.providers.claude_code.deepseek import (
            DeepSeekProviderError,
            validate_deepseek_selection,
        )

        try:
            return validate_deepseek_selection(
                account_profile=account,
                model=model_value,
                reasoning_effort=effort_value,
                settings=settings,
            )
        except DeepSeekProviderError as exc:
            raise SelectionError(exc.code, exc.message) from exc
    raise SelectionError(
        "provider_selection_not_supported", "Provider selection validation is not supported"
    )
