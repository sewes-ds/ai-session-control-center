from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_account_manager.providers.claude_code import deepseek


def _settings(tmp_path: Path) -> SimpleNamespace:
    credential = tmp_path / "credentials" / "default.token"
    credential.parent.mkdir(mode=0o700)
    credential.write_text("test-secret-token\n", encoding="utf-8")
    os.chmod(credential, 0o600)
    return SimpleNamespace(
        deepseek_api_key_path=credential,
        deepseek_api_base_url="https://api.deepseek.example",
        deepseek_balance_cache_path=tmp_path / "state" / "balance.json",
    )


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return json.dumps(self._payload).encode()


def test_deepseek_profile_and_capabilities_are_safe(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    profiles = deepseek.discover_claude_code_profiles(settings)  # type: ignore[arg-type]
    assert profiles.account_count == 1
    account = profiles.accounts[0]
    assert account.account_slug == "deepseek_default"
    assert account.auth_ready is True
    assert account.status == "readable"
    assert "test-secret-token" not in account.model_dump_json()

    capabilities = deepseek.deepseek_capabilities(
        "deepseek_default",
        settings,  # type: ignore[arg-type]
    )
    assert capabilities.provider_id == "claude_code"
    assert [item.model_id for item in capabilities.models.items] == [
        "deepseek-flash",
        "deepseek-v4-pro",
    ]
    assert all(item.supported_reasoning_efforts == ["max"] for item in capabilities.models.items)


def test_deepseek_balance_refresh_and_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: int) -> _Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(
            {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "USD",
                        "total_balance": "13.00",
                        "granted_balance": "0.00",
                        "topped_up_balance": "13.00",
                    }
                ],
            }
        )

    monkeypatch.setattr(deepseek.urllib.request, "urlopen", fake_urlopen)
    snapshot = deepseek.refresh_balance("deepseek_default", settings)  # type: ignore[arg-type]

    assert snapshot.status == "ok"
    assert snapshot.is_available is True
    assert snapshot.balance_infos[0].total_balance == "13.00"
    assert settings.deepseek_balance_cache_path.stat().st_mode & 0o777 == 0o600
    cached = deepseek.read_cached_balance("deepseek_default", settings)  # type: ignore[arg-type]
    assert cached == snapshot
    request = captured["request"]
    assert getattr(request, "full_url") == "https://api.deepseek.example/user/balance"
    assert getattr(request, "headers")["Authorization"] == "Bearer test-secret-token"


def test_deepseek_rejects_group_readable_secret(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    os.chmod(settings.deepseek_api_key_path, 0o640)

    with pytest.raises(deepseek.DeepSeekProviderError) as caught:
        deepseek.read_api_key(settings)  # type: ignore[arg-type]

    assert caught.value.code == "invalid_key_file"


def test_miniapp_exposes_provider_switch_and_real_balance() -> None:
    root = Path(__file__).resolve().parents[3]
    html = (root / "frontend/telegram-miniapp/index.html").read_text(encoding="utf-8")
    javascript = (root / "frontend/telegram-miniapp/assets/app.js").read_text(encoding="utf-8")

    assert 'id="settingsProviderSelect"' in html
    assert 'id="onboardingProvider"' in html
    assert '<option value="claude_code">Claude Code / DeepSeek</option>' in html
    assert "/api/providers/claude_code/accounts" in javascript
    assert "/balance/refresh" in javascript
    assert "Реальный API-баланс" in javascript
