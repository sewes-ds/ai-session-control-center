from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ai_account_manager.schemas.codex import CodexAccountProfile, CodexProfileDiscoveryResponse

_SAFE_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}$")


def _profiles_root() -> Path:
    default = Path.home() / ".local" / "share" / "ai-profiles"
    return Path(os.getenv("AI_PROFILES_ROOT", str(default))).expanduser()


def _safe_label_from_profile_json(profile_json_path: Path, fallback: str) -> str:
    if not profile_json_path.is_file():
        return fallback
    try:
        data: Any = json.loads(profile_json_path.read_text(encoding="utf-8"))
    except Exception:
        return fallback
    if not isinstance(data, dict):
        return fallback
    label = data.get("label") or data.get("display_name") or data.get("name")
    if isinstance(label, str) and label.strip():
        return label.strip()[:120]
    return fallback


def _profile_status(slug: str, has_auth_json: bool, has_config_toml: bool) -> str:
    if not _SAFE_SLUG_RE.fullmatch(slug):
        return "invalid_slug"
    if not has_auth_json:
        return "missing_auth"
    return "readable"


def discover_codex_profiles() -> CodexProfileDiscoveryResponse:
    """Read-only discovery of local Codex profile directories.

    This function intentionally checks only file presence and optional non-secret
    profile metadata. It must not read auth.json contents, refresh tokens, access
    tokens, cookies, or provider secrets.
    """
    profiles_root = _profiles_root()
    codex_root = profiles_root / "codex"

    accounts: list[CodexAccountProfile] = []
    if codex_root.is_dir():
        for profile_dir in sorted(
            [p for p in codex_root.iterdir() if p.is_dir()], key=lambda p: p.name
        ):
            slug = profile_dir.name
            auth_json = profile_dir / "auth.json"
            config_toml = profile_dir / "config.toml"
            profile_json = profile_dir / "profile.json"
            has_auth_json = auth_json.is_file()
            has_config_toml = config_toml.is_file()
            accounts.append(
                CodexAccountProfile(
                    account_slug=slug,
                    label=_safe_label_from_profile_json(profile_json, slug),
                    profile_path=str(profile_dir),
                    profile_path_exists=profile_dir.is_dir(),
                    has_auth_json=has_auth_json,
                    has_config_toml=has_config_toml,
                    has_profile_json=profile_json.is_file(),
                    status=_profile_status(slug, has_auth_json, has_config_toml),
                )
            )

    return CodexProfileDiscoveryResponse(
        profiles_root=str(profiles_root),
        codex_profiles_root=str(codex_root),
        root_exists=codex_root.is_dir(),
        account_count=len(accounts),
        accounts=accounts,
    )
