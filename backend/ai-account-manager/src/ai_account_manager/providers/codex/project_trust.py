from __future__ import annotations

import json
import os
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ai_account_manager.core.config import AppSettings
from ai_account_manager.core.project_trust import ProjectTrustError, ProjectTrustSyncResult


MAX_CONFIG_BYTES = 1024 * 1024


@dataclass(frozen=True)
class _ConfigPlan:
    profile: Path
    before: bytes
    after: bytes


def _private_directory(path: Path, *, code: str) -> Path:
    if path.is_symlink():
        raise ProjectTrustError(code, "Codex profile directory is unsafe")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProjectTrustError(code, "Codex profile directory is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ProjectTrustError(code, "Codex profile directory is unsafe")
    return path.resolve(strict=True)


def _read_config(path: Path) -> bytes:
    if path.is_symlink():
        raise ProjectTrustError("project_trust_config_unsafe", "Codex config is unsafe")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return b""
    except OSError as exc:
        raise ProjectTrustError(
            "project_trust_config_unsafe", "Codex config is unavailable"
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or metadata.st_size > MAX_CONFIG_BYTES
    ):
        raise ProjectTrustError("project_trust_config_unsafe", "Codex config is unsafe")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ProjectTrustError(
            "project_trust_config_unsafe", "Codex config is unavailable"
        ) from exc


def _canonical_paths(project_paths: tuple[Path, ...]) -> tuple[Path, ...]:
    canonical: set[Path] = set()
    for path in project_paths:
        if not path.is_absolute() or ".." in path.parts or path == Path("/"):
            raise ProjectTrustError("project_trust_path_invalid", "Project path is invalid")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ProjectTrustError(
                "project_trust_path_invalid", "Project path is unavailable"
            ) from exc
        if not resolved.is_dir() or resolved != path:
            raise ProjectTrustError("project_trust_path_invalid", "Project identity changed")
        canonical.add(resolved)
    return tuple(sorted(canonical, key=str))


def _render_config(raw: bytes, projects: tuple[Path, ...]) -> bytes:
    try:
        parsed = tomllib.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProjectTrustError("project_trust_config_invalid", "Codex config is invalid") from exc
    configured = parsed.get("projects", {})
    if not isinstance(configured, dict):
        raise ProjectTrustError("project_trust_config_invalid", "Codex config is invalid")
    missing: list[Path] = []
    for project in projects:
        existing = configured.get(str(project))
        if existing is None:
            missing.append(project)
            continue
        if not isinstance(existing, dict) or existing.get("trust_level") != "trusted":
            raise ProjectTrustError(
                "project_trust_conflict",
                "A Codex profile explicitly conflicts with the CUS project trust",
            )
    if not missing:
        return raw
    result = raw
    if result and not result.endswith(b"\n"):
        result += b"\n"
    if result and not result.endswith(b"\n\n"):
        result += b"\n"
    for index, project in enumerate(missing):
        if index:
            result += b"\n"
        result += f'[projects.{json.dumps(str(project))}]\ntrust_level = "trusted"\n'.encode(
            "utf-8"
        )
    if len(result) > MAX_CONFIG_BYTES:
        raise ProjectTrustError("project_trust_config_too_large", "Codex config is too large")
    return result


def _write_config(path: Path, before: bytes, after: bytes) -> None:
    if before == after:
        return
    if _read_config(path) != before:
        raise ProjectTrustError(
            "project_trust_config_changed", "Codex config changed during synchronization"
        )
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.cus-trust-", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(after)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise ProjectTrustError("project_trust_config_unsafe", "Codex config is unsafe")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _profiles(
    settings: AppSettings,
    profile_path: Path | None,
) -> tuple[Path, ...]:
    if profile_path is not None:
        return (_private_directory(profile_path, code="project_trust_profile_unsafe"),)
    root = settings.codex_profiles_root.expanduser()
    if not root.exists():
        return ()
    root = _private_directory(root, code="project_trust_profiles_root_unsafe")
    profiles: list[Path] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child.is_symlink() or not child.is_dir():
            continue
        profiles.append(_private_directory(child, code="project_trust_profile_unsafe"))
    return tuple(profiles)


def reconcile_codex_project_trust(
    project_paths: tuple[Path, ...],
    *,
    settings: AppSettings,
    profile_path: Path | None = None,
) -> ProjectTrustSyncResult:
    """Materialize one CUS project trust set into Codex profile configs.

    Every config is validated before the first write. Auth files are never read.
    Existing explicit conflicts fail closed instead of silently widening trust.
    """

    projects = _canonical_paths(project_paths)
    profiles = _profiles(settings, profile_path)
    plans: list[_ConfigPlan] = []
    for profile in profiles:
        config = profile / "config.toml"
        before = _read_config(config)
        plans.append(
            _ConfigPlan(profile=profile, before=before, after=_render_config(before, projects))
        )
    for plan in plans:
        _write_config(plan.profile / "config.toml", plan.before, plan.after)
    return ProjectTrustSyncResult(
        provider_id="codex",
        project_count=len(projects),
        profile_count=len(profiles),
        changed_profiles=tuple(plan.profile.name for plan in plans if plan.before != plan.after),
    )
