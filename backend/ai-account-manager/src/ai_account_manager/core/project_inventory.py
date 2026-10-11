from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field, ValidationError

from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.core.project_settings import project_id_for_path

STORE_VERSION = 1
_MAX_STORE_BYTES = 1024 * 1024
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_INVENTORY_ID_RE = re.compile(r"^(project|topic|config)_[0-9a-f]{20}$")
_TOPIC_CONFIG_KEY_RE = re.compile(r"^[0-9]{1,20}$")


class ProjectInventoryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class ProjectInventoryRecord(BaseModel):
    project_id: str
    identity_kind: str = "project_path"
    project_path: str | None = None
    chat_id: int | None = None
    thread_id: int | None = None
    topic_config_key: str | None = None
    archived: bool = False
    revision: int = Field(ge=1)
    reason: str | None = None
    updated_at: str


def inventory_id_for_topic(chat_id: int, thread_id: int) -> str:
    digest = hashlib.sha256(f"telegram_topic:{chat_id}:{thread_id}".encode("utf-8")).hexdigest()[:20]
    return f"topic_{digest}"


def _canonical_topic_config_path(settings: AppSettings) -> str:
    return str(settings.telegram_ai_agent_topic_config_path.expanduser().resolve(strict=False))


def validate_topic_config_key(raw_topic_key: str) -> str:
    if not _TOPIC_CONFIG_KEY_RE.fullmatch(raw_topic_key):
        raise ProjectInventoryError("invalid_topic_config_key", "Project inventory topic config key is invalid")
    return raw_topic_key


def inventory_id_for_config_topic(config_path: str | Path, raw_topic_key: str) -> str:
    canonical_config = str(Path(config_path).expanduser().resolve(strict=False))
    key = validate_topic_config_key(raw_topic_key)
    digest = hashlib.sha256(
        f"topic_config_entry:{canonical_config}:{key}".encode("utf-8")
    ).hexdigest()[:20]
    return f"config_{digest}"


def validate_inventory_id(project_id: str) -> str:
    value = project_id.strip()
    if not _INVENTORY_ID_RE.fullmatch(value):
        raise ProjectInventoryError("invalid_project_id", "Invalid project inventory id")
    return value


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _utc_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _inventory_path(settings: AppSettings) -> Path:
    path = settings.project_inventory_path.expanduser()
    if not path.is_absolute() or path.is_symlink() or path.parent.is_symlink():
        raise ProjectInventoryError("invalid_inventory_path", "Project inventory path is invalid")
    return path.resolve(strict=False)


def validate_inventory_project_path(
    project_path: str,
    settings: AppSettings | None = None,
    *,
    require_allowed_root: bool = True,
) -> str:
    settings = settings or get_settings()
    raw = Path(project_path).expanduser()
    if not raw.is_absolute():
        raise ProjectInventoryError("project_path_not_absolute", "Project path must be absolute")
    resolved = raw.resolve(strict=False)
    if require_allowed_root:
        roots = [root.expanduser().resolve(strict=False) for root in settings.ai_project_roots]
        if not any(resolved == root or _is_relative_to(resolved, root) for root in roots):
            raise ProjectInventoryError("project_outside_allowed_roots", "Project path is outside AI_PROJECT_ROOTS")
    return str(resolved)


def _bounded_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    text = re.sub(r"\s+", " ", reason).strip()
    if not text:
        return None
    if _CONTROL_RE.search(text):
        raise ProjectInventoryError("invalid_reason", "Project inventory reason is invalid")
    return text[:240]


@contextmanager
def _locked(path: Path) -> Iterator[Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    if lock_path.is_symlink():
        raise ProjectInventoryError("invalid_inventory_path", "Project inventory lock path is invalid")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _empty_store() -> dict[str, Any]:
    return {"version": STORE_VERSION, "store_revision": 0, "projects": {}}


def _load_store(path: Path, settings: AppSettings) -> dict[str, Any]:
    if not path.exists():
        return _empty_store()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_STORE_BYTES:
        raise ProjectInventoryError("inventory_store_invalid", "Project inventory store is invalid")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectInventoryError("inventory_store_invalid", "Project inventory store is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != STORE_VERSION
        or isinstance(payload.get("store_revision"), bool)
        or not isinstance(payload.get("store_revision"), int)
        or payload.get("store_revision") < 0
        or not isinstance(payload.get("projects"), dict)
    ):
        raise ProjectInventoryError("inventory_store_invalid", "Project inventory store is invalid")
    max_record_revision = 0
    for project_id, raw in payload["projects"].items():
        try:
            validate_inventory_id(project_id)
            record = ProjectInventoryRecord.model_validate(raw)
            if record.project_id != project_id:
                raise ValueError
            if record.identity_kind not in {"project_path", "telegram_topic", "topic_config_entry"}:
                raise ValueError
            if record.identity_kind == "project_path":
                if (
                    not record.project_path
                    or record.chat_id is not None
                    or record.thread_id is not None
                    or record.topic_config_key is not None
                ):
                    raise ValueError
            if record.identity_kind == "telegram_topic":
                if (
                    record.project_path is not None
                    or record.chat_id is None
                    or record.thread_id is None
                    or record.topic_config_key is not None
                ):
                    raise ValueError
                if record.project_id != inventory_id_for_topic(record.chat_id, record.thread_id):
                    raise ValueError
            if record.identity_kind == "topic_config_entry":
                if (
                    record.project_path is not None
                    or record.chat_id is not None
                    or record.thread_id is not None
                    or record.topic_config_key is None
                ):
                    raise ValueError
                if record.project_id != inventory_id_for_config_topic(
                    _canonical_topic_config_path(settings),
                    record.topic_config_key,
                ):
                    raise ValueError
            max_record_revision = max(max_record_revision, record.revision)
        except (ValidationError, ValueError, ProjectInventoryError) as exc:
            raise ProjectInventoryError("inventory_store_invalid", "Project inventory store is invalid") from exc
    if int(payload["store_revision"]) < max_record_revision:
        raise ProjectInventoryError("inventory_store_invalid", "Project inventory store is invalid")
    return payload


def _write_store(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    parent_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def read_project_inventory(settings: AppSettings | None = None) -> dict[str, ProjectInventoryRecord]:
    settings = settings or get_settings()
    path = _inventory_path(settings)
    with _locked(path):
        store = _load_store(path, settings)
    return {
        key: ProjectInventoryRecord.model_validate(value)
        for key, value in store["projects"].items()
    }


def update_project_inventory_record(
    *,
    project_id: str,
    project_path: str,
    archived: bool,
    expected_revision: int | None,
    reason: str | None = None,
    settings: AppSettings | None = None,
    require_allowed_root: bool = True,
) -> ProjectInventoryRecord:
    settings = settings or get_settings()
    normalized_id = validate_inventory_id(project_id)
    canonical = validate_inventory_project_path(
        project_path,
        settings,
        require_allowed_root=require_allowed_root,
    )
    expected_id = project_id_for_path(canonical)
    if normalized_id != expected_id:
        raise ProjectInventoryError("project_id_mismatch", "Project inventory id does not match path")
    path = _inventory_path(settings)
    with _locked(path):
        store = _load_store(path, settings)
        existing_raw = store["projects"].get(normalized_id)
        existing = (
            ProjectInventoryRecord.model_validate(existing_raw)
            if isinstance(existing_raw, dict)
            else None
        )
        if existing is None:
            if expected_revision is not None:
                raise ProjectInventoryError("revision_conflict", "Project inventory revision conflict")
            revision = 1
        else:
            if expected_revision != existing.revision:
                raise ProjectInventoryError("revision_conflict", "Project inventory revision conflict")
            revision = existing.revision + 1
        record = ProjectInventoryRecord(
            project_id=normalized_id,
            identity_kind="project_path",
            project_path=canonical,
            archived=archived,
            revision=revision,
            reason=_bounded_reason(reason),
            updated_at=_utc_iso(),
        )
        store["projects"][normalized_id] = record.model_dump()
        store["store_revision"] = int(store["store_revision"]) + 1
        _write_store(path, store)
        return record


def update_topic_inventory_record(
    *,
    project_id: str,
    chat_id: int,
    thread_id: int,
    archived: bool,
    expected_revision: int | None,
    reason: str | None = None,
    settings: AppSettings | None = None,
) -> ProjectInventoryRecord:
    settings = settings or get_settings()
    normalized_id = validate_inventory_id(project_id)
    expected_id = inventory_id_for_topic(chat_id, thread_id)
    if normalized_id != expected_id:
        raise ProjectInventoryError("project_id_mismatch", "Project inventory id does not match topic")
    path = _inventory_path(settings)
    with _locked(path):
        store = _load_store(path, settings)
        existing_raw = store["projects"].get(normalized_id)
        existing = (
            ProjectInventoryRecord.model_validate(existing_raw)
            if isinstance(existing_raw, dict)
            else None
        )
        if existing is None:
            if expected_revision is not None:
                raise ProjectInventoryError("revision_conflict", "Project inventory revision conflict")
            revision = 1
        else:
            if existing.identity_kind != "telegram_topic" or existing.chat_id != chat_id or existing.thread_id != thread_id:
                raise ProjectInventoryError("project_id_mismatch", "Project inventory id does not match topic")
            if expected_revision != existing.revision:
                raise ProjectInventoryError("revision_conflict", "Project inventory revision conflict")
            revision = existing.revision + 1
        record = ProjectInventoryRecord(
            project_id=normalized_id,
            identity_kind="telegram_topic",
            project_path=None,
            chat_id=chat_id,
            thread_id=thread_id,
            archived=archived,
            revision=revision,
            reason=_bounded_reason(reason),
            updated_at=_utc_iso(),
        )
        store["projects"][normalized_id] = record.model_dump()
        store["store_revision"] = int(store["store_revision"]) + 1
        _write_store(path, store)
        return record


def update_config_topic_inventory_record(
    *,
    project_id: str,
    topic_config_key: str,
    archived: bool,
    expected_revision: int | None,
    reason: str | None = None,
    settings: AppSettings | None = None,
) -> ProjectInventoryRecord:
    settings = settings or get_settings()
    normalized_id = validate_inventory_id(project_id)
    key = validate_topic_config_key(topic_config_key)
    expected_id = inventory_id_for_config_topic(_canonical_topic_config_path(settings), key)
    if normalized_id != expected_id:
        raise ProjectInventoryError("project_id_mismatch", "Project inventory id does not match topic config entry")
    path = _inventory_path(settings)
    with _locked(path):
        store = _load_store(path, settings)
        existing_raw = store["projects"].get(normalized_id)
        existing = (
            ProjectInventoryRecord.model_validate(existing_raw)
            if isinstance(existing_raw, dict)
            else None
        )
        if existing is None:
            if expected_revision is not None:
                raise ProjectInventoryError("revision_conflict", "Project inventory revision conflict")
            revision = 1
        else:
            if existing.identity_kind != "topic_config_entry" or existing.topic_config_key != key:
                raise ProjectInventoryError("project_id_mismatch", "Project inventory id does not match topic config entry")
            if expected_revision != existing.revision:
                raise ProjectInventoryError("revision_conflict", "Project inventory revision conflict")
            revision = existing.revision + 1
        record = ProjectInventoryRecord(
            project_id=normalized_id,
            identity_kind="topic_config_entry",
            project_path=None,
            chat_id=None,
            thread_id=None,
            topic_config_key=key,
            archived=archived,
            revision=revision,
            reason=_bounded_reason(reason),
            updated_at=_utc_iso(),
        )
        store["projects"][normalized_id] = record.model_dump()
        store["store_revision"] = int(store["store_revision"]) + 1
        _write_store(path, store)
        return record
