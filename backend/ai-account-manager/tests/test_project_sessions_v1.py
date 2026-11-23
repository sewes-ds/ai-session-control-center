from __future__ import annotations

import json
from pathlib import Path

from ai_account_manager.core.config import AppSettings
from ai_account_manager.core.runtime_control import register_agent_instance, upsert_runtime_binding
from ai_account_manager.integrations import project_sessions
from ai_account_manager.schemas.runtime_control import (
    AgentInstanceRecord,
    DesiredSpecRecord,
    RuntimeBindingRecord,
    canonical_digest,
)


def _settings(tmp_path: Path) -> AppSettings:
    bot_root = tmp_path / "telegram-agent"
    (bot_root / "tmux_sessions").mkdir(parents=True)
    return AppSettings(
        app_env="test",
        app_host="127.0.0.1",
        app_port=8710,
        ai_profiles_root=tmp_path / "profiles",
        ai_quota_cache_root=tmp_path / "quota",
        runtime_data_root=tmp_path / "runtime",
        telegram_ai_agent_root=bot_root,
        telegram_ai_agent_topic_config_path=bot_root / "topic_config.json",
        telegram_ai_agent_tmux_state_path=bot_root / "tmux_sessions" / "state.json",
        codex_usage_endpoint="https://chatgpt.com/backend-api/wham/usage",
        codex_binary=tmp_path / "bin" / "codex",
        codex_auth_enrollment_root=tmp_path / "runtime" / "enrollments",
        codex_auth_enrollment_ttl_seconds=900,
        codex_auth_stale_after_seconds=691200,
        enable_provider_codex=True,
        enable_provider_claude_code=True,
        enable_provider_gemini_cli=False,
        ai_project_roots=(tmp_path / "projects",),
        project_inventory_path=tmp_path / "runtime" / "inventory.json",
        runtime_control_path=tmp_path / "runtime" / "control.json",
    )


def _binding(project: Path) -> RuntimeBindingRecord:
    material: dict[str, object] = {
        "binding_id": "binding-42",
        "agent_instance_id": "agent-1",
        "generation_id": "generation-1",
        "revision": 3,
        "project_id": "project-study",
        "canonical_cwd": str(project),
        "provider": "claude_code",
        "account_profile": "deepseek_default",
        "model": "deepseek-chat",
        "reasoning_effort": "max",
    }
    return RuntimeBindingRecord(
        binding_id="binding-42",
        agent_instance_id="agent-1",
        chat_id=-1001,
        thread_id=42,
        topic_name="Study",
        project_id="project-study",
        canonical_cwd=str(project),
        desired=DesiredSpecRecord(
            **material,
            spec_digest=canonical_digest(material),
            source_kind="cus_ui",
            source_ref="test",
        ),
        lifecycle_status="CONFIGURED",
    )


def test_project_view_uses_runtime_binding_when_topic_file_has_no_runtime_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    project = settings.ai_project_roots[0] / "study"
    project.mkdir(parents=True)
    settings.telegram_ai_agent_topic_config_path.write_text(
        json.dumps(
            {
                "topics": {
                    "42": {"type": "project", "name": "Old display", "engine": "codex"},
                    "99": {"type": "project", "name": "Not configured"},
                }
            }
        ),
        encoding="utf-8",
    )
    settings.telegram_ai_agent_tmux_state_path.write_text("{}\n", encoding="utf-8")
    register_agent_instance(
        settings.runtime_control_path,
        AgentInstanceRecord(
            agent_instance_id="agent-1",
            pinned_upstream_sha="0" * 40,
            telegram_bot_identity="bot-agent-1",
            telegram_bot_token_fingerprint="sha256:" + "1" * 64,
            forum_chat_id=-1001,
            runtime_socket="/run/user/1000/ai-agent-runtime/agent-1/runtime.sock",
        ),
    )
    upsert_runtime_binding(settings.runtime_control_path, _binding(project.resolve()))
    monkeypatch.setattr(project_sessions, "_pane_map", lambda: {})

    response = project_sessions.observe_project_sessions(settings)

    configured = next(item for item in response.projects if item.thread_id == 42)
    assert configured.onboarding_state == "CONFIGURED"
    assert configured.management_status == "managed"
    assert configured.chat_id == -1001
    assert configured.project_path == str(project.resolve())
    assert configured.project_id == "project-study"
    assert configured.agent == "claude_code"
    assert configured.account_slug == "deepseek_default"
    assert configured.missing_launch_fields == []

    unconfigured = next(item for item in response.projects if item.thread_id == 99)
    assert unconfigured.onboarding_state == "UNCONFIGURED"
    assert unconfigured.management_status == "not_configured"
    assert unconfigured.setup_action_enabled is True
