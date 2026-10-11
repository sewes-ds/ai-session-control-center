from fastapi.testclient import TestClient
import pytest

from ai_account_manager.core.config import get_settings
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
    assert "upgrade_runtime_root" not in data
    assert "upgrade_gate_runner" not in data


def test_upgrade_mvp_settings_are_bounded_and_do_not_create_runtime_root(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "upgrade"
    monkeypatch.setenv("UPGRADE_RUNTIME_ROOT", str(root))
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.upgrade_runtime_root == root
        assert settings.upgrade_max_repair_cycles == 5
        assert settings.upgrade_runner_timeout_seconds == 300.0
        assert not root.exists()
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("value", ["0", "4", "6"])
def test_upgrade_repair_cycle_limit_is_hard_five(monkeypatch, value: str) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("UPGRADE_MAX_REPAIR_CYCLES", value)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="exactly 5"):
            get_settings()
    finally:
        get_settings.cache_clear()
