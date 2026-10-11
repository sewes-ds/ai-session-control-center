from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MINIAPP = ROOT / "frontend" / "telegram-miniapp"


def test_topic_onboarding_surface_matches_runtime_contract() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert 'id="topicOnboarding"' in html
    assert 'id="onboardingProjectPath"' in html
    assert 'id="onboardingProjectPaths"' in html
    assert 'id="onboardingAccount"' in html
    assert 'id="onboardingModel"' in html
    assert 'id="onboardingReasoning"' in html
    assert "Выберите существующий проект из списка" in html
    assert 'get("tgWebAppStartParam")' in javascript
    assert 'get("startapp")' in javascript
    assert "/api/onboarding/${encodeURIComponent(topicOnboardingToken)}" in javascript
    assert "/api/onboarding/${encodeURIComponent(topicOnboardingToken)}/apply" in javascript
    assert "/api/onboarding/${encodeURIComponent(topicOnboardingToken)}/complete" in javascript
    assert 'provider: "codex"' in javascript


def test_topic_onboarding_closes_only_after_configured_backend_response() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    apply_start = javascript.index("async function applyTopicOnboarding")
    apply_end = javascript.index("\n  function setCapabilityLoading", apply_start)
    apply_function = javascript[apply_start:apply_end]
    assert 'result.status !== "CONFIGURED"' in apply_function
    assert "result.close_miniapp !== true" in apply_function
    assert apply_function.index("await apiRequest(") < apply_function.index(
        "closeTopicOnboarding();"
    )
    assert "telegram.close();" in javascript
    assert "Настройка завершена. Возвращаемся в Telegram" in javascript


def test_legacy_cus_command_is_not_presented_as_entrypoint() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")

    assert "через команду /cus" not in html
    assert "из сообщения CUS-бота" in html


def test_project_cards_use_binding_scoped_sanitized_runtime_health() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert 'apiRequest("/api/runtime/bindings/health")' in javascript
    assert "`${project.chat_id}:${project.thread_id}`" in javascript
    assert "runtime_binding_health" in javascript
    assert "runtimeHealthPill(project)" in javascript
    assert "Runtime недоступен" in javascript
    assert "Расхождение конфигурации" in javascript


def test_managed_binding_settings_use_non_interrupting_runtime_path() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")

    assert "`topic:${project.chat_id}:${project.thread_id}`" in javascript
    assert "managedDesired?.account_profile || settings?.account_profile" in javascript
    assert "project.runtime_binding_health?.lifecycle_status === \"CONFIGURED\"" in javascript
    assert "/api/runtime/bindings/${encodeURIComponent(binding.binding_id)}/selection" in javascript
    managed_start = javascript.index("if (binding) {", javascript.index("async function confirmApplySettings"))
    legacy_start = javascript.index("const endpoint = onboarding", managed_start)
    managed_branch = javascript[managed_start:legacy_start]
    assert "expected_spec_digest: binding.desired.spec_digest" in managed_branch
    assert "confirm_interrupt" not in managed_branch
    assert "Активная задача не прервётся" in javascript
