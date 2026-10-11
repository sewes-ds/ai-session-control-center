from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from pydantic import ValidationError

from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.schemas.sessions import (
    ProjectBinding,
    ProjectSettingsMigrationMetadata,
    ProjectSettingsMigrationResult,
    ProjectSettingsRecord,
    ProjectSettingsRuntimeView,
    ProjectSettingsValidationResponse,
)

STORE_VERSION = 2
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PROJECT_ID_RE = re.compile(r"^project_[0-9a-f]{20}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ProjectSettingsError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def utc_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def project_id_for_path(project_path: str | Path) -> str:
    canonical = str(Path(project_path).expanduser().resolve(strict=False))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return f"project_{digest}"


def validate_project_id(project_id: str) -> str:
    value = project_id.strip()
    if not PROJECT_ID_RE.fullmatch(value):
        raise ProjectSettingsError("invalid_project_id", "Invalid project settings id")
    return value


def validate_safe_identifier(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not SAFE_ID_RE.fullmatch(normalized):
        raise ProjectSettingsError(f"invalid_{field_name}", f"Invalid {field_name}")
    return normalized


def validate_provider_value(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 200 or _CONTROL_RE.search(normalized):
        raise ProjectSettingsError(f"invalid_{field_name}", f"Invalid {field_name}")
    return normalized


def _project_root() -> Path:
    return Path(__file__).resolve().parents[5]


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
        raise ProjectSettingsError("project_path_not_absolute", "Project path must be absolute")
    resolved = raw.resolve(strict=False)
    roots = [root.expanduser().resolve(strict=False) for root in settings.ai_project_roots]
    if not any(resolved == root or _is_relative_to(resolved, root) for root in roots):
        raise ProjectSettingsError(
            "project_outside_allowed_roots", "Project path is outside AI_PROJECT_ROOTS"
        )
    if not resolved.is_dir():
        raise ProjectSettingsError("project_missing", "Project path does not exist")
    return resolved


def _settings_path(settings: AppSettings) -> Path:
    raw = settings.project_settings_path.expanduser()
    if any(part == ".." for part in raw.parts):
        raise ProjectSettingsError("invalid_settings_path", "Settings path must not use traversal")
    if not raw.is_absolute():
        raise ProjectSettingsError("invalid_settings_path", "Settings path must be absolute")
    path = raw.resolve(strict=False)
    repo_root = _project_root().resolve(strict=False)
    if path == repo_root or _is_relative_to(path, repo_root):
        raise ProjectSettingsError(
            "invalid_settings_path", "Settings path must not be inside the Git repository"
        )
    if raw.is_symlink() or path.is_symlink():
        raise ProjectSettingsError("invalid_settings_path", "Settings path must not be a symlink")
    if path.parent.is_symlink():
        raise ProjectSettingsError("invalid_settings_path", "Settings parent must not be a symlink")
    return path


def project_settings_lock_path(settings: AppSettings | None = None) -> Path:
    settings = settings or get_settings()
    return _settings_path(settings).parent / "project_settings_v2.lock"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)


@contextmanager
def _locked(settings: AppSettings, *, create_parent: bool) -> Iterator[Path]:
    settings_path = _settings_path(settings)
    lock_path = settings_path.parent / "project_settings_v2.lock"
    if lock_path.is_symlink():
        raise ProjectSettingsError("invalid_settings_path", "Lock path must not be a symlink")
    if create_parent:
        _ensure_parent(settings_path)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        with os.fdopen(fd, "r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield settings_path
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        pass


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("top-level settings store is not an object")
    return payload


def _validate_record(raw: Any) -> ProjectSettingsRecord:
    record = ProjectSettingsRecord.model_validate(raw)
    validate_project_id(record.project_id)
    validate_safe_identifier(record.provider_id, field_name="provider_id")
    validate_safe_identifier(record.account_profile, field_name="account_profile")
    if record.model is not None:
        validate_provider_value(record.model, field_name="model")
    if record.reasoning_effort is not None:
        validate_provider_value(record.reasoning_effort, field_name="reasoning_effort")
    if record.project_id != project_id_for_path(record.project_path):
        raise ProjectSettingsError("settings_store_invalid", "Project id does not match path")
    return record


def _validate_store(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("version") != STORE_VERSION:
        raise ProjectSettingsError("settings_store_invalid", "Unsupported settings store version")
    revision = payload.get("store_revision")
    if not isinstance(revision, int) or revision < 1:
        raise ProjectSettingsError("settings_store_invalid", "Invalid settings store revision")
    migrated = ProjectSettingsMigrationMetadata.model_validate(payload.get("migrated_from"))
    projects = payload.get("projects")
    if not isinstance(projects, dict):
        raise ProjectSettingsError("settings_store_invalid", "Invalid project settings map")
    validated_projects: dict[str, dict[str, Any]] = {}
    for project_id, raw in projects.items():
        if not isinstance(project_id, str):
            raise ProjectSettingsError("settings_store_invalid", "Invalid project id key")
        record = _validate_record(raw)
        if project_id != record.project_id:
            raise ProjectSettingsError("settings_store_invalid", "Project id key mismatch")
        validated_projects[project_id] = record.model_dump()
    return {
        "version": STORE_VERSION,
        "store_revision": revision,
        "migrated_from": migrated.model_dump(),
        "projects": validated_projects,
    }


def _quarantine_invalid(path: Path) -> Path:
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.{stamp}.invalid")
    suffix = 0
    while target.exists():
        suffix += 1
        target = path.with_name(f"{path.name}.{stamp}.{suffix}.invalid")
    os.replace(path, target)
    os.chmod(target, 0o600)
    return target


def _load_existing_store(settings: AppSettings) -> dict[str, Any]:
    path = _settings_path(settings)
    if not path.exists():
        raise ProjectSettingsError("settings_not_found", "Project settings store does not exist")
    if path.is_symlink():
        raise ProjectSettingsError("invalid_settings_path", "Settings path must not be a symlink")
    try:
        return _validate_store(_read_json(path))
    except (OSError, json.JSONDecodeError, ValueError, ValidationError, ProjectSettingsError) as exc:
        with _locked(settings, create_parent=True) as locked_path:
            if locked_path.exists() and not locked_path.is_symlink():
                try:
                    _validate_store(_read_json(locked_path))
                except (
                    OSError,
                    json.JSONDecodeError,
                    ValueError,
                    ValidationError,
                    ProjectSettingsError,
                ):
                    _quarantine_invalid(locked_path)
                    raise ProjectSettingsError(
                        "settings_store_invalid", "Project settings store is invalid"
                    ) from exc
        raise ProjectSettingsError("settings_store_invalid", "Project settings store is invalid") from exc


def _load_store_locked(path: Path) -> dict[str, Any]:
    try:
        return _validate_store(_read_json(path))
    except (OSError, json.JSONDecodeError, ValueError, ValidationError, ProjectSettingsError) as exc:
        _quarantine_invalid(path)
        raise ProjectSettingsError("settings_store_invalid", "Project settings store is invalid") from exc


def _write_store_checked(path: Path, payload: dict[str, Any]) -> None:
    try:
        _write_store_atomic(path, payload)
    except OSError as exc:
        raise ProjectSettingsError("settings_write_failed", "Project settings write failed") from exc


def read_project_settings_store(settings: AppSettings | None = None) -> tuple[dict[str, Any], bool]:
    settings = settings or get_settings()
    path = _settings_path(settings)
    if not path.exists():
        return _empty_store(None, None), True
    return _load_existing_store(settings), False


def _empty_store(source_sha256: str | None, migrated_at: str | None) -> dict[str, Any]:
    return {
        "version": STORE_VERSION,
        "store_revision": 1,
        "migrated_from": {
            "source": "project_bindings_v1",
            "source_sha256": source_sha256,
            "migrated_at": migrated_at,
        },
        "projects": {},
    }


def _write_store_atomic(path: Path, payload: dict[str, Any]) -> None:
    _ensure_parent(path)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    encoded += b"\n"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _sha256_or_none(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _legacy_container(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("projects", "bindings"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _legacy_error_key(project_path: object) -> str:
    raw = project_path if isinstance(project_path, str) else repr(project_path)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _record_from_legacy(project_path: str, raw: Any) -> ProjectSettingsRecord:
    if not isinstance(raw, dict):
        raise ProjectSettingsError("invalid_legacy_record", "Legacy record must be an object")
    project = Path(project_path).expanduser()
    if not project.is_absolute():
        raise ProjectSettingsError("invalid_project_path", "Legacy project path must be absolute")
    canonical = str(project.resolve(strict=False))
    provider = validate_safe_identifier(str(raw.get("provider_id") or ""), field_name="provider_id")
    account = validate_safe_identifier(
        str(raw.get("account_slug") or raw.get("account_profile") or ""),
        field_name="account_profile",
    )
    updated_at = raw.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at:
        updated_at = utc_iso()
    project_id = project_id_for_path(canonical)
    return ProjectSettingsRecord(
        project_id=project_id,
        project_path=canonical,
        provider_id=provider,
        account_profile=account,
        model=None,
        reasoning_effort=None,
        updated_at=updated_at,
        revision=1,
        source="legacy_v1",
    )


def migrate_project_settings_v2(
    settings: AppSettings | None = None,
) -> ProjectSettingsMigrationResult:
    settings = settings or get_settings()
    with _locked(settings, create_parent=True) as path:
        if path.exists():
            store = _load_store_locked(path)
            return ProjectSettingsMigrationResult(
                status="already_migrated",
                imported_count=len(store["projects"]),
                skipped_count=0,
                store_revision=store["store_revision"],
                source_sha256=store["migrated_from"].get("source_sha256"),
                settings_path=str(path),
            )

        source_sha = _sha256_or_none(settings.project_bindings_path)
        migrated_at = utc_iso()
        store = _empty_store(source_sha, migrated_at)
        skipped: list[dict[str, str]] = []
        if source_sha is not None:
            try:
                legacy = _read_json(settings.project_bindings_path)
            except (OSError, json.JSONDecodeError, ValueError):
                legacy = {}
            if legacy.get("version") == 1:
                for project_path, raw in sorted(_legacy_container(legacy).items()):
                    try:
                        if not isinstance(project_path, str):
                            raise ProjectSettingsError(
                                "invalid_project_path", "Legacy project path is not a string"
                            )
                        record = _record_from_legacy(project_path, raw)
                    except ProjectSettingsError as exc:
                        skipped.append({"path_hash": _legacy_error_key(project_path), "code": exc.code})
                        continue
                    store["projects"][record.project_id] = record.model_dump()
        _write_store_checked(path, _validate_store(store))
        status = "partial" if skipped else ("migrated" if store["projects"] else "empty_created")
        return ProjectSettingsMigrationResult(
            status=status,
            imported_count=len(store["projects"]),
            skipped_count=len(skipped),
            skipped=skipped,
            store_revision=store["store_revision"],
            source_sha256=source_sha,
            settings_path=str(path),
        )


def validate_provider_project_settings(
    *,
    provider_id: str,
    account_profile: str,
    model: str,
    reasoning_effort: str,
    settings: AppSettings | None = None,
) -> ProjectSettingsValidationResponse:
    provider = validate_safe_identifier(provider_id, field_name="provider_id").lower()
    account = validate_safe_identifier(account_profile, field_name="account_profile")
    model_value = validate_provider_value(model, field_name="model")
    effort_value = validate_provider_value(reasoning_effort, field_name="reasoning_effort")
    if provider == "codex":
        from ai_account_manager.providers.codex.project_settings import validate_codex_settings

        return validate_codex_settings(
            account_profile=account,
            model=model_value,
            reasoning_effort=effort_value,
            settings=settings,
        )
    raise ProjectSettingsError(
        "provider_settings_not_supported", "Provider settings validation is not supported"
    )


def upsert_project_settings(
    *,
    project_path: str,
    provider_id: str,
    account_profile: str,
    model: str,
    reasoning_effort: str,
    expected_revision: int | None,
    settings: AppSettings | None = None,
) -> ProjectSettingsRecord:
    record, _previous = upsert_project_settings_with_snapshot(
        project_path=project_path,
        provider_id=provider_id,
        account_profile=account_profile,
        model=model,
        reasoning_effort=reasoning_effort,
        expected_revision=expected_revision,
        settings=settings,
    )
    return record


def upsert_project_settings_with_snapshot(
    *,
    project_path: str,
    provider_id: str,
    account_profile: str,
    model: str,
    reasoning_effort: str,
    expected_revision: int | None,
    settings: AppSettings | None = None,
) -> tuple[ProjectSettingsRecord, ProjectSettingsRecord | None]:
    """CAS-write desired settings and return the exact record it replaced."""
    settings = settings or get_settings()
    canonical = canonical_project_path(project_path, settings)
    provider = validate_safe_identifier(provider_id, field_name="provider_id").lower()
    account = validate_safe_identifier(account_profile, field_name="account_profile")
    model_value = validate_provider_value(model, field_name="model")
    effort_value = validate_provider_value(reasoning_effort, field_name="reasoning_effort")

    with _locked(settings, create_parent=True) as path:
        if path.exists():
            store = _load_store_locked(path)
        else:
            store = _empty_store(None, None)
        project_id = project_id_for_path(canonical)
        existing = store["projects"].get(project_id)
        if existing is None:
            if expected_revision is not None:
                raise ProjectSettingsError("revision_conflict", "Project settings revision conflict")
            previous = None
            revision = 1
        else:
            current = _validate_record(existing)
            if expected_revision != current.revision:
                raise ProjectSettingsError("revision_conflict", "Project settings revision conflict")
            previous = current
            revision = current.revision + 1
        # Project trust belongs to the CUS project identity, not to the selected
        # account profile. Materialize the provider projection only after the
        # settings CAS has been validated and before publishing its record.
        from ai_account_manager.core.project_trust import (
            ProjectTrustError,
            reconcile_project_trust,
        )

        try:
            reconcile_project_trust(
                provider_id=provider,
                project_paths=(canonical,),
                settings=settings,
            )
        except ProjectTrustError as exc:
            raise ProjectSettingsError(exc.code, exc.message) from exc

        record = ProjectSettingsRecord(
            project_id=project_id,
            project_path=str(canonical),
            provider_id=provider,
            account_profile=account,
            model=model_value,
            reasoning_effort=effort_value,
            updated_at=utc_iso(),
            revision=revision,
            source="api",
        )
        store["projects"][project_id] = record.model_dump()
        store["store_revision"] += 1
        _write_store_checked(path, _validate_store(store))
        return record, previous


def restore_project_settings_record(
    *,
    project_id: str,
    project_path: str,
    expected_current: ProjectSettingsRecord,
    previous_record: ProjectSettingsRecord | None,
    settings: AppSettings | None = None,
) -> None:
    """Restore one apply transaction's desired record under an exact CAS guard."""
    settings = settings or get_settings()
    try:
        canonical = canonical_project_path(project_path, settings)
        normalized_id = validate_project_id(project_id)
        if normalized_id != project_id_for_path(canonical):
            raise ProjectSettingsError("settings_apply_rollback_failed", "Project identity changed")
        expected = _validate_record(expected_current.model_dump())
        if expected.project_id != normalized_id or expected.project_path != str(canonical):
            raise ProjectSettingsError("settings_apply_rollback_failed", "Project settings changed")
        previous = (
            None
            if previous_record is None
            else _validate_record(previous_record.model_dump())
        )
        if previous is not None and (
            previous.project_id != normalized_id or previous.project_path != str(canonical)
        ):
            raise ProjectSettingsError("settings_apply_rollback_failed", "Previous settings invalid")

        with _locked(settings, create_parent=True) as path:
            if not path.exists():
                raise ProjectSettingsError(
                    "settings_apply_rollback_failed", "Project settings store disappeared"
                )
            store = _load_store_locked(path)
            current_raw = store["projects"].get(normalized_id)
            if current_raw is None:
                raise ProjectSettingsError(
                    "settings_apply_rollback_failed", "Project settings changed"
                )
            current = _validate_record(current_raw)
            if current.model_dump() != expected.model_dump():
                raise ProjectSettingsError(
                    "settings_apply_rollback_failed", "Project settings changed"
                )
            if previous is None:
                del store["projects"][normalized_id]
            else:
                store["projects"][normalized_id] = previous.model_dump()
            store["store_revision"] += 1
            _write_store_checked(path, _validate_store(store))
    except ProjectSettingsError as exc:
        if exc.code == "settings_apply_rollback_failed":
            raise
        raise ProjectSettingsError(
            "settings_apply_rollback_failed", "Failed to restore project settings"
        ) from exc


def runtime_view_for_record(
    record: ProjectSettingsRecord,
    bindings: list[ProjectBinding],
    runtime_record: dict[str, Any] | None = None,
    expected_spec_sha256: str | None = None,
    runtime_store_invalid: bool = False,
) -> ProjectSettingsRuntimeView:
    if runtime_store_invalid:
        return ProjectSettingsRuntimeView(
            runtime_account_profile=None,
            runtime_account_matches=None,
            runtime_application_status="runtime_spec_invalid",
        )

    binding = next((item for item in bindings if item.project_path == record.project_path), None)
    binding_matches = binding is not None and binding.account_slug == record.account_profile
    if runtime_record is not None:
        runtime_account = runtime_record.get("account_profile")
        runtime_model = runtime_record.get("model")
        runtime_reasoning = runtime_record.get("reasoning_effort")
        runtime_settings_revision = runtime_record.get("settings_revision")
        runtime_spec_sha256 = runtime_record.get("spec_sha256")
        runtime_matches = (
            record.model is not None
            and record.reasoning_effort is not None
            and runtime_account == record.account_profile
            and runtime_model == record.model
            and runtime_reasoning == record.reasoning_effort
            and runtime_settings_revision == record.revision
            and runtime_spec_sha256 == expected_spec_sha256
            and binding_matches
        )
        return ProjectSettingsRuntimeView(
            runtime_account_profile=runtime_account if isinstance(runtime_account, str) else None,
            runtime_account_matches=bool(runtime_account == record.account_profile and binding_matches),
            runtime_application_status="applied" if runtime_matches else "runtime_spec_stale",
            runtime_model=runtime_model if isinstance(runtime_model, str) else None,
            runtime_reasoning_effort=runtime_reasoning if isinstance(runtime_reasoning, str) else None,
            runtime_settings_revision=(
                runtime_settings_revision if isinstance(runtime_settings_revision, int) else None
            ),
            runtime_spec_sha256=runtime_spec_sha256 if isinstance(runtime_spec_sha256, str) else None,
        )

    if binding is None:
        return ProjectSettingsRuntimeView(
            runtime_account_profile=None,
            runtime_account_matches=None,
            runtime_application_status="runtime_unknown",
        )
    if record.model is None or record.reasoning_effort is None:
        status = "incomplete"
    elif binding_matches:
        status = "account_matches_settings_pending"
    else:
        status = "account_differs"
    return ProjectSettingsRuntimeView(
        runtime_account_profile=binding.account_slug,
        runtime_account_matches=binding_matches,
        runtime_application_status=status,
    )
