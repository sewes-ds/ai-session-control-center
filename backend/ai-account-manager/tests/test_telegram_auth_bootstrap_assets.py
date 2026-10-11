import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MINIAPP = ROOT / "frontend" / "telegram-miniapp"


def _javascript_function(source: str, name: str) -> str:
    start = source.index(f"function {name}")
    brace_start = source.index(") {", start) + 2
    depth = 0
    for index in range(brace_start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"{name} function body not found")


def test_official_sdk_precedes_versioned_application_assets() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")

    sdk = 'https://telegram.org/js/telegram-web-app.js?63'
    application = "./assets/app.js?v=cus-runtime-authority-v1"
    assert sdk in html
    assert html.index(sdk) < html.index(application)
    assert "./assets/app.css?v=cus-runtime-onboarding-v1" in html


def test_dynamic_signed_init_data_sources_and_priority_execute() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    functions = "\n".join(
        _javascript_function(javascript, name)
        for name in (
            "getTelegramWebApp",
            "getTelegramSdkInitData",
            "launchParamValue",
            "getLaunchUrlInitData",
            "getCurrentTelegramInitData",
        )
    )
    script = "\n".join(
        [
            '"use strict";',
            "let window = {",
            '  Telegram: { WebApp: { initData: "sdk-signed" } },',
            '  location: { hash: "#tgWebAppData=hash%252Bsigned", search: "?tgWebAppData=search-signed" },',
            "};",
            functions,
            "const firstObject = getTelegramWebApp();",
            "const sdkPriority = getCurrentTelegramInitData();",
            "window.Telegram = undefined;",
            "const hashPriority = getCurrentTelegramInitData();",
            'window.location.hash = "#other=value";',
            "const searchFallback = getCurrentTelegramInitData();",
            'window.location.hash = "#tgWebAppData=";',
            'window.location.search = "?tgWebAppData=";',
            "const empty = getCurrentTelegramInitData();",
            'window.location.hash = "#tgWebAppData=broken%";',
            "const malformed = getCurrentTelegramInitData();",
            'window.Telegram = { WebApp: { initData: "late-signed" } };',
            "const dynamicObject = getTelegramWebApp() !== firstObject;",
            "console.log(JSON.stringify({",
            "  sdkPriority, hashPriority, searchFallback, empty, malformed, dynamicObject,",
            "}));",
        ]
    )
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert json.loads(result.stdout) == {
        "sdkPriority": "sdk-signed",
        "hashPriority": "hash%2Bsigned",
        "searchFallback": "search-signed",
        "empty": "",
        "malformed": "",
        "dynamicObject": True,
    }


def test_auth_sources_are_signed_bounded_and_not_persisted() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert "const telegram = window.Telegram?.WebApp;" not in javascript
    assert "window.Telegram?.WebApp || null" in _javascript_function(
        javascript, "getTelegramWebApp"
    )
    assert ".initData;" in _javascript_function(javascript, "getTelegramSdkInitData")
    assert "tgWebAppData" in _javascript_function(javascript, "launchParamValue")
    assert "decodeURIComponent" not in javascript
    assert "initDataUnsafe" not in javascript
    assert "tgWebAppUser" not in javascript
    assert "localStorage" not in javascript
    assert 'setItem(previewTokenKey, previewTokenFromUrl)' in javascript
    assert "setItem(previewTokenKey, initData)" not in javascript
    assert "setItem(\"tgWebAppData\"" not in javascript


def test_bootstrap_guards_initial_load_retry_and_recovery_events() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    bootstrap = _javascript_function(javascript, "bootstrapTelegramAuth")
    retry_tail = javascript[javascript.index(
        'elements.retryAuthButton.addEventListener("click"'
    ) :]

    assert "AUTH_WAIT_TIMEOUT_MS = 3000" in javascript
    assert "AUTH_POLL_INTERVAL_MS = 50" in javascript
    assert 'authStatus: previewToken ? "ready" : "pending"' in javascript
    assert "if (state.authBootstrapPromise)" in bootstrap
    assert 'setAuthStatus("pending")' in bootstrap
    assert 'setAuthStatus("unavailable")' in bootstrap
    assert 'setAuthStatus("ready")' in bootstrap
    assert "await loadPrivateDataForCurrentAuth()" in bootstrap
    assert 'if (state.authStatus === "pending") return;' in retry_tail
    assert 'onEvent("activated", handleTelegramActivated)' in javascript
    assert 'offEvent("activated", handleTelegramActivated)' in javascript
    assert 'document.addEventListener("visibilitychange", handleVisibilityChange)' in javascript
    assert 'window.addEventListener("pagehide", cleanupPage)' in javascript


def test_protected_requests_fail_locally_and_401_stops_polling() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    request = _javascript_function(javascript, "apiRequest")
    poll = _javascript_function(javascript, "scheduleProjectPoll")

    assert request.index("getCurrentTelegramInitData()") < request.index(
        "fetch(apiUrl(path)"
    )
    assert 'transitionTelegramAuthFailure("unavailable")' in request
    assert "throw new AuthUnavailableError()" in request
    assert 'headers.set("X-Telegram-Init-Data", initData)' in request
    assert request.index('if (previewToken)') < request.index(
        "getCurrentTelegramInitData()"
    )
    assert 'headers.set("X-CUS-Preview-Token", previewToken)' in request
    assert 'response.status === 401 && !previewToken' in request
    assert 'transitionTelegramAuthFailure("rejected")' in request
    assert "stopProjectPolling()" in _javascript_function(javascript, "setAuthStatus")
    assert 'state.authStatus !== "ready"' in poll
    assert "stopProjectPolling()" in poll


def test_api_request_auth_guard_executes_before_network() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    request = f"async {_javascript_function(javascript, 'apiRequest')}"
    script = "\n".join(
        [
            '"use strict";',
            "let previewToken = '';",
            "let currentInitData = '';",
            "let fetchCalls = 0;",
            "let responseStatus = 200;",
            "let lastHeaders = null;",
            "const transitions = [];",
            "class AuthUnavailableError extends Error {",
            "  constructor({ status = null } = {}) { super('auth'); this.status = status; }",
            "}",
            "function apiUrl(path) { return path; }",
            "function getCurrentTelegramInitData() { return currentInitData; }",
            "function transitionTelegramAuthFailure(status) { transitions.push(status); }",
            "async function fetch(_url, options) {",
            "  fetchCalls += 1;",
            "  lastHeaders = options.headers;",
            "  return {",
            "    ok: responseStatus === 200,",
            "    status: responseStatus,",
            "    json: async () => ({ detail: 'denied' }),",
            "  };",
            "}",
            request,
            "(async () => {",
            "  let localFailure = false;",
            "  try { await apiRequest('/missing'); } catch (error) {",
            "    localFailure = error instanceof AuthUnavailableError;",
            "  }",
            "  currentInitData = 'signed-value';",
            "  await apiRequest('/signed');",
            "  const telegramHeader = lastHeaders.get('X-Telegram-Init-Data');",
            "  const telegramPreviewHeader = lastHeaders.get('X-CUS-Preview-Token');",
            "  previewToken = 'preview-value';",
            "  await apiRequest('/preview');",
            "  const previewHeader = lastHeaders.get('X-CUS-Preview-Token');",
            "  const previewTelegramHeader = lastHeaders.get('X-Telegram-Init-Data');",
            "  previewToken = '';",
            "  responseStatus = 401;",
            "  try { await apiRequest('/rejected'); } catch (error) {}",
            "  console.log(JSON.stringify({",
            "    localFailure, fetchCalls, telegramHeader, telegramPreviewHeader,",
            "    previewHeader, previewTelegramHeader, transitions,",
            "  }));",
            "})();",
        ]
    )
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert json.loads(result.stdout) == {
        "localFailure": True,
        "fetchCalls": 3,
        "telegramHeader": "signed-value",
        "telegramPreviewHeader": None,
        "previewHeader": "preview-value",
        "previewTelegramHeader": None,
        "transitions": ["unavailable", "rejected"],
    }


def test_recovery_ui_and_single_load_poll_guards_are_present() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    loader = _javascript_function(javascript, "loadPrivateDataForCurrentAuth")
    scheduler = _javascript_function(javascript, "scheduleProjectPoll")

    assert "Не удалось получить авторизацию Telegram" in html
    assert "Закройте Mini App и откройте его заново из сообщения CUS-бота." in html
    assert "Можно также повторить проверку авторизации." in html
    assert "Повторить проверку" in html
    assert "state.loadedAuthGeneration === generation" in loader
    assert "state.loadedAuthGeneration = generation" in loader
    assert scheduler.count("state.projectPoll = setTimeout") == 1
    assert "stopProjectPolling()" in scheduler


def test_bootstrap_diagnostic_is_sanitized() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    diagnostic = _javascript_function(javascript, "telegramAuthDiagnostic")

    assert '"CUS_TELEGRAM_AUTH_BOOTSTRAP"' in diagnostic
    assert diagnostic.count("Boolean(") == 5
    assert "sdk_object_present" in diagnostic
    assert "sdk_init_data_present" in diagnostic
    assert "url_init_data_present" in diagnostic
    assert "telegram_version_present" in diagnostic
    assert "platform_present" in diagnostic
    assert "auth_status: authStatus" in diagnostic
    assert "attempt," in diagnostic
    assert ".initData," not in diagnostic
    assert "window.location" not in diagnostic
    assert "previewToken" not in diagnostic
