from __future__ import annotations

import json
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

from ai_account_manager.core.config import AppSettings
from ai_account_manager.providers.codex import app_server
from ai_account_manager.providers.codex.app_server import run_codex_app_server_probe


def _settings(tmp_path: Path, *, timeout: float = 2.0) -> AppSettings:
    return AppSettings(
        app_env="test",
        app_host="127.0.0.1",
        app_port=8710,
        ai_profiles_root=tmp_path / "profiles",
        ai_quota_cache_root=tmp_path / "quota_cache",
        runtime_data_root=tmp_path / "runtime",
        project_inventory_path=tmp_path / "runtime" / "project_inventory_v1.json",
        project_activity_path=tmp_path / "runtime" / "project_activity_v1.json",
        project_bindings_path=tmp_path / "runtime" / "project_bindings.json",
        telegram_ai_agent_root=tmp_path / "telegram-ai-agent",
        telegram_ai_agent_topic_config_path=tmp_path / "telegram-ai-agent" / "topic_config.json",
        telegram_ai_agent_tmux_state_path=tmp_path / "telegram-ai-agent" / "tmux_sessions" / "state.json",
        telegram_ai_agent_compatibility_manifest=tmp_path / "compatibility.json",
        codex_usage_endpoint="https://example.invalid/usage",
        codex_binary=tmp_path / "bin" / "codex",
        codex_auth_enrollment_root=tmp_path / "runtime" / "codex_auth_enrollments",
        codex_auth_enrollment_ttl_seconds=900,
        codex_auth_stale_after_seconds=691200,
        enable_provider_codex=True,
        enable_provider_claude_code=False,
        enable_provider_gemini_cli=False,
        codex_capabilities_cache_root=tmp_path / "runtime" / "codex_capabilities",
        codex_app_server_timeout_seconds=timeout,
    )


def _profile(settings: AppSettings, slug: str = "codex_a") -> Path:
    profile = settings.codex_profiles_root / slug
    profile.mkdir(parents=True)
    (profile / "auth.json").write_text('{"opaque":true}\n', encoding="utf-8")
    (profile / "config.toml").write_text("model='x'\n", encoding="utf-8")
    (profile / "history.jsonl").write_text("{}\n", encoding="utf-8")
    return profile


def _fake_server(tmp_path: Path) -> Path:
    script = tmp_path / "fake_codex_server.py"
    script.write_text(
        textwrap.dedent(
            r'''
            import json
            import os
            import pathlib
            import sys
            import time

            mode = os.environ.get("FAKE_MODE", "ok")
            log_path = pathlib.Path(os.environ["FAKE_LOG"])
            home = pathlib.Path(os.environ["CODEX_HOME"])
            sequence = []

            def write_log():
                payload = {
                    "home": str(home),
                    "entries": sorted(p.name for p in home.iterdir()),
                    "auth_mode": oct((home / "auth.json").stat().st_mode & 0o777),
                    "env_keys": sorted(os.environ),
                    "sequence": sequence,
                }
                tmp = log_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload), encoding="utf-8")
                tmp.replace(log_path)

            def respond(rid, result=None, error=None):
                if error is not None:
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "error": error}) + "\n")
                else:
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
                sys.stdout.flush()
                write_log()

            if mode == "timeout":
                time.sleep(10)
                raise SystemExit(0)

            for line in sys.stdin:
                request = json.loads(line)
                method = request.get("method")
                sequence.append(method)
                rid = request.get("id")
                if rid is None:
                    write_log()
                    continue
                if mode == "malformed" and method == "initialize":
                    sys.stdout.write("{not-json\n")
                    sys.stdout.flush()
                if mode == "oversized" and method == "initialize":
                    sys.stdout.write("x" * (2 * 1024 * 1024 + 2) + "\n")
                    sys.stdout.flush()
                    continue
                if method == "initialize":
                    respond(rid, {"capabilities": {}})
                elif method == "account/read":
                    respond(rid, {"requiresOpenaiAuth": False, "account": {"email": "User@Test.Example", "type": "chatgpt", "planType": "plus"}})
                elif method == "account/rateLimits/read":
                    if mode == "partial":
                        respond(rid, error={"code": "rate_failed", "message": "contains Bearer should-not-leak-token-1234567890"})
                    else:
                        respond(rid, {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 25.0, "windowDurationMins": 300}}, "rateLimitResetCredits": {"availableCount": 1, "credits": [{"id": "raw-credit-id", "resetType": "codexRateLimits", "status": "available", "grantedAt": 1, "expiresAt": 2}]}})
                elif method == "account/usage/read":
                    respond(rid, {"summary": {"tokens": 10}, "dailyUsageBuckets": [{"day": "2026-07-26", "tokens": 10}]})
                elif method == "model/list":
                    cursor = request.get("params", {}).get("cursor")
                    if mode == "empty":
                        respond(rid, {"data": [], "nextCursor": None})
                    elif mode == "repeated":
                        respond(rid, {"data": [{"id": "m1", "model": "m1", "displayName": "M1", "hidden": False, "isDefault": True, "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}], "nextCursor": "again"})
                    elif cursor:
                        respond(rid, {"data": [{"id": "m2", "model": "m2", "displayName": "M2", "hidden": False, "isDefault": False, "defaultReasoningEffort": "high", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}], "nextCursor": None})
                    else:
                        respond(rid, {"data": [{"id": "m1", "model": "m1", "displayName": "M1", "hidden": False, "isDefault": True, "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}], "nextCursor": "cursor-2"})
            '''
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _runner(
    fake_server: Path,
    log_path: Path,
    *,
    mode: str = "ok",
) -> tuple[Any, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def factory(argv, **kwargs):
        calls.append({"argv": argv, "env": kwargs["env"]})
        env = dict(kwargs["env"])
        env["FAKE_LOG"] = str(log_path)
        env["FAKE_MODE"] = mode
        return subprocess.Popen([sys.executable, str(fake_server)], **{**kwargs, "env": env})

    return factory, calls


def test_app_server_uses_temp_codex_home_and_auth_only(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profile = _profile(settings)
    fake = _fake_server(tmp_path)
    log_path = tmp_path / "server-log.json"
    monkeypatch.setattr(app_server, "CODEX_APP_SERVER_BINARY", fake)
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-inherit")
    factory, calls = _runner(fake, log_path)

    result = run_codex_app_server_probe("codex_a", settings=settings, process_factory=factory)
    log = json.loads(log_path.read_text(encoding="utf-8"))

    assert calls[0]["argv"] == [str(fake), "app-server", "--listen", "stdio://"]
    assert calls[0]["env"]["CODEX_HOME"] == log["home"]
    assert "OPENAI_API_KEY" not in calls[0]["env"]
    assert log["entries"] == ["auth.json"]
    assert log["auth_mode"] == "0o600"
    assert (profile / "config.toml").exists()
    assert result.profile_integrity_status == "unchanged"
    assert result.temp_home_removed is True


def test_app_server_request_sequence_pagination_and_no_consume(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _profile(settings)
    fake = _fake_server(tmp_path)
    log_path = tmp_path / "server-log.json"
    monkeypatch.setattr(app_server, "CODEX_APP_SERVER_BINARY", fake)
    factory, _calls = _runner(fake, log_path)

    result = run_codex_app_server_probe("codex_a", settings=settings, process_factory=factory)
    log = json.loads(log_path.read_text(encoding="utf-8"))

    assert result.methods["models"].status == "ok"
    assert len(result.methods["models"].result["data"]) == 2
    assert result.request_methods_sent == [
        "initialize",
        "initialized",
        "account/read",
        "model/list",
        "model/list",
        "account/rateLimits/read",
        "account/usage/read",
    ]
    assert "account/rateLimitResetCredit/consume" not in result.request_methods_sent
    assert result.request_methods_sent[: len(log["sequence"])] == log["sequence"]


def test_app_server_method_failures_are_partial_and_stderr_sanitized(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    _profile(settings)
    fake = _fake_server(tmp_path)
    monkeypatch.setattr(app_server, "CODEX_APP_SERVER_BINARY", fake)
    factory, _calls = _runner(fake, tmp_path / "server-log.json", mode="partial")

    result = run_codex_app_server_probe("codex_a", settings=settings, process_factory=factory)

    assert result.methods["account"].status == "ok"
    assert result.methods["rate_limits"].status == "error"
    assert result.methods["models"].status == "ok"
    assert "should-not-leak-token" not in result.stderr_safe


def test_app_server_rejects_bad_model_pagination(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _profile(settings)
    fake = _fake_server(tmp_path)
    monkeypatch.setattr(app_server, "CODEX_APP_SERVER_BINARY", fake)

    factory, _calls = _runner(fake, tmp_path / "empty-log.json", mode="empty")
    empty = run_codex_app_server_probe("codex_a", settings=settings, process_factory=factory)
    assert empty.methods["models"].error_code == "empty_models"

    factory, _calls = _runner(fake, tmp_path / "repeated-log.json", mode="repeated")
    repeated = run_codex_app_server_probe("codex_a", settings=settings, process_factory=factory)
    assert repeated.methods["models"].error_code == "repeated_cursor"


def test_app_server_malformed_timeout_and_oversized_fail_safely(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    _profile(settings)
    fake = _fake_server(tmp_path)
    monkeypatch.setattr(app_server, "CODEX_APP_SERVER_BINARY", fake)

    factory, _calls = _runner(fake, tmp_path / "malformed-log.json", mode="malformed")
    malformed = run_codex_app_server_probe("codex_a", settings=settings, process_factory=factory)
    assert malformed.methods["models"].status == "ok"

    factory, _calls = _runner(fake, tmp_path / "oversized-log.json", mode="oversized")
    oversized = run_codex_app_server_probe("codex_a", settings=settings, process_factory=factory)
    assert oversized.error_code == "stdout_line_too_large"

    timeout_settings = _settings(tmp_path / "timeout-case", timeout=0.2)
    _profile(timeout_settings)
    factory, _calls = _runner(fake, tmp_path / "timeout-log.json", mode="timeout")
    timeout = run_codex_app_server_probe("codex_a", settings=timeout_settings, process_factory=factory)
    assert timeout.error_code == "timeout"
    assert timeout.temp_home_removed is True
