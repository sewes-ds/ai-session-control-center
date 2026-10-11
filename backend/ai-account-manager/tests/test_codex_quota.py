from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from ai_account_manager.app import create_app
from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.providers.codex.quota import (
    consume_rate_limit_reset_credit,
    get_cached_codex_quota,
    normalize_codex_rate_limits_response,
    parse_codex_usage_response,
    refresh_codex_quota,
)
from ai_account_manager.schemas.codex import CodexQuotaSnapshot


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


def test_parse_codex_usage_response_primary_secondary() -> None:
    fetched_at = datetime(2026, 6, 10, 13, 0, 0, tzinfo=UTC)
    snapshot = parse_codex_usage_response(
        account_slug="codex_a",
        endpoint="https://example.test/usage",
        fetched_at=fetched_at,
        usage_payload={
            "rate_limit": {
                "primary_window": {
                    "used_percent": 37.5,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": 600,
                },
                "secondary_window": {
                    "used_percent": 12,
                    "limit_window_seconds": 604800,
                    "reset_at": "2026-06-15T00:00:00Z",
                },
            }
        },
    )

    assert snapshot.status == "ok"
    assert snapshot.windows[0].window_id == "primary"
    assert snapshot.windows[0].remaining_percent == 62.5
    assert snapshot.windows[0].reset_at == "2026-06-10T13:10:00Z"
    assert snapshot.windows[1].window_id == "secondary"
    assert snapshot.windows[1].remaining_percent == 88.0


def test_normalize_rate_limits_keeps_primary_10080_as_seven_day_not_five_hour() -> None:
    snapshot = normalize_codex_rate_limits_response(
        account_slug="codex_a",
        fetched_at=datetime(2026, 7, 27, tzinfo=UTC),
        rate_limits_payload={
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "limitName": "Codex",
                    "primary": {"usedPercent": 10, "windowDurationMins": 10080, "resetsAt": "2026-08-03T00:00:00Z"},
                }
            },
            "rateLimitResetCredits": {"availableCount": 0, "credits": None},
        },
    )

    assert snapshot.buckets[0].windows[0].window_type == "primary"
    assert snapshot.buckets[0].windows[0].window_duration_minutes == 10080
    assert snapshot.windows[0].window_id == "secondary"
    assert all(window.window_id != "primary" for window in snapshot.windows)


def test_normalize_rate_limits_keeps_compat_aliases_duration_based() -> None:
    snapshot = normalize_codex_rate_limits_response(
        account_slug="codex_a",
        fetched_at=datetime(2026, 7, 27, tzinfo=UTC),
        rate_limits_payload={
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "limitName": "Codex",
                    "primary": {"usedPercent": 20, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 30, "windowDurationMins": 10080},
                }
            },
            "rateLimitResetCredits": {"availableCount": 0, "credits": None},
        },
    )

    assert [(window.window_id, window.window_duration_minutes) for window in snapshot.windows] == [
        ("primary", 300),
        ("secondary", 10080),
    ]


def test_normalize_rate_limits_keeps_multiple_buckets_fallback_dedup_and_unknown() -> None:
    snapshot = normalize_codex_rate_limits_response(
        account_slug="codex_a",
        rate_limits_payload={
            "rateLimitsByLimitId": {
                "other": {
                    "limitName": "Other",
                    "primary": {"usedPercent": 110, "windowDurationMins": 60},
                    "secondary": "bad-window",
                },
                "codex": {
                    "limitId": "codex",
                    "limitName": "Codex",
                    "primary": {"usedPercent": 25, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 40, "windowDurationMins": 10080},
                },
            },
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 1, "windowDurationMins": 300},
            },
            "rateLimitResetCredits": {"availableCount": 2, "credits": []},
        },
    )

    assert [bucket.limit_id for bucket in snapshot.buckets] == ["codex", "other"]
    assert snapshot.buckets[0].windows[0].remaining_percent == 75
    assert snapshot.buckets[1].windows[0].remaining_percent == 0
    assert snapshot.reset_credits.available_count == 2
    assert snapshot.reset_credits.credits == []


def test_refresh_codex_quota_reads_auth_server_side_and_caches_safe_snapshot(tmp_path) -> None:
    settings = _settings(tmp_path)
    profile_dir = settings.codex_profiles_root / "codex_a"
    profile_dir.mkdir(parents=True)
    (profile_dir / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "secret-access-token",
                    "refresh_token": "secret-refresh-token",
                    "account_id": "acc_test",
                }
            }
        ),
        encoding="utf-8",
    )

    seen: dict[str, str | None] = {}

    def fake_fetcher(slug: str):
        seen["slug"] = slug
        return {
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 40, "windowDurationMins": 300},
                "secondary": {"usedPercent": 10, "windowDurationMins": 10080},
            },
            "rateLimitResetCredits": {"availableCount": 1, "credits": None},
        }

    snapshot = refresh_codex_quota("codex_a", settings=settings, rate_limits_fetcher=fake_fetcher)

    assert seen == {"slug": "codex_a"}
    assert snapshot.status == "ok"
    assert snapshot.reset_credits.available_count == 1
    dumped = snapshot.model_dump_json()
    assert "secret-access-token" not in dumped
    assert "secret-refresh-token" not in dumped
    assert get_cached_codex_quota("codex_a", settings=settings).status == "ok"


def test_get_cached_quota_returns_not_refreshed_for_missing_snapshot(tmp_path) -> None:
    snapshot = get_cached_codex_quota("codex_a", settings=_settings(tmp_path))
    assert snapshot.status == "not_refreshed"
    assert snapshot.windows == []


def test_codex_quota_cache_endpoint_not_refreshed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AI_PROFILES_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("AI_QUOTA_CACHE_ROOT", str(tmp_path / "quota"))
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        response = client.get("/api/providers/codex/accounts/codex_a/quota")
        assert response.status_code == 200
        assert response.json()["status"] == "not_refreshed"
    finally:
        get_settings.cache_clear()


def test_codex_quota_refresh_endpoint_error_does_not_expose_tokens(monkeypatch, tmp_path) -> None:
    profile_dir = tmp_path / "profiles" / "codex" / "codex_a"
    profile_dir.mkdir(parents=True)
    (profile_dir / "auth.json").write_text(
        json.dumps({"tokens": {"refresh_token": "secret-refresh-token"}}),
        encoding="utf-8",
    )

    monkeypatch.setenv("AI_PROFILES_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("AI_QUOTA_CACHE_ROOT", str(tmp_path / "quota"))
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        response = client.post("/api/providers/codex/accounts/codex_a/quota/refresh")
        assert response.status_code in {400, 502}
        body = response.text
        assert "secret-refresh-token" not in body
    finally:
        get_settings.cache_clear()


def test_consume_reset_credit_outcomes_and_idempotency_key(tmp_path) -> None:
    seen = {}
    refreshed = CodexQuotaSnapshot(account_slug="codex_a", status="ok")
    settings = _settings(tmp_path)
    profile_dir = settings.codex_profiles_root / "codex_a"
    profile_dir.mkdir(parents=True)
    (profile_dir / "auth.json").write_text("{}", encoding="utf-8")

    def caller(slug, params):
        seen["slug"] = slug
        seen["params"] = params
        return {"outcome": "alreadyRedeemed"}

    result = consume_rate_limit_reset_credit(
        "codex_a",
        idempotency_key="11111111-1111-4111-8111-111111111111",
        credit_id="opaque-credit",
        consume_caller=caller,
        refresh_after=lambda: refreshed,
        settings=settings,
    )

    assert result.success is True
    assert result.outcome == "alreadyRedeemed"
    assert result.quota is refreshed
    assert seen["params"] == {
        "idempotencyKey": "11111111-1111-4111-8111-111111111111",
        "creditId": "opaque-credit",
    }


def test_consume_reset_credit_non_success_and_unexpected_fail_closed(tmp_path) -> None:
    settings = _settings(tmp_path)
    profile_dir = settings.codex_profiles_root / "codex_a"
    profile_dir.mkdir(parents=True)
    (profile_dir / "auth.json").write_text("{}", encoding="utf-8")

    no_credit = consume_rate_limit_reset_credit(
        "codex_a",
        idempotency_key="11111111-1111-4111-8111-111111111111",
        consume_caller=lambda _slug, _params: {"outcome": "noCredit"},
        settings=settings,
    )
    assert no_credit.success is False
    assert no_credit.quota is None

    try:
        consume_rate_limit_reset_credit(
            "codex_a",
            idempotency_key="11111111-1111-4111-8111-111111111111",
            consume_caller=lambda _slug, _params: {"outcome": "surprise"},
            settings=settings,
        )
    except Exception as exc:
        assert getattr(exc, "code") == "unexpected_outcome"
    else:
        raise AssertionError("unexpected consume outcome must fail closed")
