from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ai_account_manager.app import create_app
from ai_account_manager.core.config import get_settings


def _build_static_root(root: Path) -> Path:
    assets = root / "assets"
    assets.mkdir(parents=True)
    (root / "index.html").write_text("<html>preview</html>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('ok')", encoding="utf-8")
    return root


def test_miniapp_is_served_same_origin(monkeypatch, tmp_path: Path) -> None:
    static_root = _build_static_root(tmp_path / "miniapp")
    monkeypatch.setenv("MINIAPP_STATIC_ROOT", str(static_root))
    monkeypatch.delenv("CUS_PREVIEW_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        assert client.get("/miniapp/").status_code == 200
        assert client.get("/miniapp/assets/app.js").status_code == 200
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/miniapp/"
    finally:
        get_settings.cache_clear()


def test_preview_token_protects_api_but_not_static(monkeypatch, tmp_path: Path) -> None:
    static_root = _build_static_root(tmp_path / "miniapp")
    monkeypatch.setenv("MINIAPP_STATIC_ROOT", str(static_root))
    monkeypatch.setenv("CUS_PREVIEW_TOKEN", "preview-secret")
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        assert client.get("/health").status_code == 200
        assert client.get("/miniapp/").status_code == 200
        assert client.get("/api/providers").status_code == 401
        assert client.get("/openapi.json").status_code == 401
        assert (
            client.get(
                "/api/providers",
                headers={"X-CUS-Preview-Token": "preview-secret"},
            ).status_code
            == 200
        )
        assert client.get("/api/providers?preview_token=preview-secret").status_code == 200
    finally:
        get_settings.cache_clear()


def test_production_without_auth_fails_closed_for_protected_routes(
    monkeypatch, tmp_path: Path
) -> None:
    static_root = _build_static_root(tmp_path / "miniapp")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("MINIAPP_STATIC_ROOT", str(static_root))
    monkeypatch.delenv("CUS_PREVIEW_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        for path in ["/api/providers", "/openapi.json", "/docs", "/redoc"]:
            response = client.get(path)
            assert response.status_code == 503
            assert response.json() == {"detail": "Production authentication is not configured"}
        assert client.get("/health").status_code == 200
        assert client.get("/miniapp/").status_code == 200
    finally:
        get_settings.cache_clear()


def test_development_without_auth_preserves_existing_behavior(monkeypatch, tmp_path: Path) -> None:
    static_root = _build_static_root(tmp_path / "miniapp")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("MINIAPP_STATIC_ROOT", str(static_root))
    monkeypatch.delenv("CUS_PREVIEW_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        assert client.get("/api/providers").status_code != 503
    finally:
        get_settings.cache_clear()
