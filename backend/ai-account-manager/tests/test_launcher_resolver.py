from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_account_manager.app import create_app
from ai_account_manager.core.config import get_settings
from ai_account_manager.core.launcher import (
    LauncherResolveError,
    build_codex_resume_argv,
    build_codex_start_argv,
    build_project_launch_spec,
)
from ai_account_manager.core.project_settings import upsert_project_settings
from ai_account_manager.core.runtime_launch_specs import (
    get_runtime_launch_spec,
    upsert_runtime_launch_spec,
)
from ai_account_manager.main import app
from ai_account_manager.providers.codex.reasoning_contract import (
    reconcile_reasoning_contract_specs,
)
from ai_account_manager.schemas.codex import (
    CodexCapabilitiesSnapshot,
    CodexCapabilityAccountSection,
    CodexCapabilityModelItem,
    CodexCapabilityModelsSection,
    CodexCapabilityRateLimitsSection,
    CodexCapabilityUsageSection,
)


def _clear_settings() -> None:
    get_settings.cache_clear()


def _settings_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    project_root = tmp_path / "projects"
    profiles_root = tmp_path / "profiles"
    runtime_root = tmp_path / "runtime"
    codex_binary = tmp_path / "bin" / "codex"
    codex_binary.parent.mkdir(parents=True)
    codex_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    codex_binary.chmod(0o700)
    project_root.mkdir()
    for key in (
        "AI_PROJECT_ROOTS",
        "AI_PROFILES_ROOT",
        "PROJECT_SETTINGS_PATH",
        "RUNTIME_LAUNCH_SPECS_PATH",
        "CODEX_CAPABILITIES_CACHE_ROOT",
        "CODEX_BINARY",
        "CUS_PREVIEW_TOKEN",
        "TELEGRAM_BOT_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AI_PROJECT_ROOTS", str(project_root))
    monkeypatch.setenv("AI_PROFILES_ROOT", str(profiles_root))
    monkeypatch.setenv("PROJECT_SETTINGS_PATH", str(runtime_root / "project_settings_v2.json"))
    monkeypatch.setenv("RUNTIME_LAUNCH_SPECS_PATH", str(runtime_root / "runtime_launch_specs_v1.json"))
    monkeypatch.setenv("CODEX_CAPABILITIES_CACHE_ROOT", str(runtime_root / "codex_capabilities"))
    monkeypatch.setenv("CODEX_BINARY", str(codex_binary))
    _clear_settings()
    return {
        "project_root": project_root,
        "profiles_root": profiles_root,
        "runtime_root": runtime_root,
        "codex_binary": codex_binary,
    }


def _profile(profiles_root: Path, account: str = "codex_a") -> Path:
    profile = profiles_root / "codex" / account
    profile.mkdir(parents=True, exist_ok=True)
    (profiles_root / "codex").chmod(0o700)
    profile.chmod(0o700)
    auth = profile / "auth.json"
    auth.write_text(
        '{"tokens":{"access_token":"not-real-access"}}',
        encoding="utf-8",
    )
    auth.chmod(0o600)
    return profile


def _model(
    model_id: str = "gpt-5-codex",
    efforts: list[str] | None = None,
) -> CodexCapabilityModelItem:
    return CodexCapabilityModelItem(
        model_id=model_id,
        display_name="GPT-5 Codex",
        default_reasoning_effort="medium",
        supported_reasoning_efforts=efforts if efforts is not None else ["low", "medium", "high"],
    )


def _write_cache(
    cache_root: Path,
    account_profile: str = "codex_a",
    *,
    status: str = "ok",
    models: list[CodexCapabilityModelItem] | None = None,
) -> None:
    cache_root.mkdir(parents=True, exist_ok=True)
    os.chmod(cache_root, 0o700)
    snapshot = CodexCapabilitiesSnapshot(
        account_profile=account_profile,
        profile_path="/redacted",
        status=status,
        refreshed_at="2026-07-26T00:00:00Z",
        last_success_at="2026-07-26T00:00:00Z",
        profile_integrity_status="unchanged",
        account=CodexCapabilityAccountSection(status="ok", discovered_at="2026-07-26T00:00:00Z"),
        models=CodexCapabilityModelsSection(
            status="ok",
            discovered_at="2026-07-26T00:00:00Z",
            items=models if models is not None else [_model()],
        ),
        rate_limits=CodexCapabilityRateLimitsSection(status="ok"),
        usage=CodexCapabilityUsageSection(status="ok"),
    )
    path = cache_root / f"{account_profile}.json"
    path.write_text(json.dumps(snapshot.model_dump()), encoding="utf-8")
    os.chmod(path, 0o600)


def _complete_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[dict[str, Path], Path, str]:
    paths = _settings_env(monkeypatch, tmp_path)
    project = paths["project_root"] / "alpha"
    project.mkdir()
    _profile(paths["profiles_root"])
    _write_cache(paths["runtime_root"] / "codex_capabilities")
    settings = get_settings()
    record = upsert_project_settings(
        project_path=str(project),
        provider_id="codex",
        account_profile="codex_a",
        model="gpt-5-codex",
        reasoning_effort="medium",
        expected_revision=None,
        settings=settings,
    )
    return paths, project, record.project_id


def test_launcher_resolves_codex_cwd_and_codex_home(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "repos"
    project = project_root / "the-peaks-factory"
    project.mkdir(parents=True)

    profiles_root = tmp_path / "profiles"
    account = profiles_root / "codex" / "codex_a"
    account.mkdir(parents=True)
    (account / "auth.json").write_text(
        '{"tokens":{"access_token":"not-real-token","refresh_token":"not-real-refresh"}}',
        encoding="utf-8",
    )
    (account / "config.toml").write_text("", encoding="utf-8")

    monkeypatch.setenv("AI_PROFILES_ROOT", str(profiles_root))
    monkeypatch.setenv("AI_PROJECT_ROOTS", str(project_root))
    _clear_settings()

    client = TestClient(app)
    response = client.post(
        "/api/launcher/resolve",
        json={"provider_id": "codex", "account_slug": "codex_a", "project_path": str(project)},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["provider_id"] == "codex"
    assert data["account_slug"] == "codex_a"
    assert data["cwd"] == str(project.resolve())
    assert data["command"] == "codex"
    assert data["argv"] == ["codex"]
    assert data["env"] == {"CODEX_HOME": str(account)}
    assert data["profile_path"] == str(account)
    assert data["profile_path_exists"] is True
    assert "not-real-token" not in str(data)
    assert "not-real-refresh" not in str(data)

    _clear_settings()


def test_launcher_rejects_project_outside_allowed_roots(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "repos"
    project_root.mkdir()
    outside_project = tmp_path / "outside" / "project"
    outside_project.mkdir(parents=True)

    profiles_root = tmp_path / "profiles"
    (profiles_root / "codex" / "codex_a").mkdir(parents=True)

    monkeypatch.setenv("AI_PROFILES_ROOT", str(profiles_root))
    monkeypatch.setenv("AI_PROJECT_ROOTS", str(project_root))
    _clear_settings()

    client = TestClient(app)
    response = client.post(
        "/api/launcher/resolve",
        json={
            "provider_id": "codex",
            "account_slug": "codex_a",
            "project_path": str(outside_project),
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "project_outside_allowed_roots"

    _clear_settings()


def test_launcher_rejects_unimplemented_provider(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "repos"
    project = project_root / "project"
    project.mkdir(parents=True)

    monkeypatch.setenv("AI_PROJECT_ROOTS", str(project_root))
    _clear_settings()

    client = TestClient(app)
    response = client.post(
        "/api/launcher/resolve",
        json={
            "provider_id": "claude_code",
            "account_slug": "claude_a",
            "project_path": str(project),
        },
    )

    assert response.status_code == 501
    assert response.json()["detail"]["code"] == "provider_not_implemented"

    _clear_settings()


def test_codex_start_and_resume_argv_are_exact_and_shell_free(tmp_path: Path) -> None:
    codex = str(tmp_path / "codex cli")
    cwd = str(tmp_path / "project with spaces")
    assert build_codex_start_argv(
        codex_binary=codex,
        model="gpt-5.5",
        reasoning_effort="high",
        cwd=cwd,
    ) == [
        codex,
        "--model",
        "gpt-5.5",
        "-c",
        'model_reasoning_effort="high"',
        "--cd",
        cwd,
    ]
    assert build_codex_resume_argv(
        codex_binary=codex,
        model="gpt-5.5",
        reasoning_effort="high",
        session_id="550e8400-e29b-41d4-a716-446655440000",
        cwd=cwd,
    ) == [
        codex,
        "--model",
        "gpt-5.5",
        "-c",
        'model_reasoning_effort="high"',
        "resume",
        "550e8400-e29b-41d4-a716-446655440000",
        "--cd",
        cwd,
    ]
    with pytest.raises(LauncherResolveError):
        build_codex_resume_argv(
            codex_binary=codex,
            model="gpt-5.5",
            reasoning_effort="high",
            session_id="bad;rm",
            cwd=cwd,
        )


def test_resolve_project_settings_returns_stable_immutable_spec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, project, project_id = _complete_settings(monkeypatch, tmp_path)
    client = TestClient(create_app())
    assert not (paths["runtime_root"] / "runtime_launch_specs_v1.json").exists()

    first = client.post(
        "/api/launcher/resolve-project-settings",
        json={"project_id": project_id, "expected_settings_revision": 1},
    )
    second = client.post(
        "/api/launcher/resolve-project-settings",
        json={"project_id": project_id, "expected_settings_revision": 1},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    data = first.json()
    assert second.json()["spec_sha256"] == data["spec_sha256"]
    assert data["spec_version"] == 1
    assert data["provider_id"] == "codex"
    assert data["project_id"] == project_id
    assert data["project_path"] == str(project.resolve())
    assert data["cwd"] == str(project.resolve())
    assert data["account_profile"] == "codex_a"
    assert data["model"] == "gpt-5-codex"
    assert data["reasoning_effort"] == "medium"
    assert data["settings_revision"] == 1
    assert data["command"] == str(paths["codex_binary"].resolve())
    assert data["argv"] == [
        str(paths["codex_binary"].resolve()),
        "--model",
        "gpt-5-codex",
        "-c",
        'model_reasoning_effort="medium"',
        "--cd",
        str(project.resolve()),
    ]
    assert data["env"] == {"CODEX_HOME": str((paths["profiles_root"] / "codex" / "codex_a").resolve())}
    assert data["capability_source"] == "current"
    assert "not-real-access" not in json.dumps(data)
    core = {
        key: data[key]
        for key in (
            "spec_version",
            "provider_id",
            "project_id",
            "project_path",
            "cwd",
            "account_profile",
            "model",
            "reasoning_effort",
            "settings_revision",
            "command",
            "argv",
            "env",
        )
    }
    expected_hash = hashlib.sha256(
        json.dumps(core, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    assert data["spec_sha256"] == expected_hash
    assert not (paths["runtime_root"] / "runtime_launch_specs_v1.json").exists()


def test_launch_spec_hash_changes_on_selected_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, project, project_id = _complete_settings(monkeypatch, tmp_path)
    settings = get_settings()
    base = build_project_launch_spec(project_id=project_id, expected_settings_revision=1, settings=settings)
    changed_project = paths["project_root"] / "beta"
    changed_project.mkdir()
    _write_cache(paths["runtime_root"] / "codex_capabilities", "codex_b")
    _profile(paths["profiles_root"], "codex_b")
    changed = upsert_project_settings(
        project_path=str(changed_project),
        provider_id="codex",
        account_profile="codex_b",
        model="gpt-5-codex",
        reasoning_effort="high",
        expected_revision=None,
        settings=settings,
    )
    other = build_project_launch_spec(
        project_id=changed.project_id,
        expected_settings_revision=1,
        settings=settings,
    )
    assert base.spec_sha256 != other.spec_sha256
    updated = upsert_project_settings(
        project_path=str(project),
        provider_id="codex",
        account_profile="codex_a",
        model="gpt-5-codex",
        reasoning_effort="high",
        expected_revision=1,
        settings=settings,
    )
    revised = build_project_launch_spec(
        project_id=updated.project_id,
        expected_settings_revision=2,
        settings=settings,
    )
    assert base.spec_sha256 != revised.spec_sha256


def test_resolve_project_settings_failures_and_last_known_good_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, project, project_id = _complete_settings(monkeypatch, tmp_path)
    client = TestClient(create_app())
    mismatch = client.post(
        "/api/launcher/resolve-project-settings",
        json={"project_id": project_id, "expected_settings_revision": 2},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "revision_mismatch"

    cache_path = paths["runtime_root"] / "codex_capabilities" / "codex_a.json"
    snapshot = json.loads(cache_path.read_text(encoding="utf-8"))
    snapshot["status"] = "partial"
    snapshot["models"]["status"] = "error"
    cache_path.write_text(json.dumps(snapshot), encoding="utf-8")
    blocked = client.post(
        "/api/launcher/resolve-project-settings",
        json={"project_id": project_id, "expected_settings_revision": 1},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "capabilities_current_required"
    allowed = client.post(
        "/api/launcher/resolve-project-settings",
        json={
            "project_id": project_id,
            "expected_settings_revision": 1,
            "allow_last_known_good": True,
        },
    )
    assert allowed.status_code == 200
    assert allowed.json()["capability_source"] == "last_known_good"

    auth_json = paths["profiles_root"] / "codex" / "codex_a" / "auth.json"
    auth_json.unlink()
    missing_auth = client.post(
        "/api/launcher/resolve-project-settings",
        json={
            "project_id": project_id,
            "expected_settings_revision": 1,
            "allow_last_known_good": True,
        },
    )
    assert missing_auth.status_code == 404
    assert missing_auth.json()["detail"]["code"] == "profile_missing"


def test_resolve_project_settings_rejects_incomplete_provider_model_and_reasoning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, project, project_id = _complete_settings(monkeypatch, tmp_path)
    settings_path = paths["runtime_root"] / "project_settings_v2.json"
    client = TestClient(create_app())

    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    payload["projects"][project_id]["model"] = None
    settings_path.write_text(json.dumps(payload), encoding="utf-8")
    incomplete = client.post(
        "/api/launcher/resolve-project-settings",
        json={"project_id": project_id, "expected_settings_revision": 1},
    )
    assert incomplete.status_code == 409
    assert incomplete.json()["detail"]["code"] == "settings_incomplete"

    payload["projects"][project_id]["model"] = "missing-model"
    settings_path.write_text(json.dumps(payload), encoding="utf-8")
    bad_model = client.post(
        "/api/launcher/resolve-project-settings",
        json={"project_id": project_id, "expected_settings_revision": 1},
    )
    assert bad_model.status_code == 409
    assert bad_model.json()["detail"]["code"] == "model_not_available"

    payload["projects"][project_id]["model"] = "gpt-5-codex"
    payload["projects"][project_id]["reasoning_effort"] = "not-supported"
    settings_path.write_text(json.dumps(payload), encoding="utf-8")
    bad_reasoning = client.post(
        "/api/launcher/resolve-project-settings",
        json={"project_id": project_id, "expected_settings_revision": 1},
    )
    assert bad_reasoning.status_code == 409
    assert bad_reasoning.json()["detail"]["code"] == "reasoning_not_supported"

    payload["projects"][project_id]["reasoning_effort"] = "medium"
    payload["projects"][project_id]["provider_id"] = "claude_code"
    settings_path.write_text(json.dumps(payload), encoding="utf-8")
    bad_provider = client.post(
        "/api/launcher/resolve-project-settings",
        json={"project_id": project_id, "expected_settings_revision": 1},
    )
    assert bad_provider.status_code == 409
    assert bad_provider.json()["detail"]["code"] == "provider_settings_not_supported"


def test_reasoning_contract_reconciles_legacy_spec_without_starting_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, _project_path, project_id = _complete_settings(monkeypatch, tmp_path)
    settings = get_settings()
    current = build_project_launch_spec(
        project_id=project_id,
        expected_settings_revision=1,
        settings=settings,
    )
    upsert_runtime_launch_spec(
        settings.runtime_launch_specs_path,
        {
            "project_id": current.project_id,
            "project_path": current.project_path,
            "provider_id": current.provider_id,
            "account_profile": current.account_profile,
            "model": current.model,
            "reasoning_effort": current.reasoning_effort,
            "settings_revision": current.settings_revision,
            "spec_sha256": hashlib.sha256(b"legacy-reasoning-key").hexdigest(),
            "applied_at": "2026-08-22T00:00:00Z",
            "source": "project_settings_v2",
        },
        None,
    )

    result = reconcile_reasoning_contract_specs(settings)

    assert result["configured_count"] == 1
    assert result["changed_count"] == 1
    reconciled = get_runtime_launch_spec(settings.runtime_launch_specs_path, project_id)
    assert reconciled is not None
    assert reconciled["runtime_revision"] == 2
    assert reconciled["spec_sha256"] == current.spec_sha256
    assert current.argv[3:5] == ["-c", 'model_reasoning_effort="medium"']
    assert not (paths["runtime_root"] / "runtime_apply_transaction_v1.json").exists()
