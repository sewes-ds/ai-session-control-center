from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from ai_account_manager.core.config import AppSettings
from ai_account_manager.core.project_trust import ProjectTrustError, reconcile_project_trust
from ai_account_manager.core.runtime_control import register_agent_instance, upsert_runtime_binding
from ai_account_manager.schemas.runtime_control import (
    AgentInstanceRecord,
    DesiredSpecRecord,
    RuntimeBindingRecord,
    canonical_digest,
)


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
        telegram_ai_agent_root=bot_root,
        telegram_ai_agent_topic_config_path=bot_root / "topic-config.json",
        telegram_ai_agent_tmux_state_path=bot_root / "tmux-state.json",
        codex_usage_endpoint="https://chatgpt.com/backend-api/wham/usage",
        codex_binary=tmp_path / "bin" / "codex",
        codex_auth_enrollment_root=runtime / "codex-auth-enrollments",
        codex_auth_enrollment_ttl_seconds=900,
        codex_auth_stale_after_seconds=691200,
        enable_provider_codex=True,
        enable_provider_claude_code=False,
        enable_provider_gemini_cli=False,
        ai_project_roots=(tmp_path / "projects",),
        runtime_control_path=runtime / "runtime-control-v1.json",
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


def test_runtime_control_is_the_source_of_shared_trust(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    project = _project(settings)
    first = _profile(settings, "codex_a")
    second = _profile(settings, "codex_b")

    agent = AgentInstanceRecord(
        agent_instance_id="agent-1",
        pinned_upstream_sha="0" * 40,
        telegram_bot_identity="bot-agent-1",
        telegram_bot_token_fingerprint="sha256:" + "1" * 64,
        forum_chat_id=-1001,
        runtime_socket="/run/user/1000/ai-agent-runtime/agent-1/runtime.sock",
    )
    register_agent_instance(settings.runtime_control_path, agent)
    material: dict[str, object] = {
        "binding_id": "binding-1",
        "agent_instance_id": "agent-1",
        "generation_id": "generation-1",
        "revision": 1,
        "project_id": "project-alpha",
        "canonical_cwd": str(project),
        "provider": "codex",
        "account_profile": "codex_a",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
    }
    desired = DesiredSpecRecord(
        **material,
        spec_digest=canonical_digest(material),
        source_kind="cus_ui",
        source_ref="test",
    )
    upsert_runtime_binding(
        settings.runtime_control_path,
        RuntimeBindingRecord(
            binding_id="binding-1",
            agent_instance_id="agent-1",
            chat_id=-1001,
            thread_id=42,
            topic_name="Alpha",
            project_id="project-alpha",
            canonical_cwd=str(project),
            desired=desired,
            lifecycle_status="CONFIGURED",
        ),
    )

    from ai_account_manager.core.project_trust import reconcile_configured_project_trust

    reconcile_configured_project_trust(provider_id="codex", settings=settings)

    assert _trust_level(first, project) == "trusted"
    assert _trust_level(second, project) == "trusted"
