from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel


MAX_JSON_BYTES = 256 * 1024
SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,159}$")


class ProfileFileError(RuntimeError):
    def __init__(self, code: str) -> None:
        normalized = code if SAFE_CODE.fullmatch(code) else "PROFILE_FILE_OPERATION_FAILED"
        super().__init__(normalized)
        self.code = normalized


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def assert_private_directory(path: Path, *, create: bool, code: str) -> Path:
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise ProfileFileError(code)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ProfileFileError(code)
    if not path.exists():
        if not create:
            raise ProfileFileError(code)
        path.mkdir(mode=0o700, parents=True)
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ProfileFileError(code)
    return path.resolve(strict=True)


def canonical_path(
    path: Path,
    *,
    code: str,
    must_exist: bool = True,
    allowed_root: Path | None = None,
) -> Path:
    if not path.is_absolute() or ".." in path.parts or path == Path("/") or path.is_symlink():
        raise ProfileFileError(code)
    try:
        resolved = path.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise ProfileFileError(code) from exc
    if str(path) != str(resolved):
        raise ProfileFileError(code)
    if allowed_root is not None:
        root = allowed_root.resolve(strict=True)
        if resolved != root and not _is_beneath(resolved, root):
            raise ProfileFileError(code)
    return resolved


def _canonical_json(value: BaseModel | dict[str, Any] | list[Any]) -> bytes:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def atomic_write_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise ProfileFileError("RESULT_PATH_UNSAFE")
    parent = assert_private_directory(path.parent, create=True, code="RESULT_PATH_UNSAFE")
    if path.is_symlink():
        raise ProfileFileError("RESULT_PATH_UNSAFE")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_model(path: Path, value: BaseModel | dict[str, Any] | list[Any]) -> None:
    payload = _canonical_json(value)
    if len(payload) > MAX_JSON_BYTES:
        raise ProfileFileError("RESULT_TOO_LARGE")
    atomic_write_bytes(path, payload)
