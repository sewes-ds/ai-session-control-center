"""Install the owner-approved OpenViking release into a new Codex profile.

This is a Codex profile projection, not a global hook-trust bypass.  A changed
plugin package or hook definition needs a new review and a new approved pin.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tomllib
from pathlib import Path

from ai_account_manager.core.config import AppSettings
from ai_account_manager.core.project_trust import ProjectTrustError
from ai_account_manager.providers.codex.project_trust import (
    MAX_CONFIG_BYTES,
    _private_directory,
    _read_config,
    _write_config,
)


class OpenVikingDefaultsError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_PLUGIN_ID = "openviking-memory@openviking"
_VERSION = "0.11.2"
_REVISION = "4bccb1294c013e1485c72377afcbba5ffc435cc7"
_SOURCE = "https://github.com/volcengine/OpenViking.git"
_APPROVED_TREE_SHA256 = "5c193f17a49a4ed88f29697a574e2a26a30c422df7cfabae435df2cada02a7e3"
_APPROVED_HOOKS = {
    "pre_compact:0:0": "8c255f3c4e572c0f116be8a15bb54065fc460695761c589bc5a53dc90e7d7f1e",
    "session_start:0:0": "0a64444867b60aa5d811d487f2099cde0e9c33cda4aca57fb0c0de57e7ae86db",
    "session_end:0:0": "2f0f4cfb1b2b231ff192c831178593a80752bdba9d4501a50996ccf7bb764a0f",
    "user_prompt_submit:0:0": "fc4875bbf35da63c4220388bbed28d05a379d0348fc2b95f39555ffcf9eaef59",
    "stop:0:0": "dba649f7e20596599f75faf142ad388c15d9c490873ec44717c47bb9374d8afb",
    "pre_tool_use:0:0": "c56625acacc72ae3c3e4c84e37148000e5bbdfb0c994126fcda0feca47aa8cf3",
    "stop:1:0": "49ea757d878d30ab418e9a0d86966d91c8875563700e98f06f699c428fd71a12",
    "post_tool_use:0:0": "b26c477d25f5219ecf69abe9f94b2e1eb7b09bd1f0aa47f1c2cc1e271b76b5fd",
}


def _plugin_dir(profile: Path) -> Path:
    return profile / "plugins" / "cache" / "openviking" / "openviking-memory" / _VERSION


def _tree_digest(root: Path) -> str:
    """Match a sorted `sha256sum` manifest, rejecting mutable/unsafe entries."""

    manifest = hashlib.sha256()
    files: list[tuple[str, Path]] = []
    total_bytes = 0

    def visit(directory: Path) -> None:
        nonlocal total_bytes
        metadata = directory.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise OpenVikingDefaultsError("openviking_package_unsafe")
        for child in sorted(directory.iterdir(), key=lambda entry: entry.name):
            child_metadata = child.lstat()
            if stat.S_ISDIR(child_metadata.st_mode):
                visit(child)
            elif (
                stat.S_ISREG(child_metadata.st_mode)
                and child_metadata.st_uid == os.geteuid()
                and not stat.S_IMODE(child_metadata.st_mode) & 0o022
            ):
                total_bytes += child_metadata.st_size
                files.append((child.relative_to(root).as_posix(), child))
                if len(files) > 256 or total_bytes > 16 * 1024 * 1024:
                    raise OpenVikingDefaultsError("openviking_package_too_large")
            else:
                raise OpenVikingDefaultsError("openviking_package_unsafe")

    try:
        visit(root)
        for relative, path in sorted(files):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest.update(f"{digest}  ./{relative}\n".encode("utf-8"))
    except OSError as exc:
        raise OpenVikingDefaultsError("openviking_package_unavailable") from exc
    return manifest.hexdigest()


def _approved_config(raw: bytes) -> bytes:
    try:
        parsed = tomllib.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise OpenVikingDefaultsError("openviking_config_invalid") from exc
    sections: list[str] = []
    features = parsed.get("features")
    if features is None:
        sections.append("[features]\nhooks = true\nplugin_hooks = true\n")
    elif isinstance(features, dict):
        if any(key in features and features[key] is not True for key in ("hooks", "plugin_hooks")):
            raise OpenVikingDefaultsError("openviking_config_conflict")
        missing = [key for key in ("hooks", "plugin_hooks") if key not in features]
        if missing:
            match = re.search(rb"(?m)^\[features\][ \t]*(?:#[^\r\n]*)?\r?$", raw)
            if match is None:
                raise OpenVikingDefaultsError("openviking_config_conflict")
            insertion = b"".join(f"\n{key} = true".encode() for key in missing)
            raw = raw[: match.end()] + insertion + raw[match.end() :]
    else:
        raise OpenVikingDefaultsError("openviking_config_conflict")

    marketplaces = parsed.get("marketplaces", {})
    plugins = parsed.get("plugins", {})
    hooks = parsed.get("hooks", {})
    if not all(isinstance(section, dict) for section in (marketplaces, plugins, hooks)):
        raise OpenVikingDefaultsError("openviking_config_conflict")
    marketplace = marketplaces.get("openviking")
    if marketplace is None:
        sections.append(
            "[marketplaces.openviking]\n"
            f'last_revision = "{_REVISION}"\n'
            'source_type = "git"\n'
            f'source = "{_SOURCE}"\n'
        )
    elif not isinstance(marketplace, dict) or any(
        marketplace.get(key) != value
        for key, value in (
            ("last_revision", _REVISION),
            ("source_type", "git"),
            ("source", _SOURCE),
        )
    ):
        raise OpenVikingDefaultsError("openviking_config_conflict")

    plugin = plugins.get(_PLUGIN_ID)
    if plugin is None:
        sections.append(f'[plugins."{_PLUGIN_ID}"]\nenabled = true\n')
    elif not isinstance(plugin, dict) or plugin.get("enabled") is not True:
        raise OpenVikingDefaultsError("openviking_config_conflict")

    hook_state = hooks.get("state", {})
    if not isinstance(hook_state, dict):
        raise OpenVikingDefaultsError("openviking_config_conflict")
    for suffix, digest in _APPROVED_HOOKS.items():
        key = f"{_PLUGIN_ID}:hooks/hooks.json:{suffix}"
        existing = hook_state.get(key)
        if existing is None:
            sections.append(f'[hooks.state."{key}"]\ntrusted_hash = "sha256:{digest}"\n')
        elif not isinstance(existing, dict) or existing.get("trusted_hash") != f"sha256:{digest}":
            raise OpenVikingDefaultsError("openviking_config_conflict")
    if sections:
        raw = raw.rstrip(b"\n") + (b"\n\n" if raw else b"") + "\n".join(sections).encode()
    if len(raw) > MAX_CONFIG_BYTES:
        raise OpenVikingDefaultsError("openviking_config_too_large")
    try:
        tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise OpenVikingDefaultsError("openviking_config_invalid") from exc
    return raw


def provision_approved_openviking(profile: Path, *, settings: AppSettings) -> None:
    """Fail closed if the configured source is missing or differs from the reviewed pin."""

    seed = settings.codex_openviking_seed_account
    if not seed:
        return
    source_profile = settings.codex_profiles_root / seed
    try:
        _private_directory(source_profile, code="openviking_source_unsafe")
        _private_directory(profile, code="openviking_target_unsafe")
        config_path = profile / "config.toml"
        before = _read_config(config_path)
        after = _approved_config(before)
        source_package = _plugin_dir(source_profile)
        if _tree_digest(source_package) != _APPROVED_TREE_SHA256:
            raise OpenVikingDefaultsError("openviking_package_changed")
        target_package = _plugin_dir(profile)
        if target_package.exists() or target_package.is_symlink():
            raise OpenVikingDefaultsError("openviking_target_conflict")
        target_package.parent.mkdir(parents=True, mode=0o700)
        shutil.copytree(source_package, target_package, symlinks=False)
        if _tree_digest(target_package) != _APPROVED_TREE_SHA256:
            raise OpenVikingDefaultsError("openviking_package_copy_changed")
        _write_config(config_path, before, after)
    except ProjectTrustError as exc:
        raise OpenVikingDefaultsError(exc.code) from exc
    except OSError as exc:
        raise OpenVikingDefaultsError("openviking_provision_failed") from exc
