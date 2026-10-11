from fastapi.testclient import TestClient

from ai_account_manager.main import app


def test_health() -> None:
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}


def test_providers() -> None:
    client = TestClient(app)
    response = client.get("/api/providers")
    assert response.status_code == 200
    data = response.json()
    provider_ids = {item["provider_id"] for item in data}
    assert "codex" in provider_ids
    assert "claude_code" in provider_ids
    assert "gemini_cli" in provider_ids


def test_provider_detail() -> None:
    client = TestClient(app)
    response = client.get("/api/providers/codex")
    assert response.status_code == 200
    assert response.json()["provider_id"] == "codex"


def test_unknown_provider_returns_404() -> None:
    client = TestClient(app)
    response = client.get("/api/providers/unknown")
    assert response.status_code == 404
