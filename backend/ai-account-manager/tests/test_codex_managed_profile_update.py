from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_account_manager.providers.codex.managed_profile_update import (
    reconcile_managed_profiles,
)
from ai_account_manager.providers.codex.upgrade_operational import OperationalError


def test_managed_profile_update_setting_is_idempotent_without_auth_access(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profile = profiles / "codex_a"
    profile.mkdir(mode=0o700, parents=True)
    profiles.chmod(0o700)
    config = profile / "config.toml"
    config.write_text('model = "gpt-5.6-sol"\n[features]\nweb_search = true\n')
    config.chmod(0o600)
    auth = profile / "auth.json"
    auth.write_bytes(b"sentinel-credential-content")
    auth.chmod(0o600)
    evidence = tmp_path / "evidence" / "managed-profiles.json"

    first = reconcile_managed_profiles(profiles, evidence)
    second = reconcile_managed_profiles(profiles, evidence)

    assert config.read_text().splitlines()[0] == "check_for_update_on_startup = false"
    assert config.read_text().count("check_for_update_on_startup") == 1
    assert auth.read_bytes() == b"sentinel-credential-content"
    assert first["profiles"][0]["changed"] is True
    assert second["profiles"][0]["changed"] is False
    assert json.loads(evidence.read_text())["auth_files_accessed"] is False


def test_managed_profile_rejects_setting_hidden_inside_toml_table(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profile = profiles / "codex_a"
    profile.mkdir(mode=0o700, parents=True)
    profiles.chmod(0o700)
    config = profile / "config.toml"
    config.write_text("[features]\ncheck_for_update_on_startup = true\n")
    config.chmod(0o600)

    with pytest.raises(OperationalError, match="MANAGED_PROFILE_CONFIG_AMBIGUOUS"):
        reconcile_managed_profiles(profiles, tmp_path / "evidence.json")
