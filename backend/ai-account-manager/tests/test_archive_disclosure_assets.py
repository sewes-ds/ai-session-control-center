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


def _run_node(script: str) -> object:
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return json.loads(result.stdout)


def test_archive_disclosure_zero_collapsed_and_expanded_execute() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    disclosure = _javascript_function(javascript, "archiveDisclosure")
    payload = _run_node(
        "\n".join(
            [
                "const state = {archiveExpanded: true};",
                "function projectCard(project) { return `<article data-stub-card>${project.name}</article>`; }",
                disclosure,
                "const empty = archiveDisclosure([]);",
                "state.archiveExpanded = false;",
                "const collapsed = archiveDisclosure([{name: 'A'}, {name: 'B'}]);",
                "state.archiveExpanded = true;",
                "const expanded = archiveDisclosure([{name: 'A'}, {name: 'B'}]);",
                "console.log(JSON.stringify({empty, emptyState: state.emptyState, collapsed, expanded, finalState: state.archiveExpanded}));",
            ]
        )
    )

    assert payload["empty"] == ""
    assert 'id="archivedProjectsContainer"' in payload["collapsed"]
    assert " hidden" in payload["collapsed"]
    assert "data-stub-card" not in payload["collapsed"]
    assert "Архивные проекты" in payload["collapsed"]
    assert "В архиве: 2" in payload["collapsed"]
    assert ">Показать</button>" in payload["collapsed"]
    assert 'aria-expanded="false"' in payload["collapsed"]
    assert 'aria-controls="archivedProjectsContainer"' in payload["collapsed"]
    assert "Показать архивные проекты. В архиве: 2" in payload["collapsed"]
    assert " hidden" not in payload["expanded"]
    assert payload["expanded"].count("data-stub-card") == 2
    assert ">Скрыть</button>" in payload["expanded"]
    assert 'aria-expanded="true"' in payload["expanded"]
    assert payload["finalState"] is True


def test_archive_count_updates_and_last_restore_resets_state_execute() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    disclosure = _javascript_function(javascript, "archiveDisclosure")
    payload = _run_node(
        "\n".join(
            [
                "const state = {archiveExpanded: false};",
                "function projectCard(project) { return `<article>${project.name}</article>`; }",
                disclosure,
                "const collapsedOne = archiveDisclosure([{name: 'A'}]);",
                "const collapsedTwo = archiveDisclosure([{name: 'A'}, {name: 'B'}]);",
                "const collapsedState = state.archiveExpanded;",
                "state.archiveExpanded = true;",
                "const expandedThree = archiveDisclosure([{name: 'A'}, {name: 'B'}, {name: 'C'}]);",
                "const expandedOne = archiveDisclosure([{name: 'C'}]);",
                "const expandedState = state.archiveExpanded;",
                "const empty = archiveDisclosure([]);",
                "console.log(JSON.stringify({collapsedOne, collapsedTwo, collapsedState, expandedThree, expandedOne, expandedState, empty, finalState: state.archiveExpanded}));",
            ]
        )
    )

    assert "В архиве: 1" in payload["collapsedOne"]
    assert "В архиве: 2" in payload["collapsedTwo"]
    assert payload["collapsedState"] is False
    assert payload["expandedThree"].count("<article>") == 3
    assert payload["expandedOne"].count("<article>") == 1
    assert payload["expandedState"] is True
    assert payload["empty"] == ""
    assert payload["finalState"] is False


def test_archive_toggle_preserves_anchor_and_focus_execute() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    toggle = _javascript_function(javascript, "toggleArchiveDisclosure")
    payload = _run_node(
        "\n".join(
            [
                "const button = {name: 'toggle'};",
                "const state = {archiveExpanded: false, projectView: [{archived: true}]};",
                "let captured = null; let rendered = 0; let restored = null;",
                "function captureProjectScrollAnchor(value) { captured = value.name; return {key: 'archive-disclosure', offset: 12}; }",
                "function renderProjects() { rendered += 1; }",
                "function restoreProjectScrollAnchor(anchor, options) { restored = {anchor, options}; }",
                toggle,
                "toggleArchiveDisclosure(button);",
                "console.log(JSON.stringify({expanded: state.archiveExpanded, captured, rendered, restored}));",
            ]
        )
    )

    assert payload["expanded"] is True
    assert payload["captured"] == "toggle"
    assert payload["rendered"] == 1
    assert payload["restored"]["anchor"]["key"] == "archive-disclosure"
    assert payload["restored"]["options"]["focusArchiveToggle"] is True


def test_archive_render_order_and_no_duplicate_cards_are_explicit() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    render = _javascript_function(javascript, "renderProjects")
    disclosure = _javascript_function(javascript, "archiveDisclosure")

    assert render.index('section("Рабочие проекты", working)') < render.index(
        'section("Требуют внимания", attention)'
    )
    assert render.index('section("Требуют внимания", attention)') < render.index(
        "archiveDisclosure(archive)"
    )
    assert 'filter((project) => !project.archived && !needsAttention(project))' in render
    assert 'filter((project) => !project.archived && needsAttention(project))' in render
    assert 'filter((project) => project.archived)' in render
    assert render.count("projectCard(project)") == 1
    assert disclosure.count("projectCard(project)") == 1
    assert 'state.archiveExpanded = false' in disclosure


def test_poll_refresh_tabs_visibility_and_activation_do_not_reset_disclosure() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    functions = (
        "applyProjectPayload",
        "loadProjects",
        "refreshProjectsState",
        "switchTab",
        "pollProjects",
        "handleVisibilityChange",
        "handleTelegramActivated",
    )
    for name in functions:
        body = _javascript_function(javascript, name)
        assert "archiveExpanded =" not in body
        assert "archiveExpanded:" not in body

    assert javascript.count("setTimeout(pollProjects, 10000)") == 1
    assert javascript.count('elements.projectsList.addEventListener("click"') == 1


def test_archive_mutation_keeps_backend_contract_and_disclosure_state() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    confirmation = _javascript_function(javascript, "confirmArchiveDialog")

    assert "/api/sessions/project-inventory/" in confirmation
    assert 'method: "PUT"' in confirmation
    assert "archived," in confirmation
    assert 'reason: archived ? "manual_archive" : "manual_unarchive"' in confirmation
    assert "archiveExpanded" not in confirmation
    assert "DELETE" not in confirmation
    assert "loadProjects()" in confirmation
    assert "captureProjectScrollAnchor()" in confirmation
    assert "restoreProjectScrollAnchor(scrollAnchor)" in confirmation


def test_archive_wording_and_reverse_action_are_exact() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    dialog = _javascript_function(javascript, "renderArchiveDialog")
    card = _javascript_function(javascript, "projectCard")

    assert "Скрыть из рабочих проектов" not in html
    assert "Скрыть из рабочих проектов" not in javascript
    assert "Скрыть проект из рабочих" not in html
    assert "Скрыть проект из рабочих" not in javascript
    assert html.count("Отправить проект в Архив") == 2
    assert "Отправить проект в Архив" in card
    assert "Отправить проект в Архив" in dialog
    assert "Проект исчезнет из рабочих списков ЦУС и появится в разделе „Архивные проекты“." in dialog
    assert "Telegram-тема, файлы, настройки запуска, tmux-состояние и транскрипты не удаляются." in dialog
    assert "Вернуть проект" in dialog
    assert "Вернуть в рабочие проекты" in dialog
    assert "Проект снова появится среди рабочих проектов ЦУС." in dialog


def test_hidden_archive_controls_are_not_rendered_or_focusable() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    disclosure = _javascript_function(javascript, "archiveDisclosure")

    assert '${expanded ? projects.map((project) => projectCard(project)).join("") : ""}' in disclosure
    assert '${expanded ? "" : "hidden"}' in disclosure
    assert 'type="button"' in disclosure
    assert 'data-toggle-archive' in disclosure
    assert 'aria-expanded="${expanded ? "true" : "false"}"' in disclosure
    assert 'aria-controls="archivedProjectsContainer"' in disclosure


def test_scroll_anchor_supports_cards_and_archive_disclosure() -> None:
    javascript = (MINIAPP / "assets" / "app.js").read_text(encoding="utf-8")
    capture = _javascript_function(javascript, "captureProjectScrollAnchor")
    restore = _javascript_function(javascript, "restoreProjectScrollAnchor")
    card = _javascript_function(javascript, "projectCard")
    disclosure = _javascript_function(javascript, "archiveDisclosure")

    assert "data-project-scroll-anchor" in card
    assert 'data-project-scroll-anchor="archive-disclosure"' in disclosure
    assert "preferredElement?.closest?." in capture
    assert "[data-project-scroll-anchor]" in capture
    assert "window.scrollBy(0, delta)" in restore
    assert "window.scrollTo(anchor.scrollX || 0, anchor.scrollY || 0)" in restore
    assert "preventScroll: true" in restore


def test_archive_responsive_styles_and_cache_version() -> None:
    html = (MINIAPP / "index.html").read_text(encoding="utf-8")
    css = (MINIAPP / "assets" / "app.css").read_text(encoding="utf-8")

    assert "./assets/app.css?v=cus-runtime-onboarding-v1" in html
    assert "./assets/app.js?v=cus-runtime-authority-v1" in html
    assert "https://telegram.org/js/telegram-web-app.js?63" in html
    assert ".archive-disclosure" in css
    assert ".archive-disclosure-header" in css
    assert ".archive-disclosure-count" in css
    assert ".archive-disclosure-button" in css
    assert ".archive-disclosure-content[hidden]" in css
    assert "@media (max-width: 380px)" in css
    assert ".archive-disclosure-button { width: 100%; }" in css
    assert "@media (prefers-color-scheme: dark)" in css
