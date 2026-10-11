from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from ai_account_manager.app import create_app
from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.providers.codex.auth_status import get_codex_auth_status


def _settings(tmp_path) -> AppSettings:
    return AppSettings(
        app_env="test",
        app_host="127.0.0.1",
        app_port=8710,
        ai_profiles_root=tmp_path / "profiles",
        ai_quota_cache_root=tmp_path / "quota_cache",
        runtime_data_root=tmp_path / "runtime",
        project_inventory_path=tmp_path / "runtime" / "project_inventory_v1.json",
        project_activity_path=tmp_path / "runtime" / "project_activity_v1.json",
        project_bindings_path=tmp_path / "runtime" / "project_bindings.json",
        telegram_ai_agent_root=tmp_path / "telegram-ai-agent",
        telegram_ai_agent_topic_config_path=tmp_path / "telegram-ai-agent" / "topic_config.json",
        telegram_ai_agent_tmux_state_path=tmp_path
        / "telegram-ai-agent"
        / "tmux_sessions"
        / "state.json",
        telegram_ai_agent_compatibility_manifest=tmp_path / "compatibility.json",
        codex_usage_endpoint="https://chatgpt.com/backend-api/wham/usage",
        codex_binary=tmp_path / "bin" / "codex",
        codex_auth_enrollment_root=tmp_path / "runtime" / "codex_auth_enrollments",
        codex_auth_enrollment_ttl_seconds=900,
        codex_auth_stale_after_seconds=691200,
        enable_provider_codex=True,
        enable_provider_claude_code=False,
        enable_provider_gemini_cli=False,
    )


def test_codex_auth_status_ready_does_not_expose_values(tmp_path) -> None:
    settings = _settings(tmp_path)
    profile_dir = settings.codex_profiles_root / "codex_a"
    profile_dir.mkdir(parents=True)
    (profile_dir / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "last_refresh": "2026-06-10T10:00:00Z",
                "tokens": {
                    "access_token": "secret-access-value",
                    "refresh_token": "secret-refresh-value",
                    "account_id": "acc_secret_value",
                },
            }
        ),
        encoding="utf-8",
    )

    status = get_codex_auth_status(
        "codex_a",
        settings=settings,
        now=datetime(2026, 6, 10, 11, 0, 0, tzinfo=UTC),
    )

    assert status.status == "ready"
    assert status.has_access_token is True
    assert status.has_refresh_token is True
    assert status.has_account_id is True
    dumped = status.model_dump_json()
    assert "secret-access-value" not in dumped
    assert "secret-refresh-value" not in dumped
    assert "acc_secret_value" not in dumped


def test_codex_auth_status_stale_by_last_refresh(tmp_path) -> None:
    settings = _settings(tmp_path)
    profile_dir = settings.codex_profiles_root / "codex_a"
    profile_dir.mkdir(parents=True)
    old_refresh = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
    (profile_dir / "auth.json").write_text(
        json.dumps(
            {
                "last_refresh": old_refresh.isoformat().replace("+00:00", "Z"),
                "tokens": {
                    "access_token": "secret-access-value",
                    "refresh_token": "secret-refresh-value",
                    "account_id": "acc_secret_value",
                },
            }
        ),
        encoding="utf-8",
    )

    status = get_codex_auth_status(
        "codex_a",
        settings=settings,
        now=old_refresh + timedelta(days=9),
    )

    assert status.status == "stale_by_last_refresh"
    assert status.is_stale_by_last_refresh is True
    assert status.recommendation == "let_codex_refresh_auth_on_next_trusted_run"


def test_codex_auth_status_missing_profile(tmp_path) -> None:
    status = get_codex_auth_status("codex_a", settings=_settings(tmp_path))
    assert status.status == "profile_missing"
    assert status.has_auth_json is False


def test_codex_auth_status_endpoint(monkeypatch, tmp_path) -> None:
    profile_dir = tmp_path / "profiles" / "codex" / "codex_a"
    profile_dir.mkdir(parents=True)
    (profile_dir / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "secret-access-value"}}),
        encoding="utf-8",
    )

    monkeypatch.setenv("AI_PROFILES_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("AI_QUOTA_CACHE_ROOT", str(tmp_path / "quota"))
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        response = client.get("/api/providers/codex/accounts/codex_a/auth-status")
        assert response.status_code == 200
        body = response.text
        assert "secret-access-value" not in body
        assert response.json()["has_access_token"] is True
    finally:
        get_settings.cache_clear()
