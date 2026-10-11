from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_account_manager.api.routes import codex as codex_routes
from ai_account_manager.app import create_app
from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.providers.codex.app_server import (
    CodexAppServerError,
    CodexAppServerProbeResult,
    MethodResult,
)
from ai_account_manager.providers.codex.capabilities import (
    cache_path,
    lock_path,
    read_cached_codex_capabilities,
    refresh_codex_capabilities,
)
from ai_account_manager.schemas.codex import CodexCapabilitiesSnapshot


def _settings(tmp_path: Path) -> AppSettings:
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
        telegram_ai_agent_tmux_state_path=tmp_path / "telegram-ai-agent" / "tmux_sessions" / "state.json",
        telegram_ai_agent_compatibility_manifest=tmp_path / "compatibility.json",
        codex_usage_endpoint="https://example.invalid/usage",
        codex_binary=tmp_path / "bin" / "codex",
        codex_auth_enrollment_root=tmp_path / "runtime" / "codex_auth_enrollments",
        codex_auth_enrollment_ttl_seconds=900,
        codex_auth_stale_after_seconds=691200,
        enable_provider_codex=True,
        enable_provider_claude_code=False,
        enable_provider_gemini_cli=False,
        codex_capabilities_cache_root=tmp_path / "runtime" / "codex_capabilities",
        codex_app_server_timeout_seconds=1.0,
    )


def _profile(settings: AppSettings, slug: str) -> Path:
    profile = settings.codex_profiles_root / slug
    profile.mkdir(parents=True)
    (profile / "auth.json").write_text('{"opaque":true}\n', encoding="utf-8")
    return profile


def _probe(slug: str, profile: Path, *, suffix: str = "") -> CodexAppServerProbeResult:
    return CodexAppServerProbeResult(
        account_slug=slug,
        profile_path=profile,
        profile_integrity_status="unchanged",
        methods={
            "account": MethodResult(
                "ok",
                {
                    "requiresOpenaiAuth": False,
                    "account": {
                        "email": f"{slug}@Example.COM",
                        "type": "chatgpt",
                        "planType": "plus",
                    },
                },
            ),
            "models": MethodResult(
                "ok",
                {
                    "data": [
                        {
                            "id": f"model-{suffix or slug}",
                            "model": f"model-{suffix or slug}",
                            "displayName": "Model",
                            "hidden": False,
                            "isDefault": True,
                            "defaultReasoningEffort": "medium",
                            "supportedReasoningEfforts": [
                                {"reasoningEffort": "high"},
                                {"reasoningEffort": "low"},
                                {"reasoningEffort": "high"},
                            ],
                            "inputModalities": ["text", "image", "text"],
                            "supportsPersonality": False,
                        }
                    ],
                    "nextCursor": None,
                },
            ),
            "rate_limits": MethodResult(
                "ok",
                {
                    "rateLimits": {
                        "limitId": "codex",
                        "limitName": "Codex",
                        "planType": "plus",
                        "primary": {"usedPercent": 25.5, "windowDurationMins": 300, "resetsAt": 123},
                    },
                    "rateLimitsByLimitId": {
                        "codex": {
                            "limitId": "codex",
                            "primary": {"usedPercent": 25.5, "windowDurationMins": 300},
                        },
                        "premium": {
                            "limitId": "premium",
                            "secondary": {"usedPercent": 90, "windowDurationMins": 10080},
                        },
                    },
                    "rateLimitResetCredits": {
                        "availableCount": 1,
                        "credits": [
                            {
                                "id": "raw-reset-credit-id",
                                "resetType": "codexRateLimits",
                                "status": "available",
                                "grantedAt": 1,
                                "expiresAt": 2,
                                "title": "Reset",
                                "description": "Safe",
                            }
                        ],
                    },
                },
            ),
            "usage": MethodResult(
                "ok",
                {
                    "summary": {"turns": 2, "access_token": "must-drop"},
                    "dailyUsageBuckets": [{"day": "2026-07-26", "turns": 2}],
                },
            ),
        },
    )


def test_first_read_returns_not_refreshed_and_separate_paths(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _profile(settings, "codex_a")
    _profile(settings, "codex_b")

    snapshot = read_cached_codex_capabilities("codex_a", settings=settings)

    assert snapshot.status == "not_refreshed"
    assert snapshot.models.items == []
    assert cache_path(settings, "codex_a") != cache_path(settings, "codex_b")
    assert lock_path(settings, "codex_a") != lock_path(settings, "codex_b")


def test_refresh_normalizes_models_rate_limits_reset_credits_and_safe_account(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    profile = _profile(settings, "codex_a")

    snapshot = refresh_codex_capabilities(
        "codex_a",
        settings=settings,
        probe_runner=lambda slug, settings: _probe(slug, profile),
    )
    payload = json.dumps(snapshot.model_dump(), sort_keys=True)

    assert snapshot.status == "ok"
    assert snapshot.account.safe_identifier == (
        "email_sha256:"
        + hashlib.sha256("codex_a@example.com".encode()).hexdigest()[:12]
    )
    assert snapshot.account.email_masked == "c***a@E***e.COM"
    assert snapshot.models.items[0].supported_reasoning_efforts == ["high", "low"]
    assert snapshot.models.items[0].input_modalities == ["image", "text"]
    assert len(snapshot.rate_limits.buckets) == 2
    assert snapshot.rate_limits.buckets[0].windows[0].remaining_percent == 74.5
    assert snapshot.rate_limits.reset_credits.available_count == 1
    assert snapshot.rate_limits.reset_credits.items[0].credit_id_sha256_12 == (
        hashlib.sha256("raw-reset-credit-id".encode()).hexdigest()[:12]
    )
    assert "raw-reset-credit-id" not in payload
    assert "codex_a@Example.COM" not in payload
    assert "must-drop" not in payload
    assert cache_path(settings, "codex_a").stat().st_mode & 0o777 == 0o600
    assert lock_path(settings, "codex_a").stat().st_mode & 0o777 == 0o600
    assert settings.codex_capabilities_cache_root.stat().st_mode & 0o777 == 0o700


def test_fail_soft_preserves_last_success_and_isolates_accounts(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profile_a = _profile(settings, "codex_a")
    profile_b = _profile(settings, "codex_b")
    refresh_codex_capabilities(
        "codex_a",
        settings=settings,
        probe_runner=lambda slug, settings: _probe(slug, profile_a, suffix="a"),
    )
    refresh_codex_capabilities(
        "codex_b",
        settings=settings,
        probe_runner=lambda slug, settings: _probe(slug, profile_b, suffix="b"),
    )
    before_b = cache_path(settings, "codex_b").read_bytes()

    def failed_probe(slug: str, settings: AppSettings) -> CodexAppServerProbeResult:
        probe = _probe(slug, profile_a, suffix="a2")
        probe.methods["models"] = MethodResult("error", error_code="method_failed")
        return probe

    failed = refresh_codex_capabilities("codex_a", settings=settings, probe_runner=failed_probe)

    assert failed.status == "partial"
    assert failed.models.status == "error"
    assert failed.models.items[0].model_id == "model-a"
    assert cache_path(settings, "codex_b").read_bytes() == before_b

    def process_failed(slug: str, settings: AppSettings) -> CodexAppServerProbeResult:
        raise CodexAppServerError("process_failed", "Bearer hidden-token-1234567890")

    process_error = refresh_codex_capabilities(
        "codex_a", settings=settings, probe_runner=process_failed
    )
    assert process_error.status == "partial"
    assert "hidden-token" not in (process_error.error_message or "")


def test_invalid_cache_is_quarantined(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _profile(settings, "codex_a")
    path = cache_path(settings, "codex_a")
    path.write_text("{broken", encoding="utf-8")
    path.chmod(0o600)

    snapshot = read_cached_codex_capabilities("codex_a", settings=settings)

    assert snapshot.status == "not_refreshed"
    assert not path.exists()
    assert list(path.parent.glob("codex_a.json.*.invalid"))


def test_capabilities_routes_return_expected_statuses(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profile = _profile(settings, "codex_a")
    monkeypatch.setenv("AI_PROFILES_ROOT", str(settings.ai_profiles_root))
    monkeypatch.setenv("CODEX_CAPABILITIES_CACHE_ROOT", str(settings.codex_capabilities_cache_root))
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()

    def route_refresh(slug: str) -> CodexCapabilitiesSnapshot:
        return refresh_codex_capabilities(
            slug,
            settings=settings,
            probe_runner=lambda account_slug, settings: _probe(account_slug, profile),
        )

    monkeypatch.setattr(codex_routes, "refresh_codex_capabilities", route_refresh)
    try:
        client = TestClient(create_app())
        assert client.get("/api/providers/codex/accounts/missing_valid/capabilities").status_code == 404
        missing_cache = client.get("/api/providers/codex/accounts/codex_a/capabilities")
        assert missing_cache.status_code == 200
        assert missing_cache.json()["status"] == "not_refreshed"
        assert client.get("/api/providers/codex/accounts/!/capabilities").status_code == 400
        refreshed = client.post("/api/providers/codex/accounts/codex_a/capabilities/refresh")
        assert refreshed.status_code == 200
        assert refreshed.json()["status"] == "ok"
        cached = client.get("/api/providers/codex/accounts/codex_a/capabilities")
        assert cached.status_code == 200
        assert cached.json()["models"]["items"][0]["model_id"] == "model-codex_a"
        assert client.get("/health").status_code == 200
    finally:
        get_settings.cache_clear()


def test_codex_app_server_timeout_setting_validates_range(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_APP_SERVER_TIMEOUT_SECONDS", "120")
    get_settings.cache_clear()
    try:
        assert get_settings().codex_app_server_timeout_seconds == 120.0
    finally:
        get_settings.cache_clear()

    monkeypatch.setenv("CODEX_APP_SERVER_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError):
        get_settings()
    get_settings.cache_clear()

    monkeypatch.setenv("CODEX_APP_SERVER_TIMEOUT_SECONDS", "121")
    with pytest.raises(ValueError):
        get_settings()
    get_settings.cache_clear()
