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


def test_runtime_selection_refresh_has_only_three_explicit_frontend_triggers() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    endpoint = _javascript_function(javascript, "runtimeSelectionRefresh")
    initial = _javascript_function(javascript, "refreshInitialRuntimeSelections")
    manual = _javascript_function(javascript, "refreshProjectsState")
    post_apply = _javascript_function(javascript, "confirmApplySettings")
    loader = _javascript_function(javascript, "loadPrivateDataForCurrentAuth")

    assert javascript.count("/api/sessions/runtime-selection/refresh") == 1
    assert 'method: "POST"' in endpoint
    assert initial.count("runtimeSelectionRefresh()") == 1
    assert manual.count("runtimeSelectionRefresh()") == 1
    assert 'loadProjects({ refreshRuntimeSelection: true })' in post_apply
    assert "refreshRuntimeSelection\n          ? runtimeSelectionRefresh()" in javascript
    assert "state.runtimeSelectionAutoRefreshGeneration !== generation" in loader
    assert "state.runtimeSelectionAutoRefreshGeneration = generation" in loader
    assert "void refreshInitialRuntimeSelections(generation)" in loader


def test_poll_accounts_tabs_visibility_and_activation_do_not_probe_runtime_selection() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    unrelated = [
        _javascript_function(javascript, "pollProjects"),
        _javascript_function(javascript, "refreshAllAccountQuotas"),
        _javascript_function(javascript, "refreshQuota"),
        _javascript_function(javascript, "switchTab"),
        _javascript_function(javascript, "handleVisibilityChange"),
        _javascript_function(javascript, "handleTelegramActivated"),
        _javascript_function(javascript, "refreshSelectedCapabilities"),
        _javascript_function(javascript, "toggleArchiveDisclosure"),
    ]

    for function in unrelated:
        assert "runtimeSelectionRefresh" not in function
        assert "/runtime-selection/refresh" not in function
    assert "loadProjects({ background: true })" in unrelated[0]


def test_running_and_stopped_runtime_truth_ui_contract() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    renderer = _javascript_function(javascript, "renderSettingsBlock")

    running_index = renderer.index("project.tmux_session_running")
    stopped_index = renderer.index("const nextModel")
    assert running_index < stopped_index
    running = renderer[running_index:stopped_index]
    stopped = renderer[stopped_index:]
    assert "project.current_model" in running
    assert "project.current_reasoning_effort" in running
    assert "project.runtime_model" not in running
    assert "project.model" not in running
    assert "Текущая модель не определена" in running
    assert "Текущие параметры" in running
    assert "project.model" in stopped
    assert "project.reasoning_effort" in stopped
    assert "При следующем запуске" in stopped


def test_runtime_truth_ui_has_no_source_timestamp_or_diff_disclosure() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    renderer = _javascript_function(javascript, "renderSettingsBlock")

    for forbidden in (
        "Сохранено в ЦУС",
        "Сохранено, ожидает применения",
        "Runtime значения",
        "Проверено",
        "observed_at",
        "runtime_selection_source",
        "transcript_path",
        "session_id",
    ):
        assert forbidden not in renderer
        assert forbidden not in html
    assert "Настройки следующего запуска" in html


def test_runtime_truth_asset_version_is_deterministic() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")

    assert './assets/app.css?v=cus-runtime-onboarding-v1' in html
    assert './assets/app.js?v=cus-runtime-authority-v1' in html
    assert 'https://telegram.org/js/telegram-web-app.js?63' in html
