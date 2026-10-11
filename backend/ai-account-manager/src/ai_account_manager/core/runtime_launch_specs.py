from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from ai_account_manager.core.project_settings import (
    project_id_for_path,
    validate_project_id,
    validate_provider_value,
    validate_safe_identifier,
)

STORE_VERSION = 1
SOURCE = "project_settings_v2"
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEYS = {
    "auth",
    "auth_json",
    "token",
    "tokens",
    "access_token",
    "refresh_token",
    "api_key",
    "secret",
    "password",
    "env",
    "account_id",
    "email",
}


class RuntimeLaunchSpecStoreError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def empty_runtime_launch_spec_store() -> dict[str, Any]:
    return {"version": STORE_VERSION, "store_revision": 1, "projects": {}}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _store_path(path: str | Path) -> Path:
    raw = Path(path).expanduser()
    if any(part == ".." for part in raw.parts):
        raise RuntimeLaunchSpecStoreError("invalid_store_path", "Store path must not use traversal")
    if not raw.is_absolute():
        raise RuntimeLaunchSpecStoreError("invalid_store_path", "Store path must be absolute")
    resolved = raw.resolve(strict=False)
    repo_root = _project_root().resolve(strict=False)
    if resolved == repo_root or _is_relative_to(resolved, repo_root):
        raise RuntimeLaunchSpecStoreError(
            "invalid_store_path", "Store path must not be inside the Git repository"
        )
    if raw.is_symlink() or resolved.is_symlink():
        raise RuntimeLaunchSpecStoreError("invalid_store_path", "Store path must not be a symlink")
    if resolved.parent.exists() and resolved.parent.is_symlink():
        raise RuntimeLaunchSpecStoreError(
            "invalid_store_path", "Store parent must not be a symlink"
        )
    return resolved


def _lock_path(path: Path) -> Path:
    return path.parent / f"{path.name}.lock"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)


@contextmanager
def _locked(path: Path, *, create_parent: bool) -> Iterator[Path]:
    if create_parent:
        _ensure_parent(path)
    lock_path = _lock_path(path)
    if lock_path.is_symlink():
        raise RuntimeLaunchSpecStoreError("invalid_store_path", "Lock path must not be a symlink")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        with os.fdopen(fd, "r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield path
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        pass


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


def _safe_string(value: Any, *, field_name: str, max_length: int = 512) -> str:
    if not isinstance(value, str):
        raise RuntimeLaunchSpecStoreError(f"invalid_{field_name}", f"Invalid {field_name}")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length or _CONTROL_RE.search(normalized):
        raise RuntimeLaunchSpecStoreError(f"invalid_{field_name}", f"Invalid {field_name}")
    return normalized


def _validate_iso(value: Any) -> str:
    raw = _safe_string(value, field_name="applied_at")
    parsed = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise RuntimeLaunchSpecStoreError("invalid_applied_at", "Invalid applied_at") from exc
    return raw


def _canonical_project_path(value: Any) -> str:
    raw_value = _safe_string(value, field_name="project_path", max_length=4096)
    raw = Path(raw_value).expanduser()
    if any(part == ".." for part in raw.parts):
        raise RuntimeLaunchSpecStoreError(
            "invalid_project_path", "Project path must not use traversal"
        )
    if not raw.is_absolute():
        raise RuntimeLaunchSpecStoreError("invalid_project_path", "Project path must be absolute")
    resolved = raw.resolve(strict=False)
    if raw_value != str(resolved):
        raise RuntimeLaunchSpecStoreError("invalid_project_path", "Project path must be canonical")
    if raw.is_symlink() or resolved.is_symlink():
        raise RuntimeLaunchSpecStoreError(
            "invalid_project_path", "Project path must not be a symlink"
        )
    return str(resolved)


def _assert_no_secret_keys(raw: Any) -> None:
    if isinstance(raw, dict):
        for key, value in raw.items():
            if not isinstance(key, str):
                raise RuntimeLaunchSpecStoreError("invalid_store", "Store keys must be strings")
            if key.lower() in _SECRET_KEYS:
                raise RuntimeLaunchSpecStoreError(
                    "secret_field_rejected", "Secret-like field rejected"
                )
            _assert_no_secret_keys(value)
    elif isinstance(raw, list):
        for value in raw:
            _assert_no_secret_keys(value)


def _int_at_least(value: Any, *, field_name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RuntimeLaunchSpecStoreError(f"invalid_{field_name}", f"Invalid {field_name}")
    return value


def _validate_record(raw: Any, *, require_runtime_revision: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RuntimeLaunchSpecStoreError(
            "invalid_record", "Runtime launch spec record must be an object"
        )
    _assert_no_secret_keys(raw)
    allowed = {
        "project_id",
        "project_path",
        "provider_id",
        "account_profile",
        "model",
        "reasoning_effort",
        "settings_revision",
        "runtime_revision",
        "spec_sha256",
        "applied_at",
        "source",
    }
    if set(raw) - allowed:
        raise RuntimeLaunchSpecStoreError(
            "invalid_record", "Runtime launch spec record has unsupported fields"
        )
    project_id = validate_project_id(_safe_string(raw.get("project_id"), field_name="project_id"))
    project_path = _canonical_project_path(raw.get("project_path"))
    if project_id != project_id_for_path(project_path):
        raise RuntimeLaunchSpecStoreError("invalid_project_id", "Project id does not match path")
    provider_id = validate_safe_identifier(
        _safe_string(raw.get("provider_id"), field_name="provider_id"),
        field_name="provider_id",
    ).lower()
    account_profile = validate_safe_identifier(
        _safe_string(raw.get("account_profile"), field_name="account_profile"),
        field_name="account_profile",
    )
    model = validate_provider_value(
        _safe_string(raw.get("model"), field_name="model"), field_name="model"
    )
    reasoning_effort = validate_provider_value(
        _safe_string(raw.get("reasoning_effort"), field_name="reasoning_effort"),
        field_name="reasoning_effort",
    )
    settings_revision = _int_at_least(
        raw.get("settings_revision"), field_name="settings_revision", minimum=1
    )
    runtime_revision_raw = raw.get("runtime_revision")
    if require_runtime_revision or runtime_revision_raw is not None:
        runtime_revision = _int_at_least(
            runtime_revision_raw,
            field_name="runtime_revision",
            minimum=1,
        )
    else:
        runtime_revision = 1
    spec_sha256 = _safe_string(raw.get("spec_sha256"), field_name="spec_sha256")
    if not _SHA_RE.fullmatch(spec_sha256):
        raise RuntimeLaunchSpecStoreError("invalid_spec_sha256", "Invalid spec sha256")
    applied_at = _validate_iso(raw.get("applied_at"))
    source = _safe_string(raw.get("source"), field_name="source")
    if source != SOURCE:
        raise RuntimeLaunchSpecStoreError("invalid_source", "Invalid runtime launch spec source")
    return {
        "project_id": project_id,
        "project_path": project_path,
        "provider_id": provider_id,
        "account_profile": account_profile,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "settings_revision": settings_revision,
        "runtime_revision": runtime_revision,
        "spec_sha256": spec_sha256,
        "applied_at": applied_at,
        "source": SOURCE,
    }


def _validate_store(raw: Any) -> dict[str, Any]:
    _assert_no_secret_keys(raw)
    if not isinstance(raw, dict):
        raise RuntimeLaunchSpecStoreError("invalid_store", "Store must be an object")
    if set(raw) != {"version", "store_revision", "projects"}:
        raise RuntimeLaunchSpecStoreError("invalid_store", "Store has unsupported fields")
    if raw.get("version") != STORE_VERSION:
        raise RuntimeLaunchSpecStoreError(
            "invalid_store", "Unsupported runtime launch spec store version"
        )
    store_revision = _int_at_least(
        raw.get("store_revision"), field_name="store_revision", minimum=1
    )
    projects = raw.get("projects")
    if not isinstance(projects, dict):
        raise RuntimeLaunchSpecStoreError("invalid_store", "Projects must be an object")
    validated: dict[str, dict[str, Any]] = {}
    for key, value in projects.items():
        if not isinstance(key, str):
            raise RuntimeLaunchSpecStoreError("invalid_store", "Project id key must be a string")
        record = _validate_record(value, require_runtime_revision=True)
        if key != record["project_id"]:
            raise RuntimeLaunchSpecStoreError("invalid_store", "Project id key mismatch")
        validated[key] = record
    return {"version": STORE_VERSION, "store_revision": store_revision, "projects": validated}


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("top-level runtime launch spec store is not an object")
    return payload


def _load_store_locked(path: Path) -> dict[str, Any]:
    try:
        return _validate_store(_read_json(path))
    except (OSError, json.JSONDecodeError, ValueError, RuntimeLaunchSpecStoreError) as exc:
        _quarantine_invalid(path)
        raise RuntimeLaunchSpecStoreError(
            "runtime_launch_spec_store_invalid",
            "Runtime launch spec store is invalid",
        ) from exc


def _write_store_atomic(path: Path, payload: dict[str, Any]) -> None:
    _ensure_parent(path)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    )
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
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


def read_runtime_launch_spec_store(path: str | Path) -> dict[str, Any]:
    store_path = _store_path(path)
    if not store_path.exists():
        return empty_runtime_launch_spec_store()
    if store_path.is_symlink():
        raise RuntimeLaunchSpecStoreError("invalid_store_path", "Store path must not be a symlink")
    with _locked(store_path, create_parent=False) as locked_path:
        store = _load_store_locked(locked_path)
    os.chmod(store_path, 0o600)
    return store


def get_runtime_launch_spec(path: str | Path, project_id: str) -> dict[str, Any] | None:
    normalized = validate_project_id(project_id)
    store = read_runtime_launch_spec_store(path)
    record = store["projects"].get(normalized)
    return dict(record) if record is not None else None


def upsert_runtime_launch_spec(
    path: str | Path,
    record: dict[str, Any],
    expected_runtime_revision: int | None,
) -> dict[str, Any]:
    store_path = _store_path(path)
    incoming = _validate_record(record, require_runtime_revision=False)
    with _locked(store_path, create_parent=True) as locked_path:
        store = (
            _load_store_locked(locked_path)
            if locked_path.exists()
            else empty_runtime_launch_spec_store()
        )
        existing = store["projects"].get(incoming["project_id"])
        if existing is None:
            if expected_runtime_revision is not None:
                raise RuntimeLaunchSpecStoreError(
                    "revision_conflict", "Runtime launch spec revision conflict"
                )
            incoming["runtime_revision"] = 1
        else:
            current_revision = _validate_record(existing, require_runtime_revision=True)[
                "runtime_revision"
            ]
            if expected_runtime_revision != current_revision:
                raise RuntimeLaunchSpecStoreError(
                    "revision_conflict", "Runtime launch spec revision conflict"
                )
            incoming["runtime_revision"] = current_revision + 1
        store["projects"][incoming["project_id"]] = incoming
        store["store_revision"] += 1
        validated = _validate_store(store)
        _write_store_atomic(locked_path, validated)
        return dict(incoming)


def reconcile_runtime_launch_spec_store(
    path: str | Path,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Atomically reconcile generated specs without changing process lifecycle.

    Existing project runtime revisions increase only when the generated launch
    contract changed. The whole store is written once, so a validation or write
    failure leaves the prior store available for deployment rollback.
    """

    store_path = _store_path(path)
    incoming_by_id: dict[str, dict[str, Any]] = {}
    for raw in records:
        incoming = _validate_record(raw, require_runtime_revision=False)
        project_id = incoming["project_id"]
        if project_id in incoming_by_id:
            raise RuntimeLaunchSpecStoreError(
                "duplicate_project_id", "Duplicate runtime launch spec project id"
            )
        incoming_by_id[project_id] = incoming

    with _locked(store_path, create_parent=True) as locked_path:
        store = (
            _load_store_locked(locked_path)
            if locked_path.exists()
            else empty_runtime_launch_spec_store()
        )
        reconciled = {
            "version": STORE_VERSION,
            "store_revision": store["store_revision"],
            "projects": {key: dict(value) for key, value in store["projects"].items()},
        }
        changes: list[dict[str, Any]] = []
        for project_id in sorted(incoming_by_id):
            incoming = incoming_by_id[project_id]
            existing = reconciled["projects"].get(project_id)
            if existing is None:
                incoming["runtime_revision"] = 1
                reconciled["projects"][project_id] = incoming
                changes.append(
                    {"project_id": project_id, "before_revision": None, "after_revision": 1}
                )
                continue

            current = _validate_record(existing, require_runtime_revision=True)
            comparable_fields = {
                "project_id",
                "project_path",
                "provider_id",
                "account_profile",
                "model",
                "reasoning_effort",
                "settings_revision",
                "spec_sha256",
                "source",
            }
            if all(current[field] == incoming[field] for field in comparable_fields):
                continue
            incoming["runtime_revision"] = current["runtime_revision"] + 1
            reconciled["projects"][project_id] = incoming
            changes.append(
                {
                    "project_id": project_id,
                    "before_revision": current["runtime_revision"],
                    "after_revision": incoming["runtime_revision"],
                }
            )

        if changes:
            reconciled["store_revision"] += 1
            validated = _validate_store(reconciled)
            _write_store_atomic(locked_path, validated)
        return {
            "changed": changes,
            "changed_count": len(changes),
            "configured_count": len(incoming_by_id),
            "store_revision_before": store["store_revision"],
            "store_revision_after": reconciled["store_revision"],
        }


def remove_runtime_launch_spec(
    path: str | Path,
    project_id: str,
    expected_runtime_revision: int,
) -> dict[str, Any] | None:
    store_path = _store_path(path)
    normalized = validate_project_id(project_id)
    with _locked(store_path, create_parent=True) as locked_path:
        store = (
            _load_store_locked(locked_path)
            if locked_path.exists()
            else empty_runtime_launch_spec_store()
        )
        existing = store["projects"].get(normalized)
        if existing is None:
            raise RuntimeLaunchSpecStoreError(
                "revision_conflict", "Runtime launch spec revision conflict"
            )
        current = _validate_record(existing, require_runtime_revision=True)
        if expected_runtime_revision != current["runtime_revision"]:
            raise RuntimeLaunchSpecStoreError(
                "revision_conflict", "Runtime launch spec revision conflict"
            )
        removed = store["projects"].pop(normalized)
        store["store_revision"] += 1
        _write_store_atomic(locked_path, _validate_store(store))
        return dict(removed)
