from __future__ import annotations

import os

import pytest

from ai_account_manager.core.config import get_settings


_HOST_TELEGRAM_AUTH_ENV = (
    "TELEGRAM_BOT_TOKEN",
    "CUS_TELEGRAM_BOT_TOKEN",
    "CUS_BOT_WEBHOOK_SECRET",
    "CUS_ONBOARDING_TOKEN_KEY",
    "TELEGRAM_ALLOWED_USER_IDS",
)
for _name in _HOST_TELEGRAM_AUTH_ENV:
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def _isolate_host_telegram_auth(monkeypatch: pytest.MonkeyPatch):
    """Keep a developer's live bot credentials out of the test application."""

    for name in _HOST_TELEGRAM_AUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
