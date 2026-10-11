from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_subpath_api_base_is_inferred() -> None:
    javascript = (ROOT / "frontend/telegram-miniapp/assets/app.js").read_text(encoding="utf-8")
    assert 'window.location.pathname.startsWith("/cus/")' in javascript
    assert 'const inferredApiBase' in javascript


def test_permanent_nginx_contract() -> None:
    config = (ROOT / "deploy/nginx/ai-session-control-center.conf.template").read_text(
        encoding="utf-8"
    )
    assert "listen 443 ssl http2;" in config
    assert "server_name __CUS_HOST__;" in config
    assert config.count("access_log off;") == 2
    assert "location ^~ __CUS_BASE_PATH__/api/" in config
    assert "proxy_pass http://127.0.0.1:8710/miniapp/;" in config


def test_permanent_systemd_contract() -> None:
    service = (ROOT / "deploy/systemd/ai-session-control-center.service.template").read_text(
        encoding="utf-8"
    )
    assert "EnvironmentFile=__CUS_ENV_FILE__" in service
    assert "WorkingDirectory=__CUS_ROOT__/backend/ai-account-manager" in service
    assert "Restart=on-failure" in service
    assert "python -m uvicorn ai_account_manager.main:app" in service
    assert "--no-access-log" in service
    assert "telegram-ai-agent.service" not in service
