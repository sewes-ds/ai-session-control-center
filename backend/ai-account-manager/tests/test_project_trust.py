from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from ai_account_manager.core.config import AppSettings
from ai_account_manager.core.project_settings import upsert_project_settings
from ai_account_manager.core.project_trust import ProjectTrustError, reconcile_project_trust


def _settings(tmp_path: Path) -> AppSettings:
    runtime = tmp_path / "runtime"
    bot_root = tmp_path / "telegram-ai-agent"
    return AppSettings(
        app_env="test",
        app_host="127.0.0.1",
        app_port=8710,
        ai_profiles_root=tmp_path / "profiles",
        ai_quota_cache_root=tmp_path / "quota-cache",
        runtime_data_root=runtime,
        project_bindings_path=runtime / "project-bindings.json",
        telegram_ai_agent_root=bot_root,
        telegram_ai_agent_topic_config_path=bot_root / "topic-config.json",
        telegram_ai_agent_tmux_state_path=bot_root / "tmux-state.json",
        telegram_ai_agent_compatibility_manifest=tmp_path / "compatibility.json",
        codex_usage_endpoint="https://chatgpt.com/backend-api/wham/usage",
        codex_binary=tmp_path / "bin" / "codex",
        codex_auth_enrollment_root=runtime / "codex-auth-enrollments",
        codex_auth_enrollment_ttl_seconds=900,
        codex_auth_stale_after_seconds=691200,
        enable_provider_codex=True,
        enable_provider_claude_code=False,
        enable_provider_gemini_cli=False,
        ai_project_roots=(tmp_path / "projects",),
        project_settings_path=runtime / "project-settings-v2.json",
        runtime_launch_specs_path=runtime / "runtime-launch-specs-v1.json",
    )


def _project(settings: AppSettings, name: str = "alpha") -> Path:
    path = settings.ai_project_roots[0] / name
    path.mkdir(parents=True)
    return path.resolve()


def _profile(settings: AppSettings, slug: str, config: str = 'model = "gpt-5"\n') -> Path:
    profile = settings.codex_profiles_root / slug
    profile.mkdir(parents=True)
    settings.codex_profiles_root.chmod(0o700)
    profile.chmod(0o700)
    config_path = profile / "config.toml"
    config_path.write_text(config, encoding="utf-8")
    config_path.chmod(0o600)
    return profile


def _trust_level(profile: Path, project: Path) -> str:
    parsed = tomllib.loads((profile / "config.toml").read_text(encoding="utf-8"))
    return parsed["projects"][str(project)]["trust_level"]


def test_project_trust_is_materialized_into_every_codex_profile(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    project = _project(settings)
    first = _profile(settings, "codex_a")
    second = _profile(settings, "codex_b")

    result = reconcile_project_trust(
        provider_id="codex",
        project_paths=(project,),
        settings=settings,
    )

    assert result.changed_profiles == ("codex_a", "codex_b")
    assert _trust_level(first, project) == "trusted"
    assert _trust_level(second, project) == "trusted"
    repeated = reconcile_project_trust(
        provider_id="codex",
        project_paths=(project,),
        settings=settings,
    )
    assert repeated.changed_profiles == ()


def test_conflict_is_detected_before_any_profile_is_changed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    project = _project(settings)
    first = _profile(settings, "codex_a")
    second = _profile(
        settings,
        "codex_b",
        f'[projects."{project}"]\ntrust_level = "untrusted"\n',
    )
    first_before = (first / "config.toml").read_bytes()
    second_before = (second / "config.toml").read_bytes()

    with pytest.raises(ProjectTrustError) as exc_info:
        reconcile_project_trust(
            provider_id="codex",
            project_paths=(project,),
            settings=settings,
        )

    assert exc_info.value.code == "project_trust_conflict"
    assert (first / "config.toml").read_bytes() == first_before
    assert (second / "config.toml").read_bytes() == second_before


def test_project_settings_are_the_source_of_shared_trust(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    project = _project(settings)
    first = _profile(settings, "codex_a")
    second = _profile(settings, "codex_b")

    upsert_project_settings(
        project_path=str(project),
        provider_id="codex",
        account_profile="codex_a",
        model="gpt-5.6-sol",
        reasoning_effort="high",
        expected_revision=None,
        settings=settings,
    )

    assert _trust_level(first, project) == "trusted"
    assert _trust_level(second, project) == "trusted"
