from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_account_manager.app import create_app
from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.core.launcher import build_project_launch_spec
from ai_account_manager.core.project_settings import (
    ProjectSettingsError,
    canonical_project_path,
    migrate_project_settings_v2,
    project_id_for_path,
    read_project_settings_store,
    upsert_project_settings,
    validate_provider_project_settings,
)
from ai_account_manager.core.runtime_launch_specs import upsert_runtime_launch_spec
from ai_account_manager.integrations.session_runtime import (
    upsert_project_binding,
)
from ai_account_manager.schemas.codex import (
    CodexCapabilitiesSnapshot,
    CodexCapabilityAccountSection,
    CodexCapabilityModelItem,
    CodexCapabilityModelsSection,
    CodexCapabilityRateLimitsSection,
    CodexCapabilityUsageSection,
)


def _settings(tmp_path: Path) -> AppSettings:
    bot_root = tmp_path / "telegram-ai-agent"
    runtime = tmp_path / "runtime"
    return AppSettings(
        app_env="test",
        app_host="127.0.0.1",
        app_port=8710,
        ai_profiles_root=tmp_path / "profiles",
        ai_quota_cache_root=tmp_path / "quota_cache",
        runtime_data_root=runtime,
        project_inventory_path=runtime / "project_inventory_v1.json",
        project_activity_path=runtime / "project_activity_v1.json",
        project_bindings_path=runtime / "project_bindings.json",
        telegram_ai_agent_root=bot_root,
        telegram_ai_agent_topic_config_path=bot_root / "topic_config.json",
        telegram_ai_agent_tmux_state_path=bot_root / "tmux_sessions" / "state.json",
        telegram_ai_agent_compatibility_manifest=tmp_path / "compatibility.json",
        codex_usage_endpoint="https://chatgpt.com/backend-api/wham/usage",
        codex_binary=tmp_path / "bin" / "codex",
        codex_auth_enrollment_root=runtime / "codex_auth_enrollments",
        codex_auth_enrollment_ttl_seconds=900,
        codex_auth_stale_after_seconds=691200,
        enable_provider_codex=True,
        enable_provider_claude_code=False,
        enable_provider_gemini_cli=False,
        ai_project_roots=(tmp_path / "projects",),
        codex_capabilities_cache_root=runtime / "codex_capabilities",
        project_settings_path=runtime / "project_settings_v2.json",
        runtime_launch_specs_path=runtime / "runtime_launch_specs_v1.json",
    )


def _project(settings: AppSettings, name: str = "alpha") -> Path:
    path = settings.ai_project_roots[0] / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _profile(settings: AppSettings, slug: str) -> Path:
    profile = settings.codex_profiles_root / slug
    profile.mkdir(parents=True, exist_ok=True)
    settings.codex_profiles_root.chmod(0o700)
    profile.chmod(0o700)
    auth = profile / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    auth.chmod(0o600)
    return profile


def _snapshot(
    account_profile: str,
    models: list[CodexCapabilityModelItem],
    *,
    status: str = "ok",
    model_status: str = "ok",
) -> CodexCapabilitiesSnapshot:
    return CodexCapabilitiesSnapshot(
        account_profile=account_profile,
        profile_path="/redacted",
        status=status,
        refreshed_at="2026-07-26T00:00:00Z",
        last_success_at="2026-07-26T00:00:00Z",
        profile_integrity_status="unchanged",
        account=CodexCapabilityAccountSection(status="ok", discovered_at="2026-07-26T00:00:00Z"),
        models=CodexCapabilityModelsSection(
            status=model_status,
            discovered_at="2026-07-26T00:00:00Z",
            items=models,
        ),
        rate_limits=CodexCapabilityRateLimitsSection(status="ok"),
        usage=CodexCapabilityUsageSection(status="ok"),
    )


def _write_cache(
    settings: AppSettings,
    account_profile: str,
    models: list[CodexCapabilityModelItem],
    *,
    status: str = "ok",
    model_status: str = "ok",
) -> None:
    cache_root = settings.codex_capabilities_cache_root
    cache_root.mkdir(parents=True, exist_ok=True)
    os.chmod(cache_root, 0o700)
    (cache_root / f"{account_profile}.json").write_text(
        json.dumps(_snapshot(account_profile, models, status=status, model_status=model_status).model_dump()),
        encoding="utf-8",
    )
    os.chmod(cache_root / f"{account_profile}.json", 0o600)


def _model(
    model_id: str = "gpt-5-codex",
    efforts: list[str] | None = None,
    *,
    default: str | None = "medium",
) -> CodexCapabilityModelItem:
    return CodexCapabilityModelItem(
        model_id=model_id,
        display_name="GPT-5 Codex",
        default_reasoning_effort=default,
        supported_reasoning_efforts=efforts if efforts is not None else ["low", "medium", "high"],
    )


def _env_app(monkeypatch: pytest.MonkeyPatch, settings: AppSettings) -> TestClient:
    for key in (
        "PROJECT_SETTINGS_PATH",
        "PROJECT_BINDINGS_PATH",
        "AI_PROFILES_ROOT",
        "RUNTIME_DATA_ROOT",
        "CODEX_CAPABILITIES_CACHE_ROOT",
        "AI_PROJECT_ROOTS",
        "RUNTIME_LAUNCH_SPECS_PATH",
        "CODEX_BINARY",
        "CUS_PREVIEW_TOKEN",
        "TELEGRAM_BOT_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PROJECT_SETTINGS_PATH", str(settings.project_settings_path))
    monkeypatch.setenv("PROJECT_BINDINGS_PATH", str(settings.project_bindings_path))
    monkeypatch.setenv("AI_PROFILES_ROOT", str(settings.ai_profiles_root))
    monkeypatch.setenv("RUNTIME_DATA_ROOT", str(settings.runtime_data_root))
    monkeypatch.setenv("CODEX_CAPABILITIES_CACHE_ROOT", str(settings.codex_capabilities_cache_root))
    monkeypatch.setenv("AI_PROJECT_ROOTS", ":".join(str(path) for path in settings.ai_project_roots))
    monkeypatch.setenv("RUNTIME_LAUNCH_SPECS_PATH", str(settings.runtime_launch_specs_path))
    monkeypatch.setenv("CODEX_BINARY", str(settings.codex_binary))
    get_settings.cache_clear()
    return TestClient(create_app())


def test_deterministic_project_id_and_path_enforcement(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    project = _project(settings)
    expected = hashlib.sha256(str(project.resolve()).encode("utf-8")).hexdigest()[:20]
    assert project_id_for_path(project) == f"project_{expected}"
    assert canonical_project_path(str(project), settings) == project.resolve()
    with pytest.raises(ProjectSettingsError, match="Project path is outside"):
        canonical_project_path(str(tmp_path / "other"), settings)


def test_store_modes_symlink_rejection_cas_and_secret_free_payload(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    project = _project(settings)
    _profile(settings, "codex_a")
    _write_cache(settings, "codex_a", [_model()])

    record = upsert_project_settings(
        project_path=str(project),
        provider_id="codex",
        account_profile="codex_a",
        model="gpt-5-codex",
        reasoning_effort="medium",
        expected_revision=None,
        settings=settings,
    )
    assert record.revision == 1
    assert settings.project_settings_path.parent.stat().st_mode & 0o777 == 0o700
    assert settings.project_settings_path.stat().st_mode & 0o777 == 0o600
    assert (settings.project_settings_path.parent / "project_settings_v2.lock").stat().st_mode & 0o777 == 0o600
    payload = json.loads(settings.project_settings_path.read_text(encoding="utf-8"))
    assert payload["store_revision"] == 2
    assert "profile_path" not in json.dumps(payload)
    assert "auth" not in json.dumps(payload).lower()

    updated = upsert_project_settings(
        project_path=str(project),
        provider_id="codex",
        account_profile="codex_a",
        model="gpt-5-codex",
        reasoning_effort="high",
        expected_revision=1,
        settings=settings,
    )
    assert updated.revision == 2
    with pytest.raises(ProjectSettingsError) as exc_info:
        upsert_project_settings(
            project_path=str(project),
            provider_id="codex",
            account_profile="codex_a",
            model="gpt-5-codex",
            reasoning_effort="low",
            expected_revision=1,
            settings=settings,
        )
    assert exc_info.value.code == "revision_conflict"

    symlink_settings = _settings(tmp_path / "symlink")
    symlink_settings.runtime_data_root.mkdir(parents=True)
    target = symlink_settings.runtime_data_root / "target.json"
    target.write_text("{}", encoding="utf-8")
    symlink_settings.project_settings_path.symlink_to(target)
    with pytest.raises(ProjectSettingsError) as exc_info:
        read_project_settings_store(symlink_settings)
    assert exc_info.value.code == "invalid_settings_path"


def test_invalid_values_and_store_quarantine(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    project = _project(settings)
    _profile(settings, "codex_a")
    _write_cache(settings, "codex_a", [_model()])
    with pytest.raises(ProjectSettingsError):
        upsert_project_settings(
            project_path=str(project),
            provider_id="codex",
            account_profile="bad.profile",
            model="gpt-5-codex",
            reasoning_effort="medium",
            expected_revision=None,
            settings=settings,
        )
    with pytest.raises(ProjectSettingsError):
        upsert_project_settings(
            project_path=str(project),
            provider_id="codex",
            account_profile="codex_a",
            model="bad\x01model",
            reasoning_effort="medium",
            expected_revision=None,
            settings=settings,
        )
    settings.project_settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings.project_settings_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProjectSettingsError) as exc_info:
        read_project_settings_store(settings)
    assert exc_info.value.code == "settings_store_invalid"
    assert not settings.project_settings_path.exists()
    assert list(settings.project_settings_path.parent.glob("project_settings_v2.json.*.invalid"))


def test_atomic_write_uses_fsync_and_concurrent_writers_preserve_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    first = _project(settings, "first")
    second = _project(settings, "second")
    _profile(settings, "codex_a")
    _write_cache(settings, "codex_a", [_model()])
    fsync_calls = 0
    real_fsync = os.fsync

    def counted_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        real_fsync(fd)

    monkeypatch.setattr("ai_account_manager.core.project_settings.os.fsync", counted_fsync)

    def write(path: Path) -> None:
        upsert_project_settings(
            project_path=str(path),
            provider_id="codex",
            account_profile="codex_a",
            model="gpt-5-codex",
            reasoning_effort="medium",
            expected_revision=None,
            settings=settings,
        )

    threads = [threading.Thread(target=write, args=(path,)) for path in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    store, _ = read_project_settings_store(settings)
    assert set(store["projects"]) == {project_id_for_path(first), project_id_for_path(second)}
    assert fsync_calls >= 4


def test_migration_imports_valid_v1_skips_invalid_and_preserves_legacy(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    first = _project(settings, "first")
    second = _project(settings, "second")
    settings.project_bindings_path.parent.mkdir(parents=True)
    legacy = {
        "version": 1,
        "bindings": {
            str(first): {
                "provider_id": "codex",
                "account_slug": "codex_a",
                "profile_path": "/redacted/a",
                "updated_at": "2026-07-25T01:02:03Z",
            },
            str(second): {
                "provider_id": "codex",
                "account_slug": "codex_b",
                "profile_path": "/redacted/b",
                "updated_at": "2026-07-25T04:05:06Z",
            },
            "../bad": {"provider_id": "codex", "account_slug": "bad.profile"},
        },
    }
    settings.project_bindings_path.write_text(json.dumps(legacy), encoding="utf-8")
    legacy_sha = hashlib.sha256(settings.project_bindings_path.read_bytes()).hexdigest()
    result = migrate_project_settings_v2(settings)
    assert result.status == "partial"
    assert result.imported_count == 2
    assert result.skipped_count == 1
    assert hashlib.sha256(settings.project_bindings_path.read_bytes()).hexdigest() == legacy_sha
    store, _ = read_project_settings_store(settings)
    records = {item["account_profile"]: item for item in store["projects"].values()}
    assert set(records) == {"codex_a", "codex_b"}
    assert records["codex_a"]["model"] is None
    assert records["codex_a"]["reasoning_effort"] is None
    assert records["codex_a"]["updated_at"] == "2026-07-25T01:02:03Z"
    assert migrate_project_settings_v2(settings).status == "already_migrated"


def test_migration_empty_when_legacy_missing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    result = migrate_project_settings_v2(settings)
    assert result.status == "empty_created"
    store, _ = read_project_settings_store(settings)
    assert store["migrated_from"]["source_sha256"] is None
    assert store["projects"] == {}


def test_codex_validation_account_scoped_model_reasoning_and_cache_states(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _profile(settings, "codex_a")
    _profile(settings, "codex_b")
    _write_cache(settings, "codex_a", [_model("account-a-model", ["low"])])
    _write_cache(settings, "codex_b", [_model("account-b-model", ["high"])])
    valid = validate_provider_project_settings(
        provider_id="codex",
        account_profile="codex_a",
        model="account-a-model",
        reasoning_effort="low",
        settings=settings,
    )
    assert valid.status == "valid"
    assert valid.capability_source == "current"
    with pytest.raises(ProjectSettingsError) as exc_info:
        validate_provider_project_settings(
            provider_id="codex",
            account_profile="codex_b",
            model="account-a-model",
            reasoning_effort="low",
            settings=settings,
        )
    assert exc_info.value.code == "model_not_available"
    with pytest.raises(ProjectSettingsError) as exc_info:
        validate_provider_project_settings(
            provider_id="codex",
            account_profile="codex_a",
            model="account-a-model",
            reasoning_effort="high",
            settings=settings,
        )
    assert exc_info.value.code == "reasoning_not_supported"
    with pytest.raises(ProjectSettingsError) as exc_info:
        validate_provider_project_settings(
            provider_id="claude_code",
            account_profile="codex_a",
            model="account-a-model",
            reasoning_effort="low",
            settings=settings,
        )
    assert exc_info.value.code == "provider_settings_not_supported"


def test_codex_validation_uses_current_models_when_quota_probe_is_partial(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _profile(settings, "codex_a")
    _write_cache(
        settings,
        "codex_a",
        [_model("account-a-model", ["high"])],
        status="partial",
        model_status="ok",
    )

    valid = validate_provider_project_settings(
        provider_id="codex",
        account_profile="codex_a",
        model="account-a-model",
        reasoning_effort="high",
        settings=settings,
    )

    assert valid.status == "valid"
    assert valid.capability_source == "current"
    assert valid.warnings == []


def test_codex_validation_lkg_empty_no_default_and_profile_auth_missing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _profile(settings, "codex_a")
    _write_cache(
        settings,
        "codex_a",
        [_model("lkg-model", ["medium"])],
        status="partial",
        model_status="error",
    )
    lkg = validate_provider_project_settings(
        provider_id="codex",
        account_profile="codex_a",
        model="lkg-model",
        reasoning_effort="medium",
        settings=settings,
    )
    assert lkg.capability_source == "last_known_good"
    assert lkg.warnings == ["last_known_good"]
    _profile(settings, "codex_empty")
    _write_cache(settings, "codex_empty", [], status="ok")
    with pytest.raises(ProjectSettingsError) as exc_info:
        validate_provider_project_settings(
            provider_id="codex",
            account_profile="codex_empty",
            model="none",
            reasoning_effort="medium",
            settings=settings,
        )
    assert exc_info.value.code == "capabilities_refresh_required"
    _profile(settings, "codex_default")
    _write_cache(settings, "codex_default", [_model("default-only", [], default="low")])
    with pytest.raises(ProjectSettingsError) as exc_info:
        validate_provider_project_settings(
            provider_id="codex",
            account_profile="codex_default",
            model="default-only",
            reasoning_effort="medium",
            settings=settings,
        )
    assert exc_info.value.code == "reasoning_not_supported"
    with pytest.raises(ProjectSettingsError) as exc_info:
        validate_provider_project_settings(
            provider_id="codex",
            account_profile="missing",
            model="x",
            reasoning_effort="low",
            settings=settings,
        )
    assert exc_info.value.code == "profile_missing"
    profile = _profile(settings, "codex_noauth")
    (profile / "auth.json").unlink()
    with pytest.raises(ProjectSettingsError) as exc_info:
        validate_provider_project_settings(
            provider_id="codex",
            account_profile="codex_noauth",
            model="x",
            reasoning_effort="low",
            settings=settings,
        )
    assert exc_info.value.code == "profile_missing"


def test_api_absent_validate_put_runtime_statuses_and_legacy_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    project = _project(settings)
    _profile(settings, "codex_a")
    _write_cache(settings, "codex_a", [_model()])
    binding = upsert_project_binding(
        project_path=str(project),
        provider_id="codex",
        account_slug="codex_a",
        settings=settings,
    )
    legacy_sha = hashlib.sha256(settings.project_bindings_path.read_bytes()).hexdigest()
    client = _env_app(monkeypatch, settings)

    absent = client.get("/api/sessions/project-settings")
    assert absent.status_code == 200
    assert absent.json()["migration_required"] is True
    assert not settings.project_settings_path.exists()

    validation = client.post(
        "/api/sessions/project-settings/validate",
        json={
            "project_path": str(project),
            "provider_id": "codex",
            "account_profile": "codex_a",
            "model": "gpt-5-codex",
            "reasoning_effort": "medium",
        },
    )
    assert validation.status_code == 200
    assert not settings.project_settings_path.exists()

    created = client.put(
        "/api/sessions/project-settings",
        json={
            "project_path": str(project),
            "provider_id": "codex",
            "account_profile": "codex_a",
            "model": "gpt-5-codex",
            "reasoning_effort": "medium",
            "expected_revision": None,
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["persisted"] is True
    assert body["applied_to_runtime"] is False
    assert body["runtime_account_profile"] == binding.account_slug
    assert body["runtime_account_matches"] is True
    assert body["runtime_application_status"] == "account_matches_settings_pending"
    assert hashlib.sha256(settings.project_bindings_path.read_bytes()).hexdigest() == legacy_sha

    conflict = client.put(
        "/api/sessions/project-settings",
        json={
            "project_path": str(project),
            "provider_id": "codex",
            "account_profile": "codex_a",
            "model": "gpt-5-codex",
            "reasoning_effort": "high",
            "expected_revision": None,
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "revision_conflict"
    listed = client.get("/api/sessions/project-settings").json()
    assert listed["project_count"] == 1
    single = client.get(f"/api/sessions/project-settings/{body['project_id']}")
    assert single.status_code == 200
    assert single.json()["project_id"] == body["project_id"]

    bindings_response = client.get("/api/sessions/bindings")
    assert bindings_response.status_code == 200
    assert bindings_response.json()["binding_count"] == 1
    switch_response = client.post(
        "/api/sessions/switch-account",
        json={"project_path": str(project), "account_slug": "codex_a", "confirm_interrupt": False},
    )
    assert switch_response.status_code in {404, 409}


def test_runtime_comparison_incomplete_account_differs_and_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    project = _project(settings)
    other = _project(settings, "other")
    _profile(settings, "codex_a")
    _profile(settings, "codex_b")
    _write_cache(settings, "codex_b", [_model()])
    upsert_project_binding(
        project_path=str(project),
        provider_id="codex",
        account_slug="codex_a",
        settings=settings,
    )
    migrate_project_settings_v2(settings)
    store = json.loads(settings.project_settings_path.read_text(encoding="utf-8"))
    imported = next(iter(store["projects"].values()))
    assert imported["model"] is None
    client = _env_app(monkeypatch, settings)
    incomplete = client.get("/api/sessions/project-settings").json()["projects"][0]
    assert incomplete["runtime_application_status"] == "incomplete"
    created = client.put(
        "/api/sessions/project-settings",
        json={
            "project_path": str(project),
            "provider_id": "codex",
            "account_profile": "codex_b",
            "model": "gpt-5-codex",
            "reasoning_effort": "medium",
            "expected_revision": 1,
        },
    )
    assert created.status_code == 200
    assert created.json()["runtime_application_status"] == "account_differs"
    _write_cache(settings, "codex_b", [_model()])
    unknown = client.put(
        "/api/sessions/project-settings",
        json={
            "project_path": str(other),
            "provider_id": "codex",
            "account_profile": "codex_b",
            "model": "gpt-5-codex",
            "reasoning_effort": "medium",
            "expected_revision": None,
        },
    )
    assert unknown.status_code == 200
    assert unknown.json()["runtime_application_status"] == "runtime_unknown"


def _runtime_record(settings: AppSettings, project_id: str, expected_revision: int = 1) -> dict[str, object]:
    spec = build_project_launch_spec(
        project_id=project_id,
        expected_settings_revision=expected_revision,
        allow_last_known_good=True,
        settings=settings,
    )
    return {
        "project_id": spec.project_id,
        "project_path": spec.project_path,
        "provider_id": spec.provider_id,
        "account_profile": spec.account_profile,
        "model": spec.model,
        "reasoning_effort": spec.reasoning_effort,
        "settings_revision": spec.settings_revision,
        "spec_sha256": spec.spec_sha256,
        "applied_at": "2026-07-27T00:00:00Z",
        "source": "project_settings_v2",
    }


def test_runtime_view_applied_exact_match_and_stale_variants(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    project = _project(settings)
    _profile(settings, "codex_a")
    _profile(settings, "codex_b")
    _write_cache(settings, "codex_a", [_model()])
    record = upsert_project_settings(
        project_path=str(project),
        provider_id="codex",
        account_profile="codex_a",
        model="gpt-5-codex",
        reasoning_effort="medium",
        expected_revision=None,
        settings=settings,
    )
    upsert_project_binding(
        project_path=str(project),
        provider_id="codex",
        account_slug="codex_a",
        settings=settings,
    )
    upsert_runtime_launch_spec(
        settings.runtime_launch_specs_path,
        _runtime_record(settings, record.project_id),
        None,
    )
    client = _env_app(monkeypatch, settings)
    applied = client.get(f"/api/sessions/project-settings/{record.project_id}").json()
    assert applied["runtime_application_status"] == "applied"
    assert applied["runtime_account_profile"] == "codex_a"
    assert applied["runtime_model"] == "gpt-5-codex"
    assert applied["runtime_reasoning_effort"] == "medium"
    assert applied["runtime_settings_revision"] == 1
    assert isinstance(applied["runtime_spec_sha256"], str)

    stale_record = _runtime_record(settings, record.project_id)
    stale_record["model"] = "other-model"
    stale_record["spec_sha256"] = "0" * 64
    upsert_runtime_launch_spec(settings.runtime_launch_specs_path, stale_record, 1)
    stale = client.get(f"/api/sessions/project-settings/{record.project_id}").json()
    assert stale["runtime_application_status"] == "runtime_spec_stale"
    assert stale["runtime_model"] == "other-model"

    upsert_project_binding(
        project_path=str(project),
        provider_id="codex",
        account_slug="codex_b",
        settings=settings,
    )
    stale_account = client.get(f"/api/sessions/project-settings/{record.project_id}").json()
    assert stale_account["runtime_application_status"] == "runtime_spec_stale"
    assert stale_account["runtime_account_matches"] is False


def test_runtime_store_invalid_and_absent_get_are_non_mutating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    project = _project(settings)
    _profile(settings, "codex_a")
    _write_cache(settings, "codex_a", [_model()])
    record = upsert_project_settings(
        project_path=str(project),
        provider_id="codex",
        account_profile="codex_a",
        model="gpt-5-codex",
        reasoning_effort="medium",
        expected_revision=None,
        settings=settings,
    )
    upsert_project_binding(
        project_path=str(project),
        provider_id="codex",
        account_slug="codex_a",
        settings=settings,
    )
    desired_sha = hashlib.sha256(settings.project_settings_path.read_bytes()).hexdigest()
    binding_sha = hashlib.sha256(settings.project_bindings_path.read_bytes()).hexdigest()
    client = _env_app(monkeypatch, settings)

    absent = client.get("/api/sessions/project-settings")
    assert absent.status_code == 200
    assert not settings.runtime_launch_specs_path.exists()
    assert hashlib.sha256(settings.project_settings_path.read_bytes()).hexdigest() == desired_sha
    assert hashlib.sha256(settings.project_bindings_path.read_bytes()).hexdigest() == binding_sha

    settings.runtime_launch_specs_path.parent.mkdir(parents=True, exist_ok=True)
    settings.runtime_launch_specs_path.write_text("{not json", encoding="utf-8")
    invalid_sha = hashlib.sha256(settings.runtime_launch_specs_path.read_bytes()).hexdigest()
    invalid = client.get(f"/api/sessions/project-settings/{record.project_id}")
    assert invalid.status_code == 200
    assert invalid.json()["runtime_application_status"] == "runtime_spec_invalid"
    assert settings.runtime_launch_specs_path.exists()
    assert hashlib.sha256(settings.runtime_launch_specs_path.read_bytes()).hexdigest() == invalid_sha
    assert not list(settings.runtime_launch_specs_path.parent.glob("runtime_launch_specs_v1.json.*.invalid"))
