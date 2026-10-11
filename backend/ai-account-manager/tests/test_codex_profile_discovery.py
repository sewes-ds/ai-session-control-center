from fastapi.testclient import TestClient

from ai_account_manager.main import app


def test_codex_accounts_empty_when_profiles_root_missing(monkeypatch, tmp_path) -> None:
    missing_root = tmp_path / "missing_profiles"
    monkeypatch.setenv("AI_PROFILES_ROOT", str(missing_root))

    client = TestClient(app)
    response = client.get("/api/providers/codex/accounts")

    assert response.status_code == 200
    data = response.json()
    assert data["provider_id"] == "codex"
    assert data["root_exists"] is False
    assert data["account_count"] == 0
    assert data["accounts"] == []


def test_codex_accounts_discovery_reads_only_presence_and_safe_metadata(monkeypatch, tmp_path) -> None:
    profiles_root = tmp_path / "profiles"
    codex_root = profiles_root / "codex"
    account_a = codex_root / "codex_plus_a"
    account_b = codex_root / "codex_plus_b"
    account_a.mkdir(parents=True)
    account_b.mkdir(parents=True)

    (account_a / "auth.json").write_text('{"access_token":"SECRET_SHOULD_NOT_LEAK"}', encoding="utf-8")
    (account_a / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    (account_a / "profile.json").write_text('{"label":"Codex Plus A"}', encoding="utf-8")
    (account_b / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")

    monkeypatch.setenv("AI_PROFILES_ROOT", str(profiles_root))

    client = TestClient(app)
    response = client.get("/api/providers/codex/accounts")

    assert response.status_code == 200
    data = response.json()
    assert data["root_exists"] is True
    assert data["account_count"] == 2

    by_slug = {item["account_slug"]: item for item in data["accounts"]}
    assert by_slug["codex_plus_a"]["label"] == "Codex Plus A"
    assert by_slug["codex_plus_a"]["has_auth_json"] is True
    assert by_slug["codex_plus_a"]["has_config_toml"] is True
    assert by_slug["codex_plus_a"]["status"] == "readable"
    assert by_slug["codex_plus_b"]["has_auth_json"] is False
    assert by_slug["codex_plus_b"]["status"] == "missing_auth"

    serialized = response.text
    assert "SECRET_SHOULD_NOT_LEAK" not in serialized
