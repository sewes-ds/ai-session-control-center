from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_account_manager.core.config import AppSettings
from ai_account_manager.core.project_settings import upsert_project_settings
from ai_account_manager.providers.codex.account_enrollment import (
    CodexAccountEnrollmentError,
    cancel_codex_account_enrollment,
    get_codex_account_enrollment,
    start_codex_account_enrollment,
)
from ai_account_manager.providers.codex import openviking_defaults


class _FakeProcess:
    def __init__(self, pid: int = 43210) -> None:
        self.pid = pid

    def wait(self) -> int:
        return 0


def _settings(tmp_path: Path) -> AppSettings:
    codex_binary = tmp_path / "bin" / "codex"
    codex_binary.parent.mkdir(parents=True)
    codex_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex_binary.chmod(0o700)
    bot_root = tmp_path / "telegram-ai-agent"
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
        telegram_ai_agent_root=bot_root,
        telegram_ai_agent_topic_config_path=bot_root / "topic_config.json",
        telegram_ai_agent_tmux_state_path=bot_root / "tmux_sessions" / "state.json",
        telegram_ai_agent_compatibility_manifest=tmp_path / "compatibility.json",
        codex_usage_endpoint="https://chatgpt.com/backend-api/wham/usage",
        codex_binary=codex_binary,
        codex_auth_enrollment_root=tmp_path / "runtime" / "codex_auth_enrollments",
        codex_auth_enrollment_ttl_seconds=900,
        codex_auth_stale_after_seconds=691200,
        enable_provider_codex=True,
        enable_provider_claude_code=False,
        enable_provider_gemini_cli=False,
        ai_project_roots=(tmp_path / "projects",),
        project_settings_path=tmp_path / "runtime" / "project_settings_v2.json",
        runtime_launch_specs_path=tmp_path / "runtime" / "runtime_launch_specs_v1.json",
    )


def test_start_uses_staging_and_does_not_publish_empty_profile(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    seen: dict[str, object] = {}

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        seen["start_new_session"] = kwargs["start_new_session"]
        return _FakeProcess()

    enrollment = start_codex_account_enrollment(
        "codex_b",
        label="Second account",
        settings=settings,
        popen_factory=fake_popen,
        now=datetime(2026, 6, 11, 9, 0, tzinfo=UTC),
    )

    assert enrollment.status == "starting"
    assert enrollment.account_slug == "codex_b"
    assert not (settings.codex_profiles_root / "codex_b").exists()
    assert seen["argv"] == [str(settings.codex_binary), "login", "--device-auth"]
    assert str(seen["env"]["CODEX_HOME"]).endswith("/profile")  # type: ignore[index]
    assert seen["start_new_session"] is True


def test_waiting_status_exposes_only_official_url_and_user_code(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    enrollment = start_codex_account_enrollment(
        "codex_b",
        settings=settings,
        popen_factory=lambda *args, **kwargs: _FakeProcess(),
    )
    enrollment_dir = settings.codex_auth_enrollment_root / enrollment.enrollment_id
    (enrollment_dir / "device_auth.log").write_text(
        "Open https://auth.openai.com/codex/device and enter ABCD-EFGH\n"
        "ignore https://evil.example/secret TOKEN=do-not-return\n",
        encoding="utf-8",
    )

    status = get_codex_account_enrollment(
        enrollment.enrollment_id,
        settings=settings,
        pid_alive=lambda _pid: True,
    )

    assert status.status == "waiting_for_user"
    assert status.verification_url == "https://auth.openai.com/codex/device"
    assert status.user_code == "ABCD-EFGH"
    dumped = status.model_dump_json()
    assert "evil.example" not in dumped
    assert "do-not-return" not in dumped


def test_completed_auth_atomically_publishes_profile(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    enrollment = start_codex_account_enrollment(
        "codex_b",
        label="Second account",
        settings=settings,
        popen_factory=lambda *args, **kwargs: _FakeProcess(),
        now=datetime(2026, 6, 11, 9, 0, tzinfo=UTC),
    )
    staging = settings.codex_auth_enrollment_root / enrollment.enrollment_id / "profile"
    (staging / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "secret"}}), encoding="utf-8"
    )

    status = get_codex_account_enrollment(
        enrollment.enrollment_id,
        settings=settings,
        pid_alive=lambda _pid: False,
        now=datetime(2026, 6, 11, 9, 1, tzinfo=UTC),
    )

    final_profile = settings.codex_profiles_root / "codex_b"
    assert status.status == "completed"
    assert status.profile_path == str(final_profile)
    assert (final_profile / "auth.json").is_file()
    assert json.loads((final_profile / "profile.json").read_text())["label"] == "Second account"
    assert not staging.exists()
    assert "secret" not in status.model_dump_json()


def test_new_account_inherits_centrally_configured_project_trust(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    project = settings.ai_project_roots[0] / "study"
    project.mkdir(parents=True)
    upsert_project_settings(
        project_path=str(project),
        provider_id="codex",
        account_profile="codex_a",
        model="gpt-5.6-sol",
        reasoning_effort="high",
        expected_revision=None,
        settings=settings,
    )
    enrollment = start_codex_account_enrollment(
        "codex_b",
        settings=settings,
        popen_factory=lambda *args, **kwargs: _FakeProcess(),
    )
    staging = settings.codex_auth_enrollment_root / enrollment.enrollment_id / "profile"
    auth = staging / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "secret"}}), encoding="utf-8")
    auth.chmod(0o600)

    status = get_codex_account_enrollment(
        enrollment.enrollment_id,
        settings=settings,
        pid_alive=lambda _pid: False,
    )

    config_path = settings.codex_profiles_root / "codex_b" / "config.toml"
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert status.status == "completed"
    assert parsed["projects"][str(project.resolve())]["trust_level"] == "trusted"


def _approved_openviking_seed(settings: AppSettings, monkeypatch) -> Path:
    seed = settings.codex_profiles_root / "codex_a"
    seed.mkdir(parents=True, mode=0o700)
    seed.chmod(0o700)
    package = seed / "plugins/cache/openviking/openviking-memory/0.11.2"
    (package / "hooks").mkdir(parents=True, mode=0o700)
    (package / "hooks/hooks.json").write_text('{"hooks": {}}\n', encoding="utf-8")
    monkeypatch.setattr(
        openviking_defaults,
        "_APPROVED_TREE_SHA256",
        openviking_defaults._tree_digest(package),
    )
    return package


def test_new_account_inherits_approved_openviking_hooks(tmp_path: Path, monkeypatch) -> None:
    settings = replace(_settings(tmp_path), codex_openviking_seed_account="codex_a")
    package = _approved_openviking_seed(settings, monkeypatch)
    enrollment = start_codex_account_enrollment(
        "codex_b", settings=settings, popen_factory=lambda *args, **kwargs: _FakeProcess()
    )
    staging = settings.codex_auth_enrollment_root / enrollment.enrollment_id / "profile"
    (staging / "auth.json").write_text('{"tokens": {}}', encoding="utf-8")

    status = get_codex_account_enrollment(
        enrollment.enrollment_id, settings=settings, pid_alive=lambda _pid: False
    )

    profile = settings.codex_profiles_root / "codex_b"
    config = tomllib.loads((profile / "config.toml").read_text(encoding="utf-8"))
    assert status.status == "completed"
    assert config["features"] == {"hooks": True, "plugin_hooks": True}
    assert config["plugins"]["openviking-memory@openviking"]["enabled"] is True
    assert config["marketplaces"]["openviking"]["last_revision"] == openviking_defaults._REVISION
    assert len(config["hooks"]["state"]) == 8
    assert (
        profile / "plugins/cache/openviking/openviking-memory/0.11.2/hooks/hooks.json"
    ).read_bytes() == (package / "hooks/hooks.json").read_bytes()


def test_changed_openviking_seed_blocks_account_publication(tmp_path: Path, monkeypatch) -> None:
    settings = replace(_settings(tmp_path), codex_openviking_seed_account="codex_a")
    package = _approved_openviking_seed(settings, monkeypatch)
    (package / "hooks/hooks.json").write_text('{"hooks": {"changed": true}}\n', encoding="utf-8")
    enrollment = start_codex_account_enrollment(
        "codex_b", settings=settings, popen_factory=lambda *args, **kwargs: _FakeProcess()
    )
    staging = settings.codex_auth_enrollment_root / enrollment.enrollment_id / "profile"
    (staging / "auth.json").write_text('{"tokens": {}}', encoding="utf-8")

    status = get_codex_account_enrollment(
        enrollment.enrollment_id, settings=settings, pid_alive=lambda _pid: False
    )

    assert status.status == "failed"
    assert status.error_code == "openviking_package_changed"
    assert not (settings.codex_profiles_root / "codex_b").exists()


def test_openviking_does_not_override_explicitly_disabled_hooks(
    tmp_path: Path, monkeypatch
) -> None:
    settings = replace(_settings(tmp_path), codex_openviking_seed_account="codex_a")
    _approved_openviking_seed(settings, monkeypatch)
    enrollment = start_codex_account_enrollment(
        "codex_b", settings=settings, popen_factory=lambda *args, **kwargs: _FakeProcess()
    )
    staging = settings.codex_auth_enrollment_root / enrollment.enrollment_id / "profile"
    (staging / "auth.json").write_text('{"tokens": {}}', encoding="utf-8")
    config = staging / "config.toml"
    config.write_text("[features]\nhooks = false\n", encoding="utf-8")
    config.chmod(0o600)

    status = get_codex_account_enrollment(
        enrollment.enrollment_id, settings=settings, pid_alive=lambda _pid: False
    )

    assert status.status == "failed"
    assert status.error_code == "openviking_config_conflict"
    assert not (settings.codex_profiles_root / "codex_b").exists()


def test_openviking_defaults_extend_existing_features_without_duplicate_table() -> None:
    rendered = openviking_defaults._approved_config(b"[features]\nhooks = true\n")

    parsed = tomllib.loads(rendered.decode("utf-8"))
    assert parsed["features"] == {"hooks": True, "plugin_hooks": True}
    assert rendered.count(b"[features]") == 1


def test_cancel_removes_staging_profile(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    enrollment = start_codex_account_enrollment(
        "codex_b",
        settings=settings,
        popen_factory=lambda *args, **kwargs: _FakeProcess(),
    )
    monkeypatch.setattr(
        "ai_account_manager.providers.codex.account_enrollment._terminate_process_group",
        lambda _pid: None,
    )

    result = cancel_codex_account_enrollment(enrollment.enrollment_id, settings=settings)

    assert result.status == "cancelled"
    staging = settings.codex_auth_enrollment_root / enrollment.enrollment_id / "profile"
    assert not staging.exists()
    assert not (settings.codex_profiles_root / "codex_b").exists()


def test_existing_profile_and_duplicate_active_enrollment_are_blocked(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (settings.codex_profiles_root / "codex_a").mkdir(parents=True)
    with pytest.raises(CodexAccountEnrollmentError) as existing:
        start_codex_account_enrollment("codex_a", settings=settings)
    assert existing.value.code == "account_exists"

    start_codex_account_enrollment(
        "codex_b",
        settings=settings,
        popen_factory=lambda *args, **kwargs: _FakeProcess(),
    )
    with pytest.raises(CodexAccountEnrollmentError) as duplicate:
        start_codex_account_enrollment(
            "codex_b",
            settings=settings,
            popen_factory=lambda *args, **kwargs: _FakeProcess(43211),
        )
    assert duplicate.value.code == "enrollment_in_progress"


def test_expired_authorization_cleans_staging(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    started_at = datetime(2026, 6, 11, 9, 0, tzinfo=UTC)
    enrollment = start_codex_account_enrollment(
        "codex_b",
        settings=settings,
        popen_factory=lambda *args, **kwargs: _FakeProcess(),
        now=started_at,
    )
    monkeypatch.setattr(
        "ai_account_manager.providers.codex.account_enrollment._terminate_process_group",
        lambda _pid: None,
    )

    result = get_codex_account_enrollment(
        enrollment.enrollment_id,
        settings=settings,
        pid_alive=lambda _pid: True,
        now=started_at + timedelta(seconds=901),
    )

    assert result.status == "expired"
    assert result.error_code == "authorization_expired"
    assert not (settings.codex_auth_enrollment_root / enrollment.enrollment_id / "profile").exists()


def test_start_request_rejects_password_field() -> None:
    from pydantic import ValidationError

    from ai_account_manager.schemas.codex import CodexAccountEnrollmentStartRequest

    with pytest.raises(ValidationError):
        CodexAccountEnrollmentStartRequest.model_validate(
            {"account_slug": "codex_b", "password": "must-not-be-accepted"}
        )
