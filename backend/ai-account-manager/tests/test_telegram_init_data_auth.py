from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from ai_account_manager.app import create_app
from ai_account_manager.core.config import get_settings
from ai_account_manager.auth.telegram import validate_telegram_init_data

BOT_TOKEN = "123456:test-token"
USER_ID = 777001
AUTH_DATE = 1_700_000_000


def _signed_init_data(*, user_id: int = USER_ID, auth_date: int = AUTH_DATE) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAE-test-query",
        "user": json.dumps(
            {"id": user_id, "first_name": "Eugene", "username": "tester"},
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def _build_static_root(root: Path) -> Path:
    assets = root / "assets"
    assets.mkdir(parents=True)
    (root / "index.html").write_text("<html>telegram</html>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('ok')", encoding="utf-8")
    return root


def test_validator_accepts_valid_init_data() -> None:
    principal = validate_telegram_init_data(
        _signed_init_data(),
        bot_token=BOT_TOKEN,
        max_age_seconds=3600,
        now=AUTH_DATE + 30,
    )
    assert principal.user_id == USER_ID
    assert principal.username == "tester"


def test_validator_rejects_tampered_init_data() -> None:
    raw = _signed_init_data().replace("Eugene", "Mallory")
    try:
        validate_telegram_init_data(
            raw,
            bot_token=BOT_TOKEN,
            max_age_seconds=3600,
            now=AUTH_DATE + 30,
        )
    except ValueError as exc:
        assert "signature" in str(exc)
    else:
        raise AssertionError("tampered initData was accepted")


def test_api_accepts_allowed_telegram_user(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MINIAPP_STATIC_ROOT", str(_build_static_root(tmp_path / "miniapp")))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", str(USER_ID))
    monkeypatch.setenv("TELEGRAM_INIT_DATA_MAX_AGE_SECONDS", "999999999")
    monkeypatch.delenv("CUS_PREVIEW_TOKEN", raising=False)
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        headers = {"X-Telegram-Init-Data": _signed_init_data()}
        assert client.get("/api/providers", headers=headers).status_code == 200
        response = client.get("/api/auth/telegram", headers=headers)
        assert response.status_code == 200
        assert response.json()["user"]["id"] == USER_ID
    finally:
        get_settings.cache_clear()


def test_api_rejects_unlisted_telegram_user(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MINIAPP_STATIC_ROOT", str(_build_static_root(tmp_path / "miniapp")))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "999999")
    monkeypatch.setenv("TELEGRAM_INIT_DATA_MAX_AGE_SECONDS", "999999999")
    monkeypatch.delenv("CUS_PREVIEW_TOKEN", raising=False)
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        response = client.get(
            "/api/providers",
            headers={"X-Telegram-Init-Data": _signed_init_data()},
        )
        assert response.status_code == 403
    finally:
        get_settings.cache_clear()


def test_preview_token_remains_available_for_browser_preview(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MINIAPP_STATIC_ROOT", str(_build_static_root(tmp_path / "miniapp")))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", str(USER_ID))
    monkeypatch.setenv("CUS_PREVIEW_TOKEN", "preview-secret")
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        response = client.get(
            "/api/providers",
            headers={"X-CUS-Preview-Token": "preview-secret"},
        )
        assert response.status_code == 200
    finally:
        get_settings.cache_clear()
