from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tomllib
from pathlib import Path

from ai_account_manager.providers.codex.upgrade_operational import (
    OperationalError,
    assert_private_directory,
    atomic_write_bytes,
    atomic_write_model,
    canonical_path,
)


MAX_CONFIG_BYTES = 1024 * 1024
UPDATE_KEY = "check_for_update_on_startup"
_ASSIGNMENT = re.compile(r"^\s*check_for_update_on_startup\s*=")
_TABLE = re.compile(r"^\s*\[")


def _read_config(path: Path) -> bytes:
    if path.is_symlink():
        raise OperationalError("MANAGED_PROFILE_CONFIG_UNSAFE")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return b""
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o177
        or metadata.st_size > MAX_CONFIG_BYTES
    ):
        raise OperationalError("MANAGED_PROFILE_CONFIG_UNSAFE")
    return path.read_bytes()


def _reconciled_config(raw: bytes) -> bytes:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OperationalError("MANAGED_PROFILE_CONFIG_INVALID") from exc
    lines = text.splitlines(keepends=True)
    assignment_indexes: list[int] = []
    first_table = len(lines)
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if _TABLE.match(line) and first_table == len(lines):
            first_table = index
        if _ASSIGNMENT.match(line):
            assignment_indexes.append(index)
    if len(assignment_indexes) > 1 or (
        assignment_indexes and assignment_indexes[0] >= first_table
    ):
        raise OperationalError("MANAGED_PROFILE_CONFIG_AMBIGUOUS")
    desired = f"{UPDATE_KEY} = false\n"
    if assignment_indexes:
        lines[assignment_indexes[0]] = desired
    else:
        lines.insert(0, desired)
    return "".join(lines).encode("utf-8")


def reconcile_disposable_project_trust(profile: Path, project: Path) -> None:
    directory = assert_private_directory(profile, create=True, code="DEDICATED_PROFILE_UNSAFE")
    cwd = canonical_path(project, code="CANARY_CWD_UNSAFE")
    config = directory / "config.toml"
    raw = _read_config(config)
    try:
        parsed = tomllib.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise OperationalError("CANARY_PROJECT_TRUST_CONFIG_INVALID") from exc
    projects = parsed.get("projects", {})
    if not isinstance(projects, dict):
        raise OperationalError("CANARY_PROJECT_TRUST_CONFIG_INVALID")
    existing = projects.get(str(cwd))
    if existing is not None:
        if isinstance(existing, dict) and existing.get("trust_level") == "trusted":
            return
        raise OperationalError("CANARY_PROJECT_TRUST_CONFLICT")
    prefix = raw
    if prefix and not prefix.endswith(b"\n"):
        prefix += b"\n"
    if prefix and not prefix.endswith(b"\n\n"):
        prefix += b"\n"
    section = f'[projects.{json.dumps(str(cwd))}]\ntrust_level = "trusted"\n'.encode()
    atomic_write_bytes(config, prefix + section)


def reconcile_managed_profiles(profiles_root: Path, evidence_path: Path) -> dict[str, object]:
    root = assert_private_directory(
        profiles_root, create=False, code="MANAGED_PROFILES_ROOT_UNSAFE"
    )
    results: list[dict[str, object]] = []
    for profile in sorted(root.iterdir(), key=lambda item: item.name):
        if not profile.is_dir() or profile.is_symlink():
            continue
        directory = assert_private_directory(
            profile, create=False, code="MANAGED_PROFILE_UNSAFE"
        )
        config = directory / "config.toml"
        before = _read_config(config)
        after = _reconciled_config(before)
        changed = before != after
        if changed:
            atomic_write_bytes(config, after)
        results.append(
            {
                "profile": profile.name,
                "changed": changed,
                "config_sha256": hashlib.sha256(after).hexdigest(),
                "check_for_update_on_startup": False,
            }
        )
    evidence = {
        "schema_version": 1,
        "profiles_root": str(root),
        "profile_count": len(results),
        "profiles": results,
        "auth_files_accessed": False,
    }
    atomic_write_model(evidence_path, evidence)
    return evidence


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    try:
        reconcile_managed_profiles(Path(sys.argv[1]), Path(sys.argv[2]))
    except OperationalError:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
