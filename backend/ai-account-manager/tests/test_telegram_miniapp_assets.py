import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MINIAPP = ROOT / "frontend" / "telegram-miniapp"


def _javascript_function(source: str, name: str) -> str:
    start = source.index(f"function {name}")
    brace_start = source.index("{", start)
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


def test_miniapp_assets_exist() -> None:
    assert (MINIAPP / "index.html").is_file()
    assert (MINIAPP / "assets" / "app.css").is_file()
    assert (MINIAPP / "assets" / "app.js").is_file()


def test_project_card_contract_is_user_facing() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert "Проекты" in html
    assert "Аккаунты" in html
    assert "Настроить запуск" in javascript
    assert "Сменить агента" in javascript
    assert "tmux-сессия" in javascript
    assert "Настройки запуска" in javascript
    assert "Command:" not in html
    assert "Command:" not in javascript
    assert "Next account" not in html
    assert "Next account" not in javascript
    assert "Runtime:" not in html
    assert "Runtime:" not in javascript


def test_native_enrollment_has_no_password_field() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert 'type="password"' not in html
    assert "Пароль OpenAI не вводится" in html
    assert "/api/providers/codex/account-enrollments" in javascript


def test_project_runtime_settings_ui_replaces_legacy_switch_flow() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert "/api/sessions/project-settings" in javascript
    assert "/api/sessions/project-settings/validate" in javascript
    assert "/api/sessions/project-settings/apply-selection" in javascript
    assert "/capabilities" in javascript
    assert "/capabilities/refresh" in javascript
    assert "/api/sessions/switch-account" not in javascript
    assert "switchAccountDialog" not in html
    assert "state.switchProject" not in javascript
    assert "state.switchAccountSlug" not in javascript
    assert "Сменить аккаунт</button>" not in javascript
    assert html.count(">Применить</button>") == 1
    assert "Сохранить" not in html
    assert "Проверить и применить" not in html
    assert "Применить и перезапустить" not in html
    assert ">Подтвердить</button>" in html
    assert "После подтверждения проект будет запущен с выбранными настройками." in html
    assert "Текущая задача и очередь проекта, если они есть, будут прерваны." in html
    assert 'id="settingsAccountSelect"' in html
    assert 'id="settingsModelSelect"' in html
    assert 'id="settingsReasoningSelect"' in html


def test_agent_switch_remains_deferred() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert "Сменить агента</button>" in javascript
    assert "Смена агента будет реализована отдельно" in javascript


def test_settings_merge_keeps_live_and_next_launch_values_separate() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert "Promise.all([" in javascript
    assert 'apiRequest("/api/sessions/projects")' in javascript
    assert 'apiRequest("/api/sessions/project-settings")' in javascript
    assert "settingsByPath.get(project.project_path)" in javascript
    assert "project_path" in javascript
    assert "topic_name" in javascript
    assert "account_profile" in javascript
    assert "runtime_account_profile" in javascript
    assert "runtime_model" in javascript
    assert "runtime_reasoning_effort" in javascript
    assert "runtime_settings_revision" in javascript
    assert "runtime_application_status" in javascript
    assert "runtime_selection_status" in javascript
    assert "current_model" in javascript
    assert "current_reasoning_effort" in javascript
    assert "Текущие параметры" in javascript
    assert "При следующем запуске" in javascript
    assert "Желаемые значения" not in javascript
    assert "Runtime значения" not in javascript


def test_runtime_selection_ui_fails_closed_without_diff_or_timestamp() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert 'project.runtime_selection_status === "current"' in javascript
    assert "Текущая модель не определена" in javascript
    assert "Сохранено, ожидает применения" not in javascript
    assert "Runtime значения" not in javascript
    assert "Проверено" not in javascript
    assert "observation source" not in javascript
    assert 'high: "Высокий"' in javascript
    assert 'xhigh: "Максимальный"' in javascript
    assert "post_apply_runtime_truth_unavailable" in javascript
    assert "post_apply_selection_mismatch" in javascript


def test_validation_and_unified_apply_payload_contracts() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    validate_index = javascript.index('apiRequest("/api/sessions/project-settings/validate"')
    apply_index = javascript.index('"/api/sessions/project-settings/apply-selection"')
    assert validate_index < apply_index
    assert 'apiRequest("/api/sessions/project-settings",' not in javascript
    assert 'method: "PUT"' not in _javascript_function(javascript, "confirmApplySettings")
    assert "expected_settings_revision: project.revision ?? null" in javascript
    assert "confirm_interrupt: true" in javascript
    assert "allow_last_known_good: false" in javascript
    assert "allow_last_known_good: true" not in javascript
    confirm = _javascript_function(javascript, "confirmApplySettings")
    assert confirm.count("/project-settings/apply-selection") == 1
    assert confirm.count("/project-onboarding/apply") == 1
    assert "validateForApplyWithCurrentCapabilities" not in confirm
    assert "state.settingsDialog.busy" in confirm
    availability = _javascript_function(javascript, "updateApplyAvailability")
    assert "tmux_session_running" not in availability
    opening = _javascript_function(javascript, "openApplyConfirmation")
    assert "validateForApplyWithCurrentCapabilities" in opening
    assert "/project-settings/apply-selection" not in opening
    assert 'method: "PUT"' not in opening
    assert 'state.settingsDialog.step = "confirm"' in opening


def test_new_project_onboarding_is_path_first_and_does_not_start_on_confirmation() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert 'id="settingsPathStep"' in html
    assert 'id="settingsPathInput"' in html
    assert "/api/sessions/project-onboarding/bind-path" in javascript
    assert "/api/sessions/project-onboarding/apply" in javascript
    assert 'project.onboarding_state === "UNCONFIGURED"' in javascript
    assert "Первую tmux-сессию запустит следующее сообщение" in javascript
    assert "runtime spec будет подготовлен без запуска Codex" in javascript


def test_api_error_preserves_status_code_and_detail_code() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert "class ApiError extends Error" in javascript
    assert "this.status = status" in javascript
    assert "this.code = code" in javascript
    assert 'detail?.code === "string"' in javascript
    assert "apiErrorMessage(response.status, code, rawMessage)" in javascript


def test_onboarding_errors_have_stable_user_facing_reasons() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    for code in (
        "PROJECT_IDENTITY_MISSING",
        "PATH_REQUIRED",
        "PATH_INVALID",
        "PATH_NOT_ALLOWED",
        "PROJECT_NOT_CONFIGURED",
    ):
        assert f"{code}:" in javascript


def test_apply_error_guidance_is_bounded_and_actionable() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert "revision_mismatch" in javascript
    assert "revision_conflict" in javascript
    assert "Настройки изменились в другом окне. Проверьте значения и повторите." in javascript
    assert "apply_failed_rolled_back" in javascript
    assert "Не удалось применить настройки. Предыдущее состояние восстановлено." in javascript
    assert "apply_failed_rollback_failed" in javascript
    assert "rollback тоже не завершился" in javascript
    assert "runtime_apply_recovery_required" in javascript
    assert "Не повторяйте применение" in javascript
    assert "settings_apply_rollback_failed" in javascript
    assert "Runtime восстановлен, но настройки не удалось вернуть." in javascript
    assert "work_interrupt_failed" in javascript
    assert "Настройки не изменены." in javascript


def test_private_paths_are_not_exposed_in_new_runtime_ui() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    forbidden = [
        "auth.json",
        ".env",
        "profile_path",
        "runtime_spec_sha256",
        "spec_sha256",
        "transcript",
        "initDataUnsafe",
        "CloudStorage",
    ]
    for needle in forbidden:
        assert needle not in html
        assert needle not in javascript


def test_cache_busting_query_is_deterministic() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")

    assert "./assets/app.css?v=cus-runtime-onboarding-v1" in html
    assert "./assets/app.js?v=cus-runtime-authority-v1" in html
    assert "?v=" in html
    assert "Date.now()" not in html


def test_runtime_managed_project_uses_runtime_as_authority() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    merge = _javascript_function(javascript, "mergeProjectView")
    assert 'runtimeBinding?.lifecycle_status === "CONFIGURED"' in merge
    assert 'onboarding_state: managedDesired ? "CONFIGURED"' in merge
    assert "tmux_session_running: managedDesired ? runtimeRunning" in merge
    assert "activity_status: managedDesired ? runtimeActivity" in merge
    assert "revision.spec_digest === managedDesired.spec_digest" in merge
    assert 'runtime_selection_status: desiredIsApplied ? "current"' in merge
    assert 'agent: managedDesired?.provider || project.agent' in merge

    assert 'binding.lifecycle_status === "CONFIGURED"' in javascript
    assert 'projectsResponse.integration?.status !== "compatible" && !state.runtimeManaged' in javascript

    card = _javascript_function(javascript, "projectCard")
    assert 'running ? "Активна (Runtime V1)" : "Не запущена"' in card
    assert "Лимиты аккаунта" in javascript
    assert "Codex управляется Runtime V1" in javascript


def test_check_failed_copy_reports_completed_failure() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert "Проверка выполнена с ошибками" in javascript
    assert "Проверка совместимости не выполнена" not in javascript


def test_upgrade_strip_has_only_required_pipeline_actions_and_states() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    nav_end = html.index("</nav>")
    strip = html.index('id="upgradeStrip"')
    accounts_heading = html.index('id="accountsHeading"')
    assert nav_end < strip < accounts_heading
    assert html.count('id="upgradeStrip"') == 1
    assert 'id="upgradeActionButton"' in html

    for label in ("Проверить", "Подготовить", "Совместить", "Обновить"):
        assert label in javascript
    for path in (
        "/api/providers/codex/upgrade/status",
        "/api/providers/codex/upgrade/refresh",
        "/api/providers/codex/upgrade/check",
        "/api/providers/codex/upgrade/repair/prepare",
        "/api/providers/codex/upgrade/repair",
        "/api/providers/codex/upgrade/update",
    ):
        assert path in javascript
    for state_name in (
        "CURRENT",
        "UPDATE_AVAILABLE",
        "COMPATIBLE",
        "INCOMPATIBLE",
        "EVIDENCE_READY",
        "CHECKING",
        "REPAIRING",
        "UPDATING",
        "HUMAN_REQUIRED",
        "ROLLBACK",
        "BLOCKED",
    ):
        assert state_name in javascript

    renderer = _javascript_function(javascript, "renderUpgradeStatus")
    assert 'classList.toggle("is-hidden", !label)' in renderer
    assert "state.upgradeBusy || !label" in renderer
    loader = _javascript_function(javascript, "loadUpgradeStatus")
    assert "try" in loader and "catch" in loader
    assert "loadAccounts" not in loader
    assert "loadProjects" not in loader
    discovery = _javascript_function(javascript, "refreshUpgradeDiscovery")
    assert 'method: "POST"' in discovery
    assert "upgradeRefreshInFlight" in discovery
    assert "UPGRADE_REFRESH_INTERVAL_MS" in javascript
    assert "Доступна новая версия Codex ${candidate}" in javascript
    assert "Совместимость с ЦУС ещё не проверена" in javascript
    assert "Не удалось проверить обновления" in javascript
    action = _javascript_function(javascript, "runUpgradeAction")
    assert '"X-CUS-Operation-Id": crypto.randomUUID()' in action


def test_upgrade_maintenance_copy_is_public_safe_and_non_actionable() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert 'id="upgradeMaintenanceText"' in html
    assert "/api/providers/codex/upgrade/maintenance" in javascript
    renderer = _javascript_function(javascript, "renderUpgradeMaintenance")
    assert "pending_cleanup_bytes" in renderer
    assert "reclaimed_bytes" in renderer
    assert "Очистка ожидает безопасного повтора" in renderer
    assert "binary_path" not in renderer
    assert "evidence_path" not in renderer
    assert "cleanup-manifest" not in renderer


def test_quota_ui_uses_remaining_values_with_bounded_initial_refresh() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert "Осталось ${percent.toFixed(0)}%" in javascript
    assert "Использовано ${percent.toFixed(0)}%" not in javascript
    assert "remaining_percent" in javascript
    assert "quota.windows" in javascript
    assert "Минимально осталось" not in javascript
    assert "Общий лимит аккаунта ${escapeHtml(accountLabel(accountSlug))} и модели, не личный лимит проекта" in javascript
    assert "Модель: ${escapeHtml(model)} · Бакет: ${escapeHtml(bucket)}" in javascript
    assert "refreshInitialAccountQuotas" in javascript
    assert javascript.count("/quota/refresh") == 1
    refresh_projects = _javascript_function(javascript, "refreshProjectsState")
    assert "/quota/refresh" not in refresh_projects
    assert "Лимит 5 часов" not in javascript
    assert "Недельный лимит" not in javascript
    assert (
        "progress-track"
        not in javascript[
            javascript.index("if (percent === null)") : javascript.index(
                "return `", javascript.index("if (percent === null)") + 1
            )
        ]
    )


def test_accounts_are_first_and_active_without_eager_project_polling() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    accounts_tab = html.index('data-tab="accounts"')
    projects_tab = html.index('data-tab="projects"')
    accounts_panel = html.index('id="accountsPanel"')
    projects_panel = html.index('id="projectsPanel"')
    assert accounts_tab < projects_tab
    assert accounts_panel < projects_panel
    assert 'class="tab is-active" type="button" data-tab="accounts"' in html
    assert 'id="accountsPanel" class="panel is-active"' in html
    assert 'id="projectsPanel" class="panel"' in html
    assert 'activeTab: "accounts"' in javascript
    startup = javascript[javascript.rindex("updateRefreshButtonContext();") :]
    assert "void bootstrapTelegramAuth();" in startup
    assert "void loadAccounts();" not in startup
    assert "loadProjects" not in startup
    assert "scheduleProjectPoll" not in startup


def test_contextual_top_refresh_is_bounded_and_non_destructive() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    accounts = _javascript_function(javascript, "refreshAllAccountQuotas")
    bounded = _javascript_function(javascript, "runBoundedWorkers")
    projects = _javascript_function(javascript, "refreshProjectsState")
    current = _javascript_function(javascript, "refreshCurrentTab")
    per_account = _javascript_function(javascript, "refreshQuota")

    assert 'account.has_auth_json === true && account.status === "readable"' in javascript
    assert "runBoundedWorkers(selected" in accounts
    assert "Math.min(concurrency, items.length)" in bounded
    assert "Promise.all(Array.from" in bounded
    assert "successCount += 1" in accounts
    assert "failures.push" in accounts
    assert "failures.slice(0, 10)" in accounts
    assert "Лимиты обновлены: ${successCount}" in accounts
    assert "Не удалось обновить: ${failures.length}" in accounts
    assert "state.topRefreshInFlight" in accounts
    assert "window.scrollTo(scrollX, scrollY)" in accounts
    assert "quotaRefreshPath(account.account_slug)" in accounts
    assert "rate-limit-reset-credits" not in accounts
    assert "/project-settings/apply" not in accounts
    assert "runtimeSelectionRefresh()" in projects
    assert 'apiRequest("/api/sessions/project-settings")' in projects
    assert "loadProjects" not in projects
    assert "quotaRefreshPath" not in projects
    assert "/capabilities" not in projects
    assert "rate-limit-reset-credits" not in projects
    assert "refreshAllAccountQuotas" in current
    assert "refreshProjectsState" in current
    assert "quotaRefreshPath(accountSlug)" in per_account
    assert "applyAccountQuotaSnapshot(accountSlug, quota" in per_account
    assert "refreshAll()" not in per_account


def test_bounded_worker_executes_once_with_parallelism_two() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    script = "\n".join(
        [
            "let active = 0; let maxActive = 0; const attempts = new Map();",
            f"async {_javascript_function(javascript, 'runBoundedWorkers')}",
            "(async () => {",
            " await runBoundedWorkers(['first', 'second', 'third'], async (slug) => {",
            "  attempts.set(slug, (attempts.get(slug) || 0) + 1);",
            "  active += 1; maxActive = Math.max(maxActive, active);",
            "  await new Promise((resolve) => setTimeout(resolve, 2));",
            "  active -= 1;",
            " }, 2);",
            " console.log(JSON.stringify({maxActive, attempts: Object.fromEntries(attempts)}));",
            "})().catch((error) => { console.error(error); process.exit(1); });",
        ]
    )
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    payload = json.loads(result.stdout)

    assert payload["maxActive"] == 2
    assert payload["attempts"] == {"first": 1, "second": 1, "third": 1}


def test_refresh_button_labels_follow_active_tab() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    updater = _javascript_function(javascript, "updateRefreshButtonContext")

    assert 'aria-label="Обновить лимиты всех аккаунтов"' in html
    assert 'title="Обновить лимиты всех аккаунтов"' in html
    assert "Обновить лимиты всех аккаунтов" in updater
    assert "Обновить состояние проектов" in updater
    assert 'setAttribute("aria-label", label)' in updater
    assert "elements.refreshButton.title = label" in updater


def test_spark_copy_is_only_added_for_exact_identifiers() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    predicate = _javascript_function(javascript, "isSparkQuotaBucket")
    quota_block = _javascript_function(javascript, "accountQuotaBlock")

    assert '"codex_spark"' in predicate
    assert '"gpt-5.3-codex-spark"' in predicate
    assert '"GPT-5.3-Codex-Spark"' in predicate
    assert "includes(" not in predicate
    assert "indexOf(" not in predicate
    assert "isSparkQuotaBucket(bucket)" in quota_block
    assert "Отдельный лимит модели Spark" in quota_block
    assert "Используется при выборе модели GPT-5.3-Codex-Spark." in quota_block
    compact = _javascript_function(javascript, "compactQuota")
    assert "quota.bucket_kind" in compact
    assert "windows.map" in compact
    assert compact.count("Отдельный лимит модели Spark") == 1


def test_activity_timestamps_are_rendered_only_for_matching_status() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    activity = _javascript_function(javascript, "activityPill")

    working = activity.index('project.activity_status === "working"')
    queued = activity.index('project.activity_status === "queued"')
    ready = activity.index('project.activity_status === "ready"')
    assert working < queued < ready
    assert activity.index("project.task_started_at") < queued
    assert activity.index("project.last_task_completed_at") > ready


def test_reset_credit_ui_is_account_scoped_and_idempotent() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert 'id="resetCreditDialog"' in html
    assert "Сохранённые сбросы" in javascript
    assert "Использовать сброс" in html
    assert "/rate-limit-reset-credits/consume" in javascript
    assert "crypto.randomUUID()" in javascript
    assert "state.resetDialog.idempotencyKey" in javascript
    assert "data-reset-credit-action" in javascript
    assert "data-reset-credit-id" not in javascript


def test_activity_polling_does_not_refresh_quota() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    poll_index = javascript.index("async function pollProjects")
    poll_block = javascript[poll_index : javascript.index("function openAccountDialog", poll_index)]
    assert "10000" in javascript
    assert "document.visibilityState" in javascript
    assert "state.mutationInFlight" in javascript
    assert "loadProjects({ background: true })" in poll_block
    assert "refreshAuthorizedAccountQuotas" not in poll_block
    assert "loadAccounts" not in poll_block


def test_background_project_polling_preserves_scroll_and_defers_interaction() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    poll_block = javascript[
        javascript.index("async function pollProjects") : javascript.index(
            "function markProjectInteraction"
        )
    ]

    assert "captureProjectScrollAnchor" in javascript
    assert "restoreProjectScrollAnchor" in javascript
    assert "state.pendingProjectUpdate" in javascript
    assert "state.projectInteractionActive" in javascript
    assert "data-project-key" in javascript
    assert "window.scrollBy(0, delta)" in javascript
    assert "renderSkeleton" not in poll_block


def test_activity_and_compact_settings_labels_are_user_facing() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    for text in [
        "Выполняется задача",
        "В очереди",
        "Готов",
        "Сессия не запущена",
        "Состояние работы неизвестно",
        "Параметры модели для этого агента не поддерживаются",
        "Текущие параметры",
        "При следующем запуске",
    ]:
        assert text in javascript


def test_activity_elapsed_formatter_is_separate_from_reset_countdown() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert "function formatElapsedSince" in javascript
    assert "${prefix} только что" in javascript
    assert (
        '${prefix} ${minutes} ${plural(minutes, "минуту", "минуты", "минут")} назад' in javascript
    )
    assert 'formatElapsedSince(project.last_task_completed_at, "Завершена")' in javascript
    activity_index = javascript.index("function activityPill")
    activity_block = javascript[
        activity_index : javascript.index("function projectCard", activity_index)
    ]
    assert 'formatElapsedSince(project.task_started_at, "Начата")' in activity_block
    assert "formatDurationUntil(project.task_started_at" not in activity_block
    assert "seconds < -120" in javascript
    assert 'if (!isoValue) return "";' in javascript
    assert 'if (!Number.isFinite(started)) return "";' in javascript


def test_quota_reset_parser_handles_seconds_milliseconds_and_zero_minute_case() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    script = "\n".join(
        [
            _javascript_function(javascript, "plural"),
            _javascript_function(javascript, "parseQuotaTimestamp"),
            _javascript_function(javascript, "formatFutureDuration"),
            _javascript_function(javascript, "formatQuotaResetRelative"),
            'Date.now = () => new Date("2026-07-28T12:00:00Z").getTime();',
            "console.log(JSON.stringify([",
            "  parseQuotaTimestamp(1785243540),",
            "  parseQuotaTimestamp(1785243540000),",
            '  parseQuotaTimestamp("1785243540"),',
            '  parseQuotaTimestamp("2026-07-28T12:59:00Z"),',
            "  parseQuotaTimestamp(true),",
            "  formatQuotaResetRelative(1785243540),",
            '  formatQuotaResetRelative("2026-07-28T12:00:30Z"),',
            '  formatQuotaResetRelative("2026-07-28T11:58:00Z"),',
            "]));",
        ]
    )
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert json.loads(result.stdout) == [
        1785243540000,
        1785243540000,
        1785243540000,
        1785243540000,
        None,
        "Через 59 мин",
        "Сброс сейчас",
        "Расчётное время сброса наступило",
    ]
    assert "Сброс через 0 мин" not in javascript


def test_activity_elapsed_formatter_outputs_elapsed_values() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    script = "\n".join(
        [
            _javascript_function(javascript, "plural"),
            _javascript_function(javascript, "formatElapsedSince"),
            'Date.now = () => new Date("2026-07-28T12:00:00Z").getTime();',
            "console.log(JSON.stringify([",
            '  formatElapsedSince("2026-07-28T11:59:45Z", "Начата"),',
            '  formatElapsedSince("2026-07-28T11:35:00Z", "Начата"),',
            '  formatElapsedSince("not-a-date", "Начата"),',
            '  formatElapsedSince("2026-07-28T12:05:00Z", "Начата"),',
            "]));",
        ]
    )
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert json.loads(result.stdout) == [
        "Начата только что",
        "Начата 25 минут назад",
        "",
        "",
    ]


def test_pathless_archive_ui_uses_inventory_identity_not_project_path() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert "project.archive_available" in javascript
    assert "project.inventory_identity_kind" in javascript
    assert "identity_kind: project.inventory_identity_kind" in javascript
    assert "project.archive_unavailable_reason" in javascript
    assert "archive-guidance" in javascript
    assert "!project.project_id || !project.project_path" not in javascript
    assert "window.confirm" not in javascript
    assert 'id="archiveDialog"' in (MINIAPP / "index.html").read_text(encoding="utf-8")
    assert "Отправить проект в Архив" in javascript
    assert (
        "Проект исчезнет из рабочих списков ЦУС и появится в разделе „Архивные проекты“."
        in javascript
    )
    assert (
        "Telegram-тема, файлы, настройки запуска, tmux-состояние и транскрипты не удаляются."
        in javascript
    )
    assert "Скрыть из рабочих проектов" not in javascript
    assert "Вернуть проект" in javascript
    assert "Вернуть в рабочие проекты" in javascript


def test_successful_quota_refresh_overrides_stale_auth_timestamp() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert 'quotaSnapshot?.status === "ok"' in javascript
    assert "Требуется обновление входа" not in javascript
    assert "Авторизация требует проверки" in javascript


def test_account_card_has_only_per_credit_use_buttons() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    account_card = _javascript_function(javascript, "accountCard")
    credit_row = _javascript_function(javascript, "resetCreditRow")

    assert ">Использовать</button>" not in account_card
    assert ">Использовать</button>" in credit_row
    assert "Обновить лимиты" in javascript
