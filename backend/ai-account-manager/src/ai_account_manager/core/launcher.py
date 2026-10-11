from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.core.project_settings import (
    ProjectSettingsError,
    canonical_project_path,
    read_project_settings_store,
    validate_project_id,
    validate_provider_project_settings,
)
from ai_account_manager.providers.codex.auth_status import get_codex_auth_status
from ai_account_manager.schemas.launcher import (
    ImmutableLaunchSpec,
    LauncherResolveRequest,
    LauncherResolveResponse,
)

_SAFE_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}$")
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class LauncherResolveError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _validate_slug(account_slug: str) -> None:
    if not _SAFE_SLUG_RE.fullmatch(account_slug):
        raise LauncherResolveError("invalid_account_slug", "Invalid account slug")


def _resolve_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_string(value: str, *, field_name: str, max_length: int = 512) -> str:
    if not isinstance(value, str):
        raise LauncherResolveError(f"invalid_{field_name}", f"Invalid {field_name}")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length or _CONTROL_RE.search(normalized):
        raise LauncherResolveError(f"invalid_{field_name}", f"Invalid {field_name}")
    return normalized


def _resolved_command(settings: AppSettings) -> str:
    command = str(settings.codex_binary.expanduser().resolve(strict=False))
    return _safe_string(command, field_name="command", max_length=4096)


def _reasoning_override(reasoning_effort: str) -> str:
    return "model_reasoning_effort=" + json.dumps(reasoning_effort)


def build_codex_start_argv(
    *,
    codex_binary: str,
    model: str,
    reasoning_effort: str,
    cwd: str,
) -> list[str]:
    return [
        _safe_string(codex_binary, field_name="codex_binary", max_length=4096),
        "--model",
        _safe_string(model, field_name="model", max_length=200),
        "-c",
        _reasoning_override(_safe_string(reasoning_effort, field_name="reasoning_effort")),
        "--cd",
        _safe_string(cwd, field_name="cwd", max_length=4096),
    ]


def build_codex_resume_argv(
    *,
    codex_binary: str,
    model: str,
    reasoning_effort: str,
    session_id: str,
    cwd: str,
) -> list[str]:
    session = _safe_string(session_id, field_name="session_id")
    if not _SAFE_SESSION_ID_RE.fullmatch(session) or session.startswith("-"):
        raise LauncherResolveError("invalid_session_id", "Invalid session id")
    return [
        _safe_string(codex_binary, field_name="codex_binary", max_length=4096),
        "--model",
        _safe_string(model, field_name="model", max_length=200),
        "-c",
        _reasoning_override(_safe_string(reasoning_effort, field_name="reasoning_effort")),
        "resume",
        session,
        "--cd",
        _safe_string(cwd, field_name="cwd", max_length=4096),
    ]


def _spec_hash(spec_core: dict[str, object]) -> str:
    encoded = json.dumps(
        spec_core,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _profile_path_for_codex(settings: AppSettings, account_profile: str) -> Path:
    profile = (settings.codex_profiles_root / account_profile).expanduser().resolve(strict=False)
    root = settings.codex_profiles_root.expanduser().resolve(strict=False)
    if not _is_relative_to(profile, root):
        raise LauncherResolveError("profile_missing", "Codex profile is outside CODEX_PROFILES_ROOT")
    auth_json = profile / "auth.json"
    if profile.is_symlink() or not profile.is_dir() or auth_json.is_symlink() or not auth_json.is_file():
        raise LauncherResolveError("profile_missing", "Codex profile auth.json is missing")
    return profile


def _project_settings_error(exc: ProjectSettingsError) -> LauncherResolveError:
    if exc.code == "settings_store_invalid":
        return LauncherResolveError("settings_store_invalid", exc.message)
    if exc.code in {
        "invalid_project_id",
        "project_path_not_absolute",
        "project_outside_allowed_roots",
        "project_missing",
    }:
        return LauncherResolveError(exc.code, exc.message)
    if exc.code == "profile_missing":
        return LauncherResolveError("profile_missing", exc.message)
    return LauncherResolveError(exc.code, exc.message)


def build_project_launch_spec(
    *,
    project_id: str,
    expected_settings_revision: int,
    allow_last_known_good: bool = False,
    settings: AppSettings | None = None,
) -> ImmutableLaunchSpec:
    settings = settings or get_settings()
    try:
        normalized_project_id = validate_project_id(project_id)
        store, _ = read_project_settings_store(settings)
    except ProjectSettingsError as exc:
        raise _project_settings_error(exc) from exc

    raw_record = store.get("projects", {}).get(normalized_project_id)
    if raw_record is None:
        raise LauncherResolveError("settings_missing", "Project settings are missing")

    if not isinstance(raw_record, dict):
        raise LauncherResolveError("settings_store_invalid", "Project settings record is invalid")

    try:
        revision = raw_record.get("revision")
        if revision != expected_settings_revision:
            raise LauncherResolveError("revision_mismatch", "Project settings revision mismatch")
        provider_id = _safe_string(str(raw_record.get("provider_id") or ""), field_name="provider_id").lower()
        if provider_id != "codex":
            raise LauncherResolveError("provider_settings_not_supported", "Provider launcher is not supported")
        account_profile = _safe_string(
            str(raw_record.get("account_profile") or ""),
            field_name="account_profile",
            max_length=80,
        )
        model = raw_record.get("model")
        reasoning_effort = raw_record.get("reasoning_effort")
        if not isinstance(model, str) or not isinstance(reasoning_effort, str):
            raise LauncherResolveError("settings_incomplete", "Project settings are incomplete")
        project_path = str(raw_record.get("project_path") or "")
        cwd_path = canonical_project_path(project_path, settings)
    except ProjectSettingsError as exc:
        raise _project_settings_error(exc) from exc

    if str(cwd_path) != project_path:
        raise LauncherResolveError("noncanonical_cwd", "Project settings path is not canonical")

    try:
        validation = validate_provider_project_settings(
            provider_id=provider_id,
            account_profile=account_profile,
            model=model,
            reasoning_effort=reasoning_effort,
            settings=settings,
        )
    except ProjectSettingsError as exc:
        raise _project_settings_error(exc) from exc

    if validation.capability_source != "current" and not allow_last_known_good:
        raise LauncherResolveError(
            "capabilities_current_required",
            "Current Codex capabilities are required",
        )

    profile_path = _profile_path_for_codex(settings, account_profile)
    auth_status = get_codex_auth_status(account_profile, settings=settings).status
    command = _resolved_command(settings)
    cwd = str(cwd_path)
    env = {"CODEX_HOME": str(profile_path)}
    argv = build_codex_start_argv(
        codex_binary=command,
        model=model,
        reasoning_effort=reasoning_effort,
        cwd=cwd,
    )
    warnings = list(validation.warnings)
    if auth_status != "ready":
        warnings.append(f"codex_auth_status:{auth_status}")

    core = {
        "spec_version": 1,
        "provider_id": provider_id,
        "project_id": normalized_project_id,
        "project_path": project_path,
        "cwd": cwd,
        "account_profile": account_profile,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "settings_revision": expected_settings_revision,
        "command": command,
        "argv": argv,
        "env": env,
    }
    return ImmutableLaunchSpec(
        **core,
        auth_status=auth_status,
        capability_source=validation.capability_source,
        warnings=warnings,
        spec_sha256=_spec_hash(core),
    )


def _resolve_project_path(project_path: str, settings: AppSettings) -> Path:
    raw = Path(project_path).expanduser()
    if not raw.is_absolute():
        raise LauncherResolveError("project_path_not_absolute", "Project path must be absolute")

    resolved = _resolve_path(raw)
    allowed_roots = [_resolve_path(root) for root in settings.ai_project_roots]
    if not any(_is_relative_to(resolved, root) or resolved == root for root in allowed_roots):
        raise LauncherResolveError(
            "project_outside_allowed_roots", "Project path is outside configured AI_PROJECT_ROOTS"
        )

    if not resolved.is_dir():
        raise LauncherResolveError("project_missing", "Project path does not exist or is not a directory")
    return resolved


def resolve_launcher(
    request: LauncherResolveRequest, *, settings: AppSettings | None = None
) -> LauncherResolveResponse:
    """Resolve a safe launch spec without starting processes.

    The launch spec separates project working directory from provider profile state:
    `cwd` controls what project the agent edits, while provider-specific env such as
    `CODEX_HOME` controls which account/profile the agent uses.
    """
    settings = settings or get_settings()
    provider_id = request.provider_id.strip().lower()
    account_slug = request.account_slug.strip()
    _validate_slug(account_slug)
    cwd = _resolve_project_path(request.project_path, settings)

    if provider_id == "codex":
        profile_path = settings.codex_profiles_root / account_slug
        auth_status = get_codex_auth_status(account_slug, settings=settings)
        warnings: list[str] = []
        if auth_status.status != "ready":
            warnings.append(f"codex_auth_status:{auth_status.status}")

        return LauncherResolveResponse(
            provider_id="codex",
            account_slug=account_slug,
            project_path=str(cwd),
            cwd=str(cwd),
            command="codex",
            argv=["codex"],
            env={"CODEX_HOME": str(profile_path)},
            profile_path=str(profile_path),
            profile_path_exists=profile_path.is_dir(),
            auth_status=auth_status.status,
            warnings=warnings,
        )

    if provider_id in {"claude_code", "gemini_cli"}:
        raise LauncherResolveError("provider_not_implemented", f"Provider {provider_id} launcher is not implemented")

    raise LauncherResolveError("provider_unknown", "Unknown provider")
