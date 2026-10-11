import json
import os
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


def _run_node(script: str) -> object:
    env = os.environ.copy()
    env["TZ"] = "UTC"
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )
    return json.loads(result.stdout)


def test_exact_local_reset_datetime_and_relative_details_execute() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    functions = "\n".join(
        _javascript_function(javascript, name)
        for name in (
            "plural",
            "parseQuotaTimestamp",
            "formatLocalDateTime",
            "formatFutureDuration",
            "formatQuotaResetRelative",
            "quotaResetDetails",
        )
    )
    payload = _run_node(
        "\n".join(
            [
                functions,
                'Date.now = () => new Date("2026-07-30T17:17:00Z").getTime();',
                'const value = "2026-08-05T11:59:00Z";',
                "console.log(JSON.stringify({",
                "  exact: formatLocalDateTime(value),",
                "  relative: formatQuotaResetRelative(value),",
                "  details: quotaResetDetails(value),",
                "  seconds: parseQuotaTimestamp(1785931140),",
                "  milliseconds: parseQuotaTimestamp(1785931140000),",
                '  iso: parseQuotaTimestamp("2026-08-05T11:59:00Z"),',
                "  unknown: quotaResetDetails(null),",
                '  now: formatQuotaResetRelative("2026-07-30T17:17:30Z"),',
                '  past: formatQuotaResetRelative("2026-07-30T17:15:00Z"),',
                "}));",
            ]
        )
    )

    assert payload["exact"] == "5 авг. 2026, 11:59"
    assert payload["relative"] == "Через 5 дней 18 ч 42 мин"
    assert payload["details"]["datetime"] == "2026-08-05T11:59:00.000Z"
    assert payload["seconds"] == payload["milliseconds"] == payload["iso"]
    assert payload["unknown"]["relative"] == "Время сброса неизвестно"
    assert payload["now"] == "Сброс сейчас"
    assert payload["past"] == "Расчётное время сброса наступило"
    assert "0 мин" not in json.dumps(payload, ensure_ascii=False)


def test_quota_reset_markup_uses_accessible_time_and_past_guidance() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    markup = _javascript_function(javascript, "quotaResetDetailsMarkup")
    row = _javascript_function(javascript, "quotaRow")

    assert '<time datetime="${escapeHtml(details.datetime)}">' in markup
    assert "Сброс: ${escapeHtml(details.exact)}" in markup
    assert "Обновите лимиты" in markup
    assert "quotaResetDetailsMarkup(resetAt)" in row
    assert row.count("quotaResetDetailsMarkup(resetAt)") == 2


def test_credit_sorting_expiry_and_default_title_execute() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    functions = "\n".join(
        _javascript_function(javascript, name)
        for name in (
            "plural",
            "parseQuotaTimestamp",
            "formatLocalDateTime",
            "formatFutureDuration",
            "resetCreditTitle",
            "resetCreditExpiryState",
            "sortedResetCredits",
        )
    )
    payload = _run_node(
        "\n".join(
            [
                functions,
                'Date.now = () => new Date("2026-07-30T12:00:00Z").getTime();',
                "const credits = [",
                ' {title: "Unknown", expires_at: null},',
                ' {title: "", expires_at: "2026-08-02T12:00:00Z"},',
                ' {title: "Soon A", expires_at: "2026-07-30T20:00:00Z"},',
                ' {title: "Soon B", expires_at: "2026-07-30T20:00:00Z"},',
                "];",
                "console.log(JSON.stringify({",
                " order: sortedResetCredits(credits).map((credit) => resetCreditTitle(credit)),",
                " warning: resetCreditExpiryState(credits[2].expires_at),",
                ' expired: resetCreditExpiryState("2026-07-30T11:00:00Z"),',
                " unknown: resetCreditExpiryState(null),",
                "}));",
            ]
        )
    )

    assert payload["order"] == [
        "Soon A",
        "Soon B",
        "Полный сброс лимитов",
        "Unknown",
    ]
    assert payload["warning"]["status"] == "warning"
    assert payload["expired"]["status"] == "expired"
    assert payload["expired"]["relative"] == "Срок действия истёк — обновите лимиты"
    assert payload["unknown"]["status"] == "unknown"
    assert payload["unknown"]["relative"] == "Срок действия не указан"


def test_credit_rows_count_only_partial_and_opaque_selection_execute() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    function_names = (
        "plural",
        "parseQuotaTimestamp",
        "formatLocalDateTime",
        "formatFutureDuration",
        "quotaFreshnessPresentation",
        "quotaSectionHeading",
        "resetCreditTitle",
        "resetCreditExpiryState",
        "sortedResetCredits",
        "registerResetCreditAction",
        "resetCreditRow",
        "resetCreditsBlock",
    )
    functions = "\n".join(
        _javascript_function(javascript, name) for name in function_names
    )
    payload = _run_node(
        "\n".join(
            [
                "function escapeHtml(value) { return String(value ?? '')",
                " .replaceAll('&', '&amp;').replaceAll('<', '&lt;')",
                " .replaceAll('>', '&gt;').replaceAll('\"', '&quot;'); }",
                "const state = {",
                " resetCreditActions: new Map(), resetCreditActionCounter: 0,",
                " accountDetails: new Map(),",
                "};",
                functions,
                'Date.now = () => new Date("2026-07-30T12:00:00Z").getTime();',
                "const detailed = {status: 'ok', reset_credits: {available_count: 4, credits: [",
                " {credit_id: 'opaque-later', title: '', expires_at: '2026-08-03T12:00:00Z'},",
                " {credit_id: 'opaque-soon', title: 'Скоро', expires_at: '2026-07-30T18:00:00Z'},",
                " {credit_id: 'opaque-expired', title: 'Истёк', expires_at: '2026-07-29T12:00:00Z'},",
                "]}};",
                "const detailedHtml = resetCreditsBlock('account', detailed, {status: 'fresh'});",
                "const selections = Array.from(state.resetCreditActions.values());",
                "state.resetCreditActions.clear(); state.resetCreditActionCounter = 0;",
                "const countOnlyHtml = resetCreditsBlock('account',",
                " {status: 'ok', reset_credits: {available_count: 2, credits: null}},",
                " {status: 'fresh'});",
                "const generic = Array.from(state.resetCreditActions.values())[0];",
                "state.resetCreditActions.clear(); state.resetCreditActionCounter = 0;",
                "const zeroHtml = resetCreditsBlock('account',",
                " {status: 'ok', reset_credits: {available_count: 0, credits: []}},",
                " {status: 'fresh'});",
                "const nullHtml = resetCreditsBlock('account',",
                " {status: 'ok', reset_credits: {available_count: null, credits: null}},",
                " {status: 'fresh'});",
                "console.log(JSON.stringify({",
                " detailedHtml, selections, countOnlyHtml, generic, zeroHtml, nullHtml,",
                "}));",
            ]
        )
    )

    detailed_html = payload["detailedHtml"]
    assert detailed_html.count('class="reset-credit-row ') == 3
    assert "Полный сброс лимитов" in detailed_html
    assert "Истекает менее чем через сутки" in detailed_html
    assert "Срок действия истёк — обновите лимиты" in detailed_html
    assert "Ещё без подробностей: 2" in detailed_html
    assert "opaque-later" not in detailed_html
    assert "opaque-soon" not in detailed_html
    assert "opaque-expired" not in detailed_html
    assert len(payload["selections"]) == 3
    assert payload["selections"][0]["credit"]["credit_id"] == "opaque-soon"
    assert "Доступно сбросов: 2" in payload["countOnlyHtml"]
    assert "Подробные сроки действия не получены" in payload["countOnlyHtml"]
    assert payload["countOnlyHtml"].count("reset-credit-row") == 0
    assert payload["generic"]["credit"]["credit_id"] is None
    assert "Доступных сохранённых сбросов нет" in payload["zeroHtml"]
    assert "data-reset-credit-action" not in payload["zeroHtml"]
    assert "Данные о сохранённых сбросах не получены" in payload["nullHtml"]
    assert "data-reset-credit-action" not in payload["nullHtml"]


def test_per_credit_dialog_and_confirm_contracts_are_bounded() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    opening = _javascript_function(javascript, "openResetCreditDialog")
    confirm = _javascript_function(javascript, "confirmResetCredit")

    assert "state.resetCreditActions.get" in opening
    assert opening.count("crypto.randomUUID()") == 1
    assert "apiRequest" not in opening
    assert "Будет использован следующий доступный сохранённый сброс." in opening
    assert "всему аккаунту и всем проектам" in opening
    assert "один сохранённый сброс может быть израсходован" in opening
    assert "state.resetDialog.busy" in confirm
    assert confirm.count("/rate-limit-reset-credits/consume") == 1
    assert "idempotency_key: state.resetDialog.idempotencyKey" in confirm
    assert "credit_id: state.resetDialog.credit?.credit_id || null" in confirm
    assert "if (response.quota)" in confirm
    assert "applyAccountQuotaSnapshot(accountSlug, response.quota" in confirm
    assert "await loadCachedQuotaForAccount(accountSlug)" in confirm
    assert "refreshAll" not in confirm
    assert "quotaRefreshPath" not in confirm


def test_duplicate_confirmation_uses_one_request_and_same_key() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    confirm = f"async {_javascript_function(javascript, 'confirmResetCredit')}"
    payload = _run_node(
        "\n".join(
            [
                "const state = {",
                " resetDialog: {accountSlug: 'account', credit: {credit_id: 'selected'},",
                " idempotencyKey: 'same-key', busy: false}, mutationInFlight: false,",
                "};",
                "const elements = {",
                " confirmResetCreditButton: {disabled: false}, resetCreditError: {}, accountsStatus: {},",
                "};",
                "let calls = 0; let bodies = [];",
                "async function apiRequest(_path, options) {",
                " calls += 1; bodies.push(JSON.parse(options.body));",
                " await new Promise((resolve) => setTimeout(resolve, 2));",
                " return {success: true, quota: {status: 'ok'}};",
                "}",
                "function setNotice() {}",
                "function closeResetCreditDialog() {}",
                "function applyAccountQuotaSnapshot() {}",
                "function isAuthUnavailableError() { return false; }",
                "function markQuotaRefreshFailure() {}",
                "function renderAccountCards() {}",
                "async function loadCachedQuotaForAccount() {}",
                confirm,
                "(async () => {",
                " await Promise.all([confirmResetCredit(), confirmResetCredit()]);",
                " console.log(JSON.stringify({calls, bodies}));",
                "})().catch((error) => { console.error(error); process.exit(1); });",
            ]
        )
    )

    assert payload == {
        "calls": 1,
        "bodies": [{"idempotency_key": "same-key", "credit_id": "selected"}],
    }


def test_initial_refresh_is_once_per_auth_generation_and_non_repeating() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    loader = _javascript_function(javascript, "loadPrivateDataForCurrentAuth")
    initial = _javascript_function(javascript, "refreshInitialAccountQuotas")
    visibility = _javascript_function(javascript, "handleVisibilityChange")
    activated = _javascript_function(javascript, "handleTelegramActivated")
    switch_tab = _javascript_function(javascript, "switchTab")
    poll = _javascript_function(javascript, "pollProjects")

    assert "state.quotaAutoRefreshGeneration !== generation" in loader
    assert "state.quotaAutoRefreshGeneration = generation" in loader
    assert "state.quotaInitialRefreshInFlight = true" in loader
    assert "void refreshInitialAccountQuotas(generation)" in loader
    assert 'loadAccounts({ initialQuotaRefresh })' in loader
    assert 'state.authStatus !== "ready"' in initial
    assert "state.authReadyGeneration !== generation" in initial
    assert "runBoundedWorkers(selected" in initial
    assert initial.count("quotaRefreshPath(account.account_slug)") == 1
    assert "retry" not in initial.lower()
    for unrelated in (visibility, activated, switch_tab, poll):
        assert "refreshInitialAccountQuotas" not in unrelated
        assert "quotaRefreshPath" not in unrelated


def test_initial_refresh_skips_unreadable_and_preserves_partial_success() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    functions = "\n".join(
        [
            _javascript_function(javascript, "authorizedAccounts"),
            _javascript_function(javascript, "quotaRefreshPath"),
            f"async {_javascript_function(javascript, 'runBoundedWorkers')}",
            f"async {_javascript_function(javascript, 'refreshInitialAccountQuotas')}",
        ]
    )
    payload = _run_node(
        "\n".join(
            [
                "const state = {authStatus: 'ready', authReadyGeneration: 7,",
                " quotaInitialRefreshGeneration: 7, quotaInitialRefreshInFlight: true, accounts: [",
                " {account_slug: 'one', has_auth_json: true, status: 'readable'},",
                " {account_slug: 'two', has_auth_json: true, status: 'readable'},",
                " {account_slug: 'skip-auth', has_auth_json: false, status: 'readable'},",
                " {account_slug: 'skip-status', has_auth_json: true, status: 'unreadable'},",
                "]};",
                "let active = 0; let maxActive = 0; const attempts = {};",
                "const applied = []; const failures = [];",
                "async function apiRequest(path, options) {",
                " if (options.method !== 'POST') throw new Error('wrong method');",
                " const slug = path.split('/').at(-3);",
                " attempts[slug] = (attempts[slug] || 0) + 1;",
                " active += 1; maxActive = Math.max(maxActive, active);",
                " await new Promise((resolve) => setTimeout(resolve, 2)); active -= 1;",
                " if (slug === 'two') throw new Error('partial');",
                " return {status: 'ok', account_slug: slug};",
                "}",
                "function applyAccountQuotaSnapshot(slug) { applied.push(slug); }",
                "function isAuthUnavailableError() { return false; }",
                "function markQuotaRefreshFailure(slug) { failures.push(slug); }",
                "function renderAccountCards() {}",
                "function updateRefreshButtonContext() {}",
                functions,
                "(async () => {",
                " await refreshInitialAccountQuotas(7);",
                " console.log(JSON.stringify({attempts, applied, failures, maxActive, inFlight: state.quotaInitialRefreshInFlight}));",
                "})().catch((error) => { console.error(error); process.exit(1); });",
            ]
        )
    )

    assert payload["attempts"] == {"one": 1, "two": 1}
    assert payload["applied"] == ["one"]
    assert payload["failures"] == ["two"]
    assert payload["maxActive"] == 2
    assert payload["inFlight"] is False


def test_freshness_states_do_not_present_pending_cache_as_fresh() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    presentation = _javascript_function(
        javascript, "quotaFreshnessPresentation"
    )
    quota_block = _javascript_function(javascript, "accountQuotaBlock")

    assert "Получаем актуальные лимиты…" in presentation
    assert "Актуальные данные" in presentation
    assert "Не удалось получить свежие данные" in presentation
    assert "Показаны последние данные от" in presentation
    assert "Свежие данные недоступны" in presentation
    pending_index = quota_block.index('presentation.status === "pending"')
    rows_index = quota_block.index("for (const bucket of quotaSnapshot.buckets")
    assert pending_index < rows_index
    assert "Получаем актуальные лимиты…" in quota_block[:rows_index]


def test_manual_and_project_refresh_boundaries_remain_separate() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    manual_all = _javascript_function(javascript, "refreshAllAccountQuotas")
    manual_one = _javascript_function(javascript, "refreshQuota")
    projects = _javascript_function(javascript, "refreshProjectsState")

    assert "runBoundedWorkers(selected" in manual_all
    assert manual_all.count("quotaRefreshPath(account.account_slug)") == 1
    assert manual_one.count("quotaRefreshPath(accountSlug)") == 1
    assert "applyAccountQuotaSnapshot(accountSlug, quota" in manual_one
    assert "quotaRefreshPath" not in projects
    assert "/quota/refresh" not in projects


def test_responsive_styles_and_cache_version_are_present() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")
    css = (MINIAPP / "assets" / "app.css").read_text(encoding="utf-8")

    assert "./assets/app.css?v=cus-runtime-onboarding-v1" in html
    assert "./assets/app.js?v=cus-runtime-authority-v1" in html
    assert "https://telegram.org/js/telegram-web-app.js?63" in html
    assert "@media (max-width: 380px)" in css
    assert "@media (prefers-color-scheme: dark)" in css
    assert ".quota-section-heading" in css
    assert ".freshness-pending" in css
    assert ".reset-credit-list" in css
    assert "overflow-wrap: anywhere" in css
