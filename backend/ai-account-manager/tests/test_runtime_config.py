from fastapi.testclient import TestClient

from ai_account_manager.main import app


def test_runtime_public_config_does_not_expose_tokens() -> None:
    client = TestClient(app)
    response = client.get("/api/runtime/config-public")
    assert response.status_code == 200
    data = response.json()
    rendered = str(data).lower()
    assert "token" not in rendered
    assert "secret" not in rendered
    assert "password" not in rendered
    assert data["enabled_providers"]["codex"] is True
