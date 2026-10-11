(() => {
  "use strict";

  const inferredApiBase = window.location.pathname.startsWith("/cus/") ? "/cus" : "";
  const API_BASE = (window.CUS_API_BASE || inferredApiBase).replace(/\/$/, "");
  const AUTH_WAIT_TIMEOUT_MS = 3000;
  const AUTH_POLL_INTERVAL_MS = 50;
  const UPGRADE_REFRESH_INTERVAL_MS = 15 * 60 * 1000;
  const previewTokenKey = "cus_preview_token";
  const previewUrl = new URL(window.location.href);
  const previewTokenFromUrl = previewUrl.searchParams.get("preview_token") || "";
  if (previewTokenFromUrl) {
    window.sessionStorage.setItem(previewTokenKey, previewTokenFromUrl);
    previewUrl.searchParams.delete("preview_token");
    window.history.replaceState({}, document.title, previewUrl.toString());
  }
  const previewToken = previewTokenFromUrl || window.sessionStorage.getItem(previewTokenKey) || "";

  function onboardingStartParam() {
    const candidates = [
      previewUrl.searchParams.get("tgWebAppStartParam"),
      previewUrl.searchParams.get("startapp"),
    ];
    return candidates.find((value) => (
      typeof value === "string"
      && value.length >= 32
      && value.length <= 512
      && /^[A-Za-z0-9_-]+$/.test(value)
    )) || "";
  }

  const topicOnboardingToken = onboardingStartParam();

  const effortOrder = ["minimal", "low", "medium", "high", "xhigh"];
  const effortLabels = {
    minimal: "Минимальный",
    low: "Низкий",
    medium: "Средний",
    high: "Высокий",
    xhigh: "Максимальный",
  };

  const state = {
    activeTab: "accounts",
    enrollmentId: null,
    enrollmentPoll: null,
    projects: [],
    projectSettings: [],
    projectView: [],
    runtimeBindingHealth: new Map(),
    accounts: [],
    accountDetails: new Map(),
    integration: null,
    capabilityCache: new Map(),
    settingsDialog: {
      project: null,
      step: "edit",
      path: "",
      selections: { account: "", model: "", reasoning: "" },
      unavailable: { account: false, model: false, reasoning: false },
      validation: null,
      busy: false,
    },
    resetDialog: {
      accountSlug: "",
      credit: null,
      idempotencyKey: null,
      busy: false,
    },
    resetCreditActions: new Map(),
    resetCreditActionCounter: 0,
    archiveDialog: {
      projectKey: "",
      archived: false,
      project: null,
      busy: false,
      error: "",
    },
    mutationInFlight: false,
    topRefreshInFlight: false,
    projectPoll: null,
    projectsLoaded: false,
    pendingProjectUpdate: null,
    projectInteractionActive: false,
    projectInteractionTimer: null,
    archiveExpanded: false,
    settingsBackButtonHandler: null,
    authStatus: previewToken ? "ready" : "pending",
    authBootstrapPromise: null,
    authWaitTimer: null,
    authAttempt: 0,
    authReadyGeneration: previewToken ? 1 : 0,
    loadedAuthGeneration: 0,
    quotaAutoRefreshGeneration: 0,
    quotaInitialRefreshInFlight: false,
    quotaInitialRefreshGeneration: 0,
    runtimeSelectionAutoRefreshGeneration: 0,
    runtimeSelectionInitialRefreshInFlight: false,
    runtimeSelectionInitialRefreshGeneration: 0,
    quotaFreshnessByAccount: new Map(),
    telegramEventSource: null,
    cleanupComplete: false,
    runtimeManaged: false,
    upgradeAction: "NONE",
    upgradeStatus: null,
    upgradeBusy: false,
    upgradeRefreshInFlight: false,
    upgradeRefreshTimer: null,
    upgradeMaintenance: null,
    topicOnboarding: {
      record: null,
      busy: false,
      selections: { account: "", model: "", reasoning: "" },
    },
  };

  const elements = {
    refreshButton: document.getElementById("refreshButton"),
    upgradeStatusText: document.getElementById("upgradeStatusText"),
    upgradeMaintenanceText: document.getElementById("upgradeMaintenanceText"),
    upgradeActionButton: document.getElementById("upgradeActionButton"),
    tabs: Array.from(document.querySelectorAll(".tab")),
    panels: {
      projects: document.getElementById("projectsPanel"),
      accounts: document.getElementById("accountsPanel"),
    },
    projectsList: document.getElementById("projectsList"),
    projectsStatus: document.getElementById("projectsStatus"),
    accountsList: document.getElementById("accountsList"),
    accountsStatus: document.getElementById("accountsStatus"),
    addAccountButton: document.getElementById("addAccountButton"),
    accountDialog: document.getElementById("accountDialog"),
    accountCreateStep: document.getElementById("accountCreateStep"),
    accountEnrollStep: document.getElementById("accountEnrollStep"),
    accountSlugInput: document.getElementById("accountSlugInput"),
    accountLabelInput: document.getElementById("accountLabelInput"),
    startEnrollmentButton: document.getElementById("startEnrollmentButton"),
    cancelEnrollmentButton: document.getElementById("cancelEnrollmentButton"),
    copyCodeButton: document.getElementById("copyCodeButton"),
    enrollmentUserCode: document.getElementById("enrollmentUserCode"),
    enrollmentLink: document.getElementById("enrollmentLink"),
    enrollmentStatusText: document.getElementById("enrollmentStatusText"),
    accountCreateError: document.getElementById("accountCreateError"),
    accountEnrollError: document.getElementById("accountEnrollError"),
    closeDialogButtons: Array.from(document.querySelectorAll("[data-close-dialog]")),
    projectSettingsDialog: document.getElementById("projectSettingsDialog"),
    settingsPathStep: document.getElementById("settingsPathStep"),
    settingsPathInput: document.getElementById("settingsPathInput"),
    settingsPathError: document.getElementById("settingsPathError"),
    bindProjectPathButton: document.getElementById("bindProjectPathButton"),
    settingsEditStep: document.getElementById("settingsEditStep"),
    settingsConfirmStep: document.getElementById("settingsConfirmStep"),
    settingsProjectTopic: document.getElementById("settingsProjectTopic"),
    settingsProjectPath: document.getElementById("settingsProjectPath"),
    settingsRuntimeStatus: document.getElementById("settingsRuntimeStatus"),
    settingsAccountSelect: document.getElementById("settingsAccountSelect"),
    settingsModelSelect: document.getElementById("settingsModelSelect"),
    settingsModelHelp: document.getElementById("settingsModelHelp"),
    settingsReasoningSelect: document.getElementById("settingsReasoningSelect"),
    settingsReasoningHelp: document.getElementById("settingsReasoningHelp"),
    refreshCapabilitiesButton: document.getElementById("refreshCapabilitiesButton"),
    settingsEditNotice: document.getElementById("settingsEditNotice"),
    settingsEditError: document.getElementById("settingsEditError"),
    openApplyConfirmationButton: document.getElementById("openApplyConfirmationButton"),
    closeSettingsDialogButtons: Array.from(document.querySelectorAll("[data-close-settings-dialog]")),
    resetCreditDialog: document.getElementById("resetCreditDialog"),
    resetCreditSummary: document.getElementById("resetCreditSummary"),
    resetCreditError: document.getElementById("resetCreditError"),
    confirmResetCreditButton: document.getElementById("confirmResetCreditButton"),
    cancelResetCreditButton: document.getElementById("cancelResetCreditButton"),
    archiveDialog: document.getElementById("archiveDialog"),
    archiveDialogTitle: document.getElementById("archiveDialogTitle"),
    archiveSummary: document.getElementById("archiveSummary"),
    archiveError: document.getElementById("archiveError"),
    confirmArchiveButton: document.getElementById("confirmArchiveButton"),
    cancelArchiveButton: document.getElementById("cancelArchiveButton"),
    settingsConfirmSummary: document.getElementById("settingsConfirmSummary"),
    settingsConfirmError: document.getElementById("settingsConfirmError"),
    settingsConfirmPrimaryCopy: document.getElementById("settingsConfirmPrimaryCopy"),
    settingsConfirmSecondaryCopy: document.getElementById("settingsConfirmSecondaryCopy"),
    confirmApplySettingsButton: document.getElementById("confirmApplySettingsButton"),
    backToSettingsEditButton: document.getElementById("backToSettingsEditButton"),
    emptyTemplate: document.getElementById("emptyStateTemplate"),
    authRecoveryNotice: document.getElementById("authRecoveryNotice"),
    retryAuthButton: document.getElementById("retryAuthButton"),
    mainApplication: document.getElementById("mainApplication"),
    topicOnboarding: document.getElementById("topicOnboarding"),
    onboardingTopicName: document.getElementById("onboardingTopicName"),
    onboardingState: document.getElementById("onboardingState"),
    onboardingProjectPath: document.getElementById("onboardingProjectPath"),
    onboardingProjectPaths: document.getElementById("onboardingProjectPaths"),
    onboardingAccount: document.getElementById("onboardingAccount"),
    onboardingModel: document.getElementById("onboardingModel"),
    onboardingModelHelp: document.getElementById("onboardingModelHelp"),
    onboardingReasoning: document.getElementById("onboardingReasoning"),
    onboardingReasoningHelp: document.getElementById("onboardingReasoningHelp"),
    onboardingRefreshCapabilities: document.getElementById("onboardingRefreshCapabilities"),
    onboardingNotice: document.getElementById("onboardingNotice"),
    onboardingError: document.getElementById("onboardingError"),
    onboardingApply: document.getElementById("onboardingApply"),
  };

  if (topicOnboardingToken) {
    elements.mainApplication.classList.add("is-hidden");
    elements.topicOnboarding.classList.remove("is-hidden");
  }

  class ApiError extends Error {
    constructor(message, { status = null, code = null } = {}) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.code = code;
    }
  }

  class AuthUnavailableError extends Error {
    constructor({ status = null } = {}) {
      super("Требуется повторная авторизация Telegram.");
      this.name = "AuthUnavailableError";
      this.status = status;
    }
  }

  function getTelegramWebApp() {
    try {
      return window.Telegram?.WebApp || null;
    } catch {
      return null;
    }
  }

  function getTelegramSdkInitData() {
    const value = getTelegramWebApp()?.initData;
    return typeof value === "string" && value.length > 0 ? value : "";
  }

  function launchParamValue(source) {
    if (typeof source !== "string" || source.length === 0) return "";
    if (/%(?![0-9a-f]{2})/i.test(source)) return "";
    try {
      const value = new URLSearchParams(source).get("tgWebAppData");
      return typeof value === "string" && value.length > 0 ? value : "";
    } catch {
      return "";
    }
  }

  function getLaunchUrlInitData() {
    const hashValue = launchParamValue(window.location.hash.replace(/^#/, ""));
    if (hashValue) return hashValue;
    return launchParamValue(window.location.search.replace(/^\?/, ""));
  }

  function getCurrentTelegramInitData() {
    return getTelegramSdkInitData() || getLaunchUrlInitData() || "";
  }

  function apiUrl(path) {
    return `${API_BASE}${path}`;
  }

  function boundedText(value, fallback) {
    const text = typeof value === "string" ? value : fallback;
    return text.replace(/\s+/g, " ").trim().slice(0, 240) || fallback;
  }

  function apiErrorMessage(status, code, rawMessage) {
    const mapped = {
      revision_mismatch: "Настройки изменились в другом окне. Проверьте значения и повторите.",
      revision_conflict: "Настройки изменились в другом окне. Проверьте значения и повторите.",
      capabilities_refresh_required: "Список моделей нужно обновить. Нажмите «Обновить модели» и повторите.",
      capabilities_current_required: "Для применения нужен свежий список моделей. Обновите модели и повторите.",
      model_not_available: "Эта модель недоступна для выбранного аккаунта. Выберите другую модель.",
      reasoning_not_supported: "Выберите поддерживаемый уровень reasoning для этой модели.",
      work_in_progress: "Проект сейчас выполняет задачу. Дождитесь завершения или остановите работу вручную.",
      session_not_running: "Не удалось определить состояние session проекта. Обновите проекты и повторите.",
      work_interrupt_failed: "Не удалось безопасно остановить текущую работу проекта. Настройки не изменены.",
      session_start_failed: "Не удалось применить настройки. Предыдущее состояние восстановлено.",
      apply_failed_rolled_back: "Не удалось применить настройки. Предыдущее состояние восстановлено.",
      post_apply_runtime_truth_unavailable: "Codex не подтвердил фактические параметры новой сессии. Предыдущее состояние восстановлено.",
      post_apply_selection_mismatch: "Фактические параметры Codex не совпали с выбранными. Предыдущее состояние восстановлено.",
      apply_failed_rollback_failed: "Применение не удалось, rollback тоже не завершился. Не повторяйте применение; требуется восстановление оператором.",
      runtime_apply_recovery_required: "Runtime требует ручного восстановления. Не повторяйте применение; требуется оператор.",
      settings_apply_rollback_failed: "Runtime восстановлен, но настройки не удалось вернуть. Не повторяйте операцию; требуется оператор.",
      control_socket_unavailable: "Временный сбой интеграции с Telegram Agent. Повторите позже.",
      control_socket_failed: "Временный сбой интеграции с Telegram Agent. Повторите позже.",
      integration_incompatible: "Интеграция с Telegram Agent несовместима с этим действием.",
      PROJECT_IDENTITY_MISSING: "Telegram-идентичность проекта не сохранена. Обновите проекты после операторской миграции.",
      PATH_REQUIRED: "Укажите абсолютный путь к каталогу проекта.",
      PATH_INVALID: "Каталог не найден или путь некорректен. Проверьте абсолютный путь.",
      PATH_NOT_ALLOWED: "Этот каталог находится вне разрешённых owner roots или недоступен владельцу сервиса.",
      PROJECT_NOT_CONFIGURED: "Проект не готов к этому шагу настройки. Обновите список проектов и повторите.",
      archive_identity_unavailable: "Идентичность архива сейчас недоступна. Обновите проекты и повторите.",
      project_id_mismatch: "Backend не подтвердил идентичность проекта. Обновите проекты и повторите.",
      ONBOARDING_TOKEN_EXPIRED: "Ссылка настройки обновлена. Закройте Mini App и снова нажмите «Настроить в ЦУС» в топике.",
      ONBOARDING_TOKEN_INVALID: "Ссылка настройки недействительна. Откройте кнопку из сообщения CUS-бота.",
      ONBOARDING_STATE_CHANGED: "Состояние настройки изменилось. Откройте Mini App заново из сообщения CUS-бота.",
      RUNTIME_UNAVAILABLE: "Runtime временно недоступен. Настройки не применены; повторите позже.",
      CUS_BOT_DELIVERY_UNAVAILABLE: "Настройки сохранены, но подтверждение в Telegram ещё не доставлено. Повторите отправку.",
    };
    if (mapped[code]) return mapped[code];
    const safe = boundedText(rawMessage, "Запрос завершился ошибкой.");
    return status ? `${safe} HTTP ${status}.` : safe;
  }

  async function apiRequest(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    if (previewToken) {
      headers.set("X-CUS-Preview-Token", previewToken);
    } else {
      const initData = getCurrentTelegramInitData();
      if (!initData) {
        transitionTelegramAuthFailure("unavailable");
        throw new AuthUnavailableError();
      }
      headers.set("X-Telegram-Init-Data", initData);
    }

    const response = await fetch(apiUrl(path), { ...options, headers });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      if (response.status === 401 && !previewToken) {
        transitionTelegramAuthFailure("rejected");
        throw new AuthUnavailableError({ status: response.status });
      }
      const detail = payload?.detail;
      const code = typeof detail?.code === "string" ? detail.code : null;
      const rawMessage = detail?.message || (typeof detail === "string" ? detail : payload?.message);
      throw new ApiError(apiErrorMessage(response.status, code, rawMessage), {
        status: response.status,
        code,
      });
    }
    return payload;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function statusPill(text, tone = "neutral") {
    return `<span class="status-pill status-${tone}">${escapeHtml(text)}</span>`;
  }

  function setNotice(element, message, kind = "warning") {
    if (
      state.authStatus !== "ready"
      && (element === elements.accountsStatus || element === elements.projectsStatus)
    ) {
      element.textContent = "";
      element.className = "notice is-hidden";
      return;
    }
    if (!message) {
      element.textContent = "";
      element.className = "notice is-hidden";
      return;
    }
    element.textContent = message;
    element.className = `notice${kind === "success" ? " notice-success" : ""}${kind === "error" ? " notice-error" : ""}${kind === "critical" ? " notice-critical" : ""}`;
  }

  function isAuthUnavailableError(error) {
    return error instanceof AuthUnavailableError;
  }

  const upgradeActionLabels = {
    CHECK: "Проверить",
    PREPARE: "Подготовить",
    REPAIR: "Совместить",
    UPDATE: "Обновить",
  };

  function upgradeStatusCopy(status) {
    const candidate = status?.candidate_version;
    const current = status?.current_version;
    const stateName = status?.state || "CURRENT";
    if (!current && state.runtimeManaged && stateName === "CURRENT") {
      return "Codex управляется Runtime V1";
    }
    if (status?.discovery_error_code && current) {
      return `Codex ${current} · Не удалось проверить обновления`;
    }
    if (stateName === "CURRENT" || stateName === "ACTIVE") {
      return current ? `Codex ${current}` : "Версия Codex не определена";
    }
    if (stateName === "UPDATE_AVAILABLE") {
      return candidate
        ? `Доступна новая версия Codex ${candidate}\nСовместимость с ЦУС ещё не проверена`
        : "Доступна новая версия Codex";
    }
    if (stateName === "COMPATIBLE") return `Codex ${candidate || ""} — совместима`.trim();
    if (stateName === "INCOMPATIBLE") return `Codex ${candidate || ""} — требуется совместить`.trim();
    if (stateName === "EVIDENCE_READY") return "Данные для совмещения готовы";
    if (stateName === "HUMAN_REQUIRED") return "Пять попыток исчерпаны — требуется человек";
    if (stateName === "CHECK_FAILED") return "Проверка выполнена с ошибками — повторять без исправления не нужно";
    if (stateName === "ROLLBACK") return "Обновление отменено — восстановлена предыдущая версия";
    if (stateName === "BLOCKED") return "Обновление заблокировано — требуется человек";
    if (stateName === "CHECKING") return "Проверяем совместимость…";
    if (stateName === "REPAIRING") return "Выполняется совмещение…";
    if (stateName === "UPDATING" || stateName === "ROLLING_BACK") return "Выполняется обновление…";
    return "Версия Codex не определена";
  }

  function renderUpgradeStatus(status = null) {
    state.upgradeStatus = status;
    state.upgradeAction = status?.action || "NONE";
    state.upgradeBusy = Boolean(status?.busy);
    elements.upgradeStatusText.textContent = upgradeStatusCopy(status);
    const label = upgradeActionLabels[state.upgradeAction];
    elements.upgradeActionButton.classList.toggle("is-hidden", !label);
    elements.upgradeActionButton.textContent = label || "";
    elements.upgradeActionButton.disabled = state.upgradeBusy || !label;
  }

  function compactBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return "";
    if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} ГБ`;
    if (bytes >= 1024 ** 2) return `${Math.round(bytes / 1024 ** 2)} МБ`;
    return `${Math.round(bytes / 1024)} КБ`;
  }

  function renderUpgradeMaintenance(status = null) {
    state.upgradeMaintenance = status;
    const element = elements.upgradeMaintenanceText;
    element.classList.remove("maintenance-warning", "maintenance-error");
    if (!status) {
      element.textContent = "";
      element.classList.add("is-hidden");
      return;
    }
    const pending = compactBytes(status.pending_cleanup_bytes);
    const reclaimed = compactBytes(status.reclaimed_bytes);
    if (status.last_cleanup_status === "FAILED") {
      element.textContent = pending ? `Очистка не завершена · ожидают ${pending}` : "Очистка не завершена";
      element.classList.add("maintenance-error");
    } else if (status.last_cleanup_status === "PENDING") {
      element.textContent = pending ? `Очистка ожидает безопасного повтора · ${pending}` : "Очистка ожидает безопасного повтора";
      element.classList.add("maintenance-warning");
    } else if (status.last_cleanup_at && status.cleaned_count > 0) {
      element.textContent = reclaimed ? `Хранилище очищено · освобождено ${reclaimed}` : "Хранилище очищено";
    } else {
      element.textContent = `Хранилище обновлений · ${compactBytes(status.disposable_usage_bytes)}`;
    }
    element.classList.remove("is-hidden");
  }

  async function loadUpgradeMaintenance() {
    try {
      renderUpgradeMaintenance(await apiRequest("/api/providers/codex/upgrade/maintenance"));
    } catch {
      renderUpgradeMaintenance();
    }
  }

  async function loadUpgradeStatus() {
    try {
      const [status, maintenance] = await Promise.all([
        apiRequest("/api/providers/codex/upgrade/status"),
        apiRequest("/api/providers/codex/upgrade/maintenance").catch(() => null),
      ]);
      renderUpgradeStatus(status);
      renderUpgradeMaintenance(maintenance);
    } catch {
      renderUpgradeStatus();
      renderUpgradeMaintenance();
    }
  }

  async function refreshUpgradeDiscovery() {
    if (state.authStatus !== "ready" || state.upgradeRefreshInFlight) return;
    state.upgradeRefreshInFlight = true;
    try {
      renderUpgradeStatus(await apiRequest("/api/providers/codex/upgrade/refresh", { method: "POST" }));
      await loadUpgradeMaintenance();
    } catch {
      await loadUpgradeStatus();
    } finally {
      state.upgradeRefreshInFlight = false;
    }
  }

  function scheduleUpgradeRefresh() {
    clearInterval(state.upgradeRefreshTimer);
    state.upgradeRefreshTimer = setInterval(() => {
      if (state.authStatus === "ready" && document.visibilityState === "visible") {
        void refreshUpgradeDiscovery();
      }
    }, UPGRADE_REFRESH_INTERVAL_MS);
  }

  async function runUpgradeAction() {
    if (state.upgradeBusy) return;
    const paths = {
      CHECK: "/api/providers/codex/upgrade/check",
      PREPARE: "/api/providers/codex/upgrade/repair/prepare",
      REPAIR: "/api/providers/codex/upgrade/repair",
      UPDATE: "/api/providers/codex/upgrade/update",
    };
    const path = paths[state.upgradeAction];
    if (!path) return;
    state.upgradeBusy = true;
    elements.upgradeActionButton.disabled = true;
    try {
      const options = { method: "POST" };
      if (state.upgradeAction === "CHECK" || state.upgradeAction === "UPDATE") {
        options.headers = { "X-CUS-Operation-Id": crypto.randomUUID() };
      }
      renderUpgradeStatus(await apiRequest(path, options));
      await loadUpgradeMaintenance();
    } catch {
      await loadUpgradeStatus();
    } finally {
      state.upgradeBusy = false;
    }
  }

  function stopProjectPolling() {
    clearTimeout(state.projectPoll);
    state.projectPoll = null;
  }

  function updateAuthControls() {
    const ready = state.authStatus === "ready";
    elements.tabs.forEach((tab) => { tab.disabled = !ready; });
    elements.addAccountButton.disabled = !ready;
    updateRefreshButtonContext();
  }

  function renderAuthRecovery() {
    const visible = state.authStatus === "unavailable" || state.authStatus === "rejected";
    const checking = state.authStatus === "pending" && state.authAttempt > 1;
    if (topicOnboardingToken) {
      setNotice(
        elements.onboardingError,
        visible
          ? "Не удалось подтвердить доступ Telegram. Закройте Mini App и откройте кнопку настройки снова."
          : checking
          ? "Повторно проверяем доступ Telegram…"
          : "",
        "error",
      );
      return;
    }
    elements.authRecoveryNotice.classList.toggle("is-hidden", !visible && !checking);
    elements.retryAuthButton.disabled = state.authStatus === "pending";
    elements.retryAuthButton.textContent = checking ? "Проверка…" : "Повторить проверку";
  }

  function clearPrivateContent() {
    state.accounts = [];
    state.projects = [];
    state.projectSettings = [];
    state.projectView = [];
    state.projectsLoaded = false;
    state.pendingProjectUpdate = null;
    state.capabilityCache.clear();
    state.accountDetails.clear();
    state.quotaFreshnessByAccount.clear();
    state.runtimeSelectionInitialRefreshInFlight = false;
    state.resetCreditActions.clear();
    state.resetCreditActionCounter = 0;
    elements.accountsList.replaceChildren();
    elements.projectsList.replaceChildren();
    elements.accountDialog.classList.add("is-hidden");
    elements.projectSettingsDialog.classList.add("is-hidden");
    elements.resetCreditDialog.classList.add("is-hidden");
    elements.archiveDialog.classList.add("is-hidden");
    clearTimeout(state.enrollmentPoll);
    state.enrollmentId = null;
    hideTelegramBackButton();
    setNotice(elements.accountsStatus, "");
    setNotice(elements.projectsStatus, "");
  }

  function setAuthStatus(status) {
    state.authStatus = status;
    if (status !== "ready") stopProjectPolling();
    renderAuthRecovery();
    updateAuthControls();
  }

  function transitionTelegramAuthFailure(status) {
    if (previewToken) return;
    if (status !== "unavailable" && status !== "rejected") return;
    clearPrivateContent();
    setAuthStatus(status);
  }

  function renderSkeleton(container, count = 1) {
    container.innerHTML = Array.from({ length: count }, () => `
      <div class="card skeleton" aria-hidden="true">
        <div class="skeleton-line short"></div>
        <div class="skeleton-line medium"></div>
        <div class="skeleton-line"></div>
        <div class="skeleton-line medium"></div>
      </div>
    `).join("");
  }

  function renderEmpty(container, title, message) {
    const fragment = elements.emptyTemplate.content.cloneNode(true);
    fragment.querySelector("h3").textContent = title;
    fragment.querySelector("p").textContent = message;
    container.replaceChildren(fragment);
  }

  function plural(value, one, few, many) {
    const n = Math.abs(value) % 100;
    const n1 = n % 10;
    if (n > 10 && n < 20) return many;
    if (n1 > 1 && n1 < 5) return few;
    if (n1 === 1) return one;
    return many;
  }

  function parseQuotaTimestamp(value) {
    if (value === null || value === undefined || typeof value === "boolean") return null;
    if (typeof value === "number") {
      if (!Number.isFinite(value)) return null;
      if (value >= 946684800 && value <= 4102444800) return value * 1000;
      if (value >= 946684800000 && value <= 4102444800000) return value;
      return null;
    }
    if (typeof value !== "string") return null;
    const text = value.trim();
    if (!text) return null;
    if (/^\d{10,13}$/.test(text)) {
      return parseQuotaTimestamp(Number(text));
    }
    const parsed = Date.parse(text);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function formatLocalDateTime(value) {
    const target = parseQuotaTimestamp(value);
    if (target === null) return "";
    return new Intl.DateTimeFormat("ru-RU", {
      day: "numeric",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date(target)).replace(/(\d{4})\s*г\./, "$1");
  }

  function formatFutureDuration(deltaSeconds) {
    let seconds = Math.max(61, Math.floor(deltaSeconds));
    const days = Math.floor(seconds / 86400);
    seconds %= 86400;
    const hours = Math.floor(seconds / 3600);
    seconds %= 3600;
    const minutes = Math.floor(seconds / 60);
    const parts = [];
    if (days) parts.push(`${days} ${plural(days, "день", "дня", "дней")}`);
    if (hours) parts.push(`${hours} ч`);
    if (minutes) parts.push(`${minutes} мин`);
    if (!parts.length) parts.push("1 мин");
    return parts.slice(0, 3).join(" ");
  }

  function formatQuotaResetRelative(value) {
    const target = parseQuotaTimestamp(value);
    if (target === null) return "Время сброса неизвестно";
    const deltaSeconds = Math.round((target - Date.now()) / 1000);
    if (Math.abs(deltaSeconds) <= 60) return "Сброс сейчас";
    if (deltaSeconds < -60) return "Расчётное время сброса наступило";
    return `Через ${formatFutureDuration(deltaSeconds)}`;
  }

  function quotaResetDetails(value) {
    const target = parseQuotaTimestamp(value);
    if (target === null) {
      return {
        datetime: "",
        exact: "",
        relative: "Время сброса неизвестно",
        past: false,
      };
    }
    const deltaSeconds = Math.round((target - Date.now()) / 1000);
    return {
      datetime: new Date(target).toISOString(),
      exact: formatLocalDateTime(target),
      relative: formatQuotaResetRelative(target),
      past: deltaSeconds < -60,
    };
  }

  function quotaResetDetailsMarkup(value) {
    const details = quotaResetDetails(value);
    if (!details.datetime) {
      return `<div class="quota-reset-details"><span>${escapeHtml(details.relative)}</span></div>`;
    }
    return `
      <div class="quota-reset-details${details.past ? " quota-reset-past" : ""}">
        <time datetime="${escapeHtml(details.datetime)}">Сброс: ${escapeHtml(details.exact)}</time>
        <span>${escapeHtml(details.relative)}</span>
        ${details.past ? "<span>Обновите лимиты</span>" : ""}
      </div>`;
  }

  function formatElapsedSince(isoValue, prefix) {
    if (!isoValue) return "";
    const started = new Date(isoValue).getTime();
    if (!Number.isFinite(started)) return "";
    const seconds = Math.round((Date.now() - started) / 1000);
    if (seconds < -120) return "";
    if (seconds < 60) return `${prefix} только что`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${prefix} ${minutes} ${plural(minutes, "минуту", "минуты", "минут")} назад`;
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return `${prefix} ${hours} ч назад`;
    const days = Math.floor(hours / 24);
    return `${prefix} ${days} ${plural(days, "день", "дня", "дней")} назад`;
  }

  function clampPercent(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return null;
    return Math.min(100, Math.max(0, value));
  }

  function durationLabel(minutes) {
    if (minutes === 60) return "1-часовое окно";
    if (minutes === 300) return "5-часовое окно";
    if (minutes === 1440) return "24-часовое окно";
    if (minutes === 10080) return "7-дневное окно";
    if (minutes && minutes % 1440 === 0) return `${minutes / 1440}-дневное окно`;
    if (minutes && minutes % 60 === 0) return `${minutes / 60}-часовое окно`;
    return minutes ? `Окно ${minutes} мин` : "Окно неизвестной длительности";
  }

  function quotaRow(label, remaining, resetAt, { reached = "" } = {}) {
    const percent = clampPercent(remaining);
    if (percent === null) {
      return `
        <div class="quota-row quota-row-empty">
          <div class="quota-topline"><span class="quota-name">${escapeHtml(label)}</span><span class="quota-value">Нет данных</span></div>
          <p class="quota-reset">Обновите лимиты аккаунта</p>
          ${quotaResetDetailsMarkup(resetAt)}
          ${reached ? `<p class="quota-reached">${escapeHtml(reached)}</p>` : ""}
        </div>`;
    }
    const value = `Осталось ${percent.toFixed(0)}%`;
    return `
      <div class="quota-row">
        <div class="quota-topline"><span class="quota-name">${escapeHtml(label)}</span><span class="quota-value">${escapeHtml(value)}</span></div>
        <div class="progress-track"><div class="progress-bar" style="width:${percent}%"></div></div>
        ${quotaResetDetailsMarkup(resetAt)}
        ${reached ? `<p class="quota-reached">${escapeHtml(reached)}</p>` : ""}
      </div>`;
  }

  function remainingFromUsed(value) {
    const used = clampPercent(value);
    return used === null ? null : 100 - used;
  }

  function allQuotaWindows(snapshot) {
    const rows = [];
    for (const bucket of snapshot?.buckets || []) {
      for (const window of bucket.windows || []) {
        rows.push({ bucket, window });
      }
    }
    return rows;
  }

  function compactQuota(project) {
    const quota = project.quota || {};
    const windows = Array.isArray(quota.windows) ? quota.windows : [];
    if (quota.bucket_kind === "unknown" || !windows.length) {
      const managedAccount = project.runtime_binding_health?.lifecycle_status === "CONFIGURED"
        ? project.runtime_binding_health.desired?.account_profile || ""
        : "";
      if (managedAccount) {
        return `
          <button class="quota-block quota-compact quota-button" type="button" data-focus-account="${escapeHtml(managedAccount)}">
            <p class="quota-scope">Лимиты аккаунта ${escapeHtml(accountLabel(managedAccount))}</p>
            <p class="quota-reset">Актуальные значения доступны во вкладке «Аккаунты»</p>
          </button>`;
      }
      return `<div class="quota-block quota-compact"><p class="quota-reset">${escapeHtml(quota.unavailable_reason || "Лимиты недоступны")}</p></div>`;
    }
    const accountSlug = quota.account_slug || project.account_slug || "";
    const model = modelDisplayName(quota.model_id, accountSlug);
    const bucket = quota.bucket_label || quota.selected_limit_name || quota.selected_limit_id || "Codex";
    const rows = windows.map((window) => quotaRow(
      window.window_label || window.window_type || "Окно лимита",
      window.remaining_percent,
      window.resets_at,
    ));
    const sparkNote = quota.bucket_kind === "spark"
      ? `<div class="quota-explanation"><strong>Отдельный лимит модели Spark</strong><p class="quota-reset">Используется только моделями Spark.</p></div>`
      : "";
    return `
      <button class="quota-block quota-compact quota-button" type="button" data-focus-account="${escapeHtml(accountSlug)}" ${accountSlug ? "" : "disabled"}>
        <p class="quota-scope">Общий лимит аккаунта ${escapeHtml(accountLabel(accountSlug))} и модели, не личный лимит проекта</p>
        <p class="quota-bucket-label">Модель: ${escapeHtml(model)} · Бакет: ${escapeHtml(bucket)}</p>
        ${rows.join("")}
        ${sparkNote}
      </button>`;
  }

  function authorizedAccounts() {
    return state.accounts.filter((account) => account.has_auth_json === true && account.status === "readable");
  }

  function accountLabel(accountSlug) {
    const account = state.accounts.find((item) => item.account_slug === accountSlug);
    return account?.label || account?.account_slug || accountSlug || "Не выбрано";
  }

  function modelDisplayName(modelId, accountSlug) {
    if (!modelId) return "—";
    const snapshots = [
      currentCapabilities(accountSlug),
      ...state.capabilityCache.values(),
    ].filter(Boolean);
    for (const snapshot of snapshots) {
      const model = modelsFromCapabilities(snapshot).find((item) => item.model_id === modelId);
      if (model?.display_name) return model.display_name;
    }
    const gptMatch = /^gpt-(\d+(?:\.\d+)*)(?:-(.+))?$/.exec(modelId);
    if (!gptMatch) return modelId;
    const suffix = (gptMatch[2] || "")
      .split("-")
      .filter(Boolean)
      .map((part) => part[0].toUpperCase() + part.slice(1))
      .join(" ");
    return `GPT-${gptMatch[1]}${suffix ? ` ${suffix}` : ""}`;
  }

  function mergeProjectView() {
    const settingsByPath = new Map(state.projectSettings.map((record) => [record.project_path, record]));
    state.projectView = state.projects.map((project) => {
      const settings = project.project_path ? settingsByPath.get(project.project_path) || null : null;
      const runtimeHealthKey = Number.isInteger(project.chat_id) && Number.isInteger(project.thread_id)
        ? `${project.chat_id}:${project.thread_id}`
        : "";
      const runtimeBinding = runtimeHealthKey
        ? state.runtimeBindingHealth.get(runtimeHealthKey) || null
        : null;
      const managedDesired = runtimeBinding?.lifecycle_status === "CONFIGURED"
        ? runtimeBinding.desired
        : null;
      const runtimeRunning = Boolean(managedDesired && runtimeBinding.active_instance_count > 0);
      const desiredIsApplied = Boolean(
        runtimeRunning
        && runtimeBinding.applied?.some(
          (revision) => revision.spec_digest === managedDesired.spec_digest,
        ),
      );
      const runtimeActivity = {
        BUSY: "working",
        IDLE: "ready",
        STOPPED: "session_stopped",
        UNKNOWN: "unknown",
      }[runtimeBinding?.activity];
      return {
        ...project,
        settings,
        agent: managedDesired?.provider || project.agent,
        onboarding_state: managedDesired ? "CONFIGURED" : project.onboarding_state,
        missing_launch_fields: managedDesired ? [] : project.missing_launch_fields,
        tmux_session_running: managedDesired ? runtimeRunning : project.tmux_session_running,
        activity_status: managedDesired ? runtimeActivity || "unknown" : project.activity_status,
        current_model: desiredIsApplied ? managedDesired.model : project.current_model,
        current_reasoning_effort: desiredIsApplied
          ? managedDesired.reasoning_effort
          : project.current_reasoning_effort,
        runtime_selection_status: desiredIsApplied ? "current" : project.runtime_selection_status,
        project_id: settings?.project_id || project.project_id || null,
        revision: settings?.revision ?? null,
        provider_id: settings?.provider_id || null,
        account_profile: managedDesired?.account_profile || settings?.account_profile || null,
        model: managedDesired?.model || settings?.model || null,
        reasoning_effort: managedDesired?.reasoning_effort || settings?.reasoning_effort || null,
        runtime_account_profile: settings?.runtime_account_profile || null,
        runtime_model: settings?.runtime_model || null,
        runtime_reasoning_effort: settings?.runtime_reasoning_effort || null,
        runtime_settings_revision: settings?.runtime_settings_revision ?? null,
        runtime_application_status: settings?.runtime_application_status || "missing",
        runtime_binding_health: runtimeBinding,
      };
    });
  }

  function projectKey(project) {
    if (Number.isInteger(project.chat_id) && Number.isInteger(project.thread_id)) {
      return `topic:${project.chat_id}:${project.thread_id}`;
    }
    return project.composite_key || project.project_id || `topic:${project.thread_id}`;
  }

  function projectByKey(key) {
    return state.projectView.find((project) => projectKey(project) === key) || null;
  }

  function renderSettingsBlock(project) {
    if (project.agent !== "codex") {
      return `
        <section class="launch-settings" aria-label="Настройки запуска">
          <div class="launch-settings-header">
            <h4>Настройки запуска</h4>
          </div>
          <p class="settings-compact-line">Параметры модели для этого агента не поддерживаются</p>
        </section>`;
    }
    if (project.tmux_session_running) {
      const current = project.runtime_selection_status === "current";
      const currentModel = current
        ? modelDisplayName(project.current_model, project.account_slug)
        : "Текущая модель не определена";
      const currentReasoning = current && project.current_reasoning_effort
        ? (effortLabels[project.current_reasoning_effort] || project.current_reasoning_effort)
        : "—";
      return `
        <section class="launch-settings" aria-label="Текущие параметры Codex">
          <div class="launch-settings-header">
            <h4>Текущие параметры</h4>
          </div>
          <dl class="settings-list">
            <div><dt>Модель</dt><dd>${escapeHtml(currentModel)}</dd></div>
            <div><dt>Reasoning</dt><dd>${escapeHtml(currentReasoning)}</dd></div>
          </dl>
        </section>`;
    }
    const nextModel = modelDisplayName(project.model, project.account_profile || project.account_slug);
    const nextReasoning = project.reasoning_effort
      ? (effortLabels[project.reasoning_effort] || project.reasoning_effort)
      : "—";
    return `
      <section class="launch-settings" aria-label="Параметры следующего запуска Codex">
        <div class="launch-settings-header">
          <h4>При следующем запуске</h4>
        </div>
        <p class="settings-compact-line">${escapeHtml(nextModel)} · ${escapeHtml(nextReasoning)}</p>
      </section>`;
  }

  function activityPill(project) {
    if (project.activity_status === "working") {
      const started = formatElapsedSince(project.task_started_at, "Начата");
      return statusPill(`Выполняется задача${started ? ` · ${started}` : ""}`, "warning");
    }
    if (project.activity_status === "queued") {
      const pending = Number.isInteger(project.pending_count) && project.pending_count > 0
        ? ` · ${project.pending_count}`
        : "";
      return statusPill(`В очереди${pending}`, "warning");
    }
    if (project.activity_status === "ready") {
      const completed = formatElapsedSince(project.last_task_completed_at, "Завершена");
      return statusPill(`Готов${completed ? ` · ${completed}` : ""}`, "success");
    }
    if (project.activity_status === "session_stopped") {
      return statusPill("Сессия не запущена", "neutral");
    }
    return statusPill("Состояние работы неизвестно", "warning");
  }

  function runtimeHealthPill(project) {
    const status = project.runtime_binding_health?.health;
    if (!status) return "";
    const labels = {
      RUNTIME_HEALTHY: ["Runtime готов", "success"],
      DEGRADED_CUS_SYNC: ["CUS недоступен · LKG", "warning"],
      PENDING_CHANGE: ["Ожидает перезапуска", "warning"],
      OBSERVED_PENDING_SYNC: ["Синхронизация настроек", "warning"],
      UNMANAGED: ["Не управляется Runtime", "neutral"],
      UNCONFIGURED: ["Runtime не настроен", "warning"],
      ARCHIVED: ["Runtime в архиве", "neutral"],
      RUNTIME_UNAVAILABLE: ["Runtime недоступен", "danger"],
      UNSUPPORTED_CODEX_SUBPROCESS: ["Codex subprocess запрещён", "danger"],
      CONFIG_DRIFT: ["Расхождение конфигурации", "danger"],
      CORRELATION_MISMATCH: ["Ошибка привязки Runtime", "danger"],
      AMBIGUOUS: ["Неоднозначная привязка", "danger"],
      OUT_OF_SYNC: ["Runtime не синхронизирован", "danger"],
      OBSERVED_POLICY_REJECTED: ["Настройки отклонены", "danger"],
      ERROR: ["Ошибка Runtime", "danger"],
    };
    const [label, tone] = labels[status] || ["Состояние Runtime неизвестно", "warning"];
    return statusPill(label, tone);
  }

  function projectCard(project) {
    const running = Boolean(project.tmux_session_running);
    const managedAccount = project.runtime_binding_health?.lifecycle_status === "CONFIGURED"
      ? project.runtime_binding_health.desired?.account_profile
      : null;
    const account = managedAccount ? accountLabel(managedAccount)
      : project.account_label || project.account_slug || "Не определён";
    const unconfigured = project.onboarding_state === "UNCONFIGURED";
    const agent = unconfigured && project.agent === "unknown"
      ? "Не выбран"
      : project.agent
        ? project.agent[0].toUpperCase() + project.agent.slice(1)
        : "Не определён";
    const quota = project.quota || {};
    const sessionLabel = project.runtime_binding_health?.lifecycle_status === "CONFIGURED"
      ? running ? "Активна (Runtime V1)" : "Не запущена"
      : project.tmux_session_name || "Не создана";
    const canConfigure = project.runtime_binding_health?.lifecycle_status === "CONFIGURED"
      || unconfigured
      || (project.agent === "codex" && Boolean(project.project_path) && authorizedAccounts().length > 0);
    const configureTitle = canConfigure ? "" : "Нужен проект Codex и хотя бы один читаемый авторизованный аккаунт";
    const canArchive = Boolean(project.archive_available) && !running;
    const archiveTitle = canArchive ? "" : (project.archive_unavailable_reason || "Архивирование требует подтверждённой остановленной идентичности проекта");
    const key = projectKey(project);
    const missingLabels = {
      project_path: "путь",
      account_profile: "аккаунт",
      model: "модель",
      reasoning_effort: "reasoning",
      runtime_launch_spec: "runtime spec",
    };
    const missing = (project.missing_launch_fields || []).map((item) => missingLabels[item] || item);
    const onboardingNotice = unconfigured
      ? `<div class="notice onboarding-notice"><strong>UNCONFIGURED</strong><br>Не заполнено: ${escapeHtml(missing.join(", ") || "подтверждение настройки")}. Запуск сообщений заблокирован.</div>`
      : "";
    return `
      <article class="card" data-project-key="${escapeHtml(key)}" data-project-scroll-anchor="project:${escapeHtml(key)}">
        <div class="card-body">
          <div class="card-header">
            <div>
              <h3 class="card-title">${escapeHtml(project.topic_name)}</h3>
              <p class="card-subtitle">${escapeHtml(project.project_path || "Путь не указан")}</p>
            </div>
            <div class="status-stack">
              ${activityPill(project)}
              ${runtimeHealthPill(project)}
            </div>
          </div>

          ${onboardingNotice}

          <div class="info-grid">
            <div class="info-item">
              <span class="info-label">Агент</span>
              <span class="info-value">${escapeHtml(agent)}</span>
            </div>
            <div class="info-item">
              <span class="info-label">${managedAccount ? "Выбранный аккаунт" : "Аккаунт"}</span>
              <span class="info-value">${escapeHtml(account)}</span>
            </div>
            <div class="info-item info-item-full">
              <span class="info-label">tmux-сессия</span>
              <span class="info-value">${escapeHtml(sessionLabel)}</span>
            </div>
          </div>

          ${unconfigured ? "" : renderSettingsBlock(project)}

          ${compactQuota(project)}
          ${project.archive_unavailable_reason ? `<p class="archive-guidance">${escapeHtml(project.archive_unavailable_reason)}</p>` : ""}
        </div>
        <div class="card-actions">
          <button class="button button-secondary" type="button" data-configure-project="${escapeHtml(key)}" ${canConfigure ? "" : "disabled"} title="${escapeHtml(configureTitle)}">Настроить запуск</button>
          ${project.archived
            ? `<button class="button button-secondary" type="button" data-unarchive-project="${escapeHtml(key)}">Вернуть в рабочие проекты</button>`
            : `<button class="button button-secondary" type="button" data-archive-project="${escapeHtml(key)}" title="${escapeHtml(archiveTitle)}">Отправить проект в Архив</button>`}
          <button class="button" type="button" disabled title="Смена агента будет реализована отдельно">Сменить агента</button>
        </div>
      </article>`;
  }

  function archiveDisclosure(projects) {
    if (!projects.length) {
      state.archiveExpanded = false;
      return "";
    }
    const count = projects.length;
    const expanded = state.archiveExpanded;
    const action = expanded ? "Скрыть" : "Показать";
    return `
      <section class="archive-disclosure" data-archive-disclosure data-project-scroll-anchor="archive-disclosure" aria-labelledby="archivedProjectsHeading">
        <div class="archive-disclosure-header">
          <div class="archive-disclosure-copy">
            <h3 id="archivedProjectsHeading">Архивные проекты</h3>
            <p id="archivedProjectsCount" class="archive-disclosure-count">В архиве: ${count}</p>
          </div>
          <button
            class="button button-secondary archive-disclosure-button"
            type="button"
            data-toggle-archive
            aria-expanded="${expanded ? "true" : "false"}"
            aria-controls="archivedProjectsContainer"
            aria-label="${action} архивные проекты. В архиве: ${count}"
          >${action}</button>
        </div>
        <div
          id="archivedProjectsContainer"
          class="archive-disclosure-content"
          aria-labelledby="archivedProjectsHeading archivedProjectsCount"
          ${expanded ? "" : "hidden"}
        >${expanded ? projects.map((project) => projectCard(project)).join("") : ""}</div>
      </section>`;
  }

  function renderProjects() {
    mergeProjectView();
    if (!state.projectView.length) {
      state.archiveExpanded = false;
      renderEmpty(elements.projectsList, "Проекты не найдены", "В конфигурации тмукс-бота пока нет проектов.");
      return;
    }
    const runtimeErrors = [
        "RUNTIME_UNAVAILABLE",
        "UNSUPPORTED_CODEX_SUBPROCESS",
        "CONFIG_DRIFT",
        "CORRELATION_MISMATCH",
        "AMBIGUOUS",
        "OUT_OF_SYNC",
        "OBSERVED_POLICY_REJECTED",
        "ERROR",
      ];
    const needsAttention = (project) => {
      if (project.runtime_binding_health?.lifecycle_status === "CONFIGURED") {
        return runtimeErrors.includes(project.runtime_binding_health.health);
      }
      return Boolean(
        project.onboarding_state === "UNCONFIGURED"
        || !project.path_exists
        || ["stale_state", "orphan_tmux", "unknown"].includes(project.session_status)
        || project.management_status === "runtime_invalid"
        || runtimeErrors.includes(project.runtime_binding_health?.health),
      );
    };
    const working = state.projectView.filter((project) => !project.archived && !needsAttention(project));
    const attention = state.projectView.filter((project) => !project.archived && needsAttention(project));
    const archive = state.projectView.filter((project) => project.archived);
    const section = (title, items) => items.length
      ? `<div class="project-group"><h3>${escapeHtml(title)}</h3>${items.map((project) => projectCard(project)).join("")}</div>`
      : "";
    elements.projectsList.innerHTML = [
      section("Рабочие проекты", working),
      section("Требуют внимания", attention),
      archiveDisclosure(archive),
    ].join("");
  }

  function captureProjectScrollAnchor(preferredElement = null) {
    const preferredAnchor = preferredElement?.closest?.("[data-project-scroll-anchor]") || null;
    const anchors = preferredAnchor
      ? [preferredAnchor]
      : Array.from(elements.projectsList.querySelectorAll("[data-project-scroll-anchor]"));
    for (const element of anchors) {
      const rect = element.getBoundingClientRect();
      if (rect.bottom > 0) {
        return {
          key: element.dataset.projectScrollAnchor,
          offset: rect.top,
          scrollX: window.scrollX,
          scrollY: window.scrollY,
        };
      }
    }
    return null;
  }

  function restoreProjectScrollAnchor(anchor, { focusArchiveToggle = false } = {}) {
    if (!anchor?.key) return;
    requestAnimationFrame(() => {
      const element = elements.projectsList.querySelector(
        `[data-project-scroll-anchor="${CSS.escape(anchor.key)}"]`,
      );
      if (!element) {
        window.scrollTo(anchor.scrollX || 0, anchor.scrollY || 0);
        return;
      }
      const delta = element.getBoundingClientRect().top - anchor.offset;
      if (delta) window.scrollBy(0, delta);
      if (focusArchiveToggle) {
        elements.projectsList.querySelector("[data-toggle-archive]")?.focus({ preventScroll: true });
      }
    });
  }

  function toggleArchiveDisclosure(button) {
    const archive = state.projectView.filter((project) => project.archived);
    if (!archive.length) {
      state.archiveExpanded = false;
      return;
    }
    const anchor = captureProjectScrollAnchor(button);
    state.archiveExpanded = !state.archiveExpanded;
    renderProjects();
    restoreProjectScrollAnchor(anchor, { focusArchiveToggle: true });
  }

  function projectDialogOpen() {
    return !elements.projectSettingsDialog.classList.contains("is-hidden")
      || !elements.resetCreditDialog.classList.contains("is-hidden")
      || !elements.archiveDialog.classList.contains("is-hidden")
      || !elements.accountDialog.classList.contains("is-hidden");
  }

  function shouldDeferProjectUpdate() {
    return state.projectInteractionActive
      || projectDialogOpen()
      || elements.projectsList.contains(document.activeElement);
  }

  function applyProjectPayload(
    projectsResponse,
    settingsResponse,
    { background = false, runtimeHealthResponse = null } = {},
  ) {
    const anchor = background ? captureProjectScrollAnchor() : null;
    state.projects = projectsResponse.projects || [];
    state.projectSettings = settingsResponse.projects || [];
    if (runtimeHealthResponse) {
      state.runtimeBindingHealth = new Map(
        (runtimeHealthResponse.bindings || []).map((binding) => [
          `${binding.chat_id}:${binding.thread_id}`,
          binding,
        ]),
      );
      state.runtimeManaged = (runtimeHealthResponse.bindings || []).some(
        (binding) => binding.lifecycle_status === "CONFIGURED",
      );
      renderUpgradeStatus(state.upgradeStatus);
    }
    state.integration = projectsResponse.integration || null;
    state.projectsLoaded = true;
    if (projectsResponse.integration?.status !== "compatible" && !state.runtimeManaged) {
      setNotice(elements.projectsStatus, projectsResponse.integration?.reason || "Интеграция с тмукс-ботом несовместима.", "error");
    }
    renderProjects();
    if (background) restoreProjectScrollAnchor(anchor);
  }

  function applyPendingProjectUpdate() {
    if (!state.pendingProjectUpdate || shouldDeferProjectUpdate()) return;
    const pending = state.pendingProjectUpdate;
    state.pendingProjectUpdate = null;
    applyProjectPayload(pending.projectsResponse, pending.settingsResponse, {
      background: true,
      runtimeHealthResponse: pending.runtimeHealthResponse || null,
    });
  }

  function authStatusText(account, authStatus, quotaSnapshot) {
    if (!account.has_auth_json || account.status === "missing_auth") return ["Не авторизован", "danger"];
    if (quotaSnapshot?.status === "ok") return ["Авторизация готова", "success"];
    if (!authStatus) return ["Авторизация не проверена", "warning"];
    if (authStatus.status === "ready") return ["Авторизация готова", "success"];
    if (authStatus.status === "stale_by_last_refresh") return ["Авторизация требует проверки", "warning"];
    return ["Проблема авторизации", "danger"];
  }

  function quotaWindow(snapshot, id) {
    return snapshot?.windows?.find((item) => item.window_id === id) || null;
  }

  function isSparkQuotaBucket(bucket) {
    const exactIds = new Set(["codex_spark", "gpt-5.3-codex-spark"]);
    const exactNames = new Set(["GPT-5.3-Codex-Spark"]);
    return exactIds.has(bucket?.limit_id) || exactNames.has(bucket?.limit_name);
  }

  function quotaFreshnessPresentation(quotaSnapshot, freshness) {
    const status = freshness?.status || (quotaSnapshot?.status === "ok" ? "cached" : "unavailable");
    const fetchedAt = freshness?.fetchedAt || quotaSnapshot?.fetched_at || null;
    const fetchedLabel = formatLocalDateTime(fetchedAt);
    if (status === "pending") {
      return {
        status,
        badge: "Получаем актуальные лимиты…",
        badgeTone: "pending",
        copy: "",
      };
    }
    if (status === "fresh") {
      return {
        status,
        badge: "Актуальные данные",
        badgeTone: "fresh",
        copy: fetchedLabel ? `Обновлено ${fetchedLabel}` : "Обновлено сейчас",
      };
    }
    if (status === "stale") {
      return {
        status,
        badge: "Не удалось получить свежие данные",
        badgeTone: "stale",
        copy: fetchedLabel
          ? `Показаны последние данные от ${fetchedLabel}`
          : "Показаны последние сохранённые данные",
      };
    }
    if (status === "cached") {
      return {
        status,
        badge: "Сохранённые данные",
        badgeTone: "neutral",
        copy: fetchedLabel ? `Сохранены ${fetchedLabel}` : "",
      };
    }
    return {
      status: "unavailable",
      badge: "Свежие данные недоступны",
      badgeTone: "unavailable",
      copy: "",
    };
  }

  function quotaSectionHeading(title, presentation) {
    return `
      <div class="quota-section-heading">
        <h4>${escapeHtml(title)}</h4>
        <span class="freshness freshness-${escapeHtml(presentation.badgeTone)}">${escapeHtml(presentation.badge)}</span>
      </div>
      ${presentation.copy ? `<p class="quota-freshness-copy">${escapeHtml(presentation.copy)}</p>` : ""}`;
  }

  function accountQuotaBlock(quotaSnapshot, freshness) {
    const presentation = quotaFreshnessPresentation(quotaSnapshot, freshness);
    const heading = quotaSectionHeading("Лимиты тарифа", presentation);
    if (presentation.status === "pending") {
      return `<section class="quota-section quota-block">${heading}<div class="quota-pending">Получаем актуальные лимиты…</div></section>`;
    }
    if (quotaSnapshot?.status !== "ok") {
      return `<section class="quota-section quota-block">${heading}<p class="quota-reset">Свежие данные недоступны</p></section>`;
    }
    const rows = [];
    for (const bucket of quotaSnapshot.buckets || []) {
      const bucketName = bucket.limit_name || bucket.limit_id || "Неизвестный лимит";
      for (const window of bucket.windows || []) {
        const label = `${bucketName} · ${durationLabel(window.window_duration_minutes)}`;
        rows.push(quotaRow(label, window.remaining_percent, window.resets_at, {
          reached: bucket.rate_limit_reached_type || "",
        }));
      }
      if (isSparkQuotaBucket(bucket)) {
        rows.push(`<div class="quota-explanation">
          <strong>Отдельный лимит модели Spark</strong>
          <p class="quota-reset">Используется при выборе модели GPT-5.3-Codex-Spark.</p>
        </div>`);
      }
    }
    if (!rows.length) {
      return `<section class="quota-section quota-block">${heading}<p class="quota-reset">Окна лимитов не получены</p></section>`;
    }
    return `<section class="quota-section quota-block">${heading}${rows.join("")}</section>`;
  }

  function resetCreditTitle(credit) {
    const title = typeof credit?.title === "string" ? credit.title.replace(/\s+/g, " ").trim() : "";
    return title.slice(0, 120) || "Полный сброс лимитов";
  }

  function resetCreditExpiryState(value) {
    const target = parseQuotaTimestamp(value);
    if (target === null) {
      return {
        status: "unknown",
        datetime: "",
        exact: "",
        relative: "Срок действия не указан",
      };
    }
    const deltaSeconds = Math.round((target - Date.now()) / 1000);
    if (deltaSeconds < -60) {
      return {
        status: "expired",
        datetime: new Date(target).toISOString(),
        exact: formatLocalDateTime(target),
        relative: "Срок действия истёк — обновите лимиты",
      };
    }
    if (deltaSeconds <= 86400) {
      return {
        status: "warning",
        datetime: new Date(target).toISOString(),
        exact: formatLocalDateTime(target),
        relative: Math.abs(deltaSeconds) <= 60
          ? "Истекает сейчас"
          : `Через ${formatFutureDuration(deltaSeconds)}`,
      };
    }
    return {
      status: "normal",
      datetime: new Date(target).toISOString(),
      exact: formatLocalDateTime(target),
      relative: `Через ${formatFutureDuration(deltaSeconds)}`,
    };
  }

  function sortedResetCredits(credits) {
    return (Array.isArray(credits) ? credits : [])
      .map((credit, index) => ({
        credit,
        index,
        expiry: parseQuotaTimestamp(credit?.expires_at),
      }))
      .sort((left, right) => {
        if (left.expiry === null && right.expiry !== null) return 1;
        if (left.expiry !== null && right.expiry === null) return -1;
        if (left.expiry !== right.expiry) return (left.expiry || 0) - (right.expiry || 0);
        return left.index - right.index;
      })
      .map((item) => item.credit);
  }

  function registerResetCreditAction(accountSlug, credit, generic = false) {
    state.resetCreditActionCounter += 1;
    const actionKey = `reset-${state.resetCreditActionCounter}`;
    state.resetCreditActions.set(actionKey, {
      accountSlug,
      generic,
      credit: {
        credit_id: generic ? null : (typeof credit?.credit_id === "string" && credit.credit_id ? credit.credit_id : null),
        title: generic ? "Доступный сохранённый сброс" : resetCreditTitle(credit),
        expires_at: generic ? null : (credit?.expires_at ?? null),
      },
    });
    return actionKey;
  }

  function resetCreditRow(accountSlug, credit) {
    const expiry = resetCreditExpiryState(credit?.expires_at);
    const hasCreditId = typeof credit?.credit_id === "string" && credit.credit_id.length > 0;
    const enabled = hasCreditId && expiry.status !== "expired";
    const actionKey = enabled ? registerResetCreditAction(accountSlug, credit) : "";
    const tone = expiry.status === "warning" ? "warning" : expiry.status === "expired" ? "danger" : "neutral";
    return `
      <article class="reset-credit-row reset-credit-row-${tone}">
        <h5 class="reset-credit-title">${escapeHtml(resetCreditTitle(credit))}</h5>
        <div class="reset-credit-expiry">
          ${expiry.datetime
            ? `<time datetime="${escapeHtml(expiry.datetime)}">Действует до ${escapeHtml(expiry.exact)}</time>`
            : ""}
          ${expiry.status === "warning" ? '<span class="reset-credit-state reset-credit-state-warning">Истекает менее чем через сутки</span>' : ""}
          <span class="reset-credit-state reset-credit-state-${tone}">${escapeHtml(expiry.relative)}</span>
        </div>
        <button class="button button-secondary" type="button"
          ${actionKey ? `data-reset-credit-action="${escapeHtml(actionKey)}"` : "disabled"}>Использовать</button>
      </article>`;
  }

  function resetCreditsBlock(accountSlug, quotaSnapshot, freshness) {
    const presentation = quotaFreshnessPresentation(quotaSnapshot, freshness);
    const heading = quotaSectionHeading("Сохранённые сбросы", presentation);
    const intro = '<p class="reset-credit-intro">Сохранённые сбросы применяются только вручную и относятся ко всему аккаунту.</p>';
    if (presentation.status === "pending") {
      return `<section class="quota-section">${heading}${intro}<div class="quota-pending">Получаем актуальные лимиты…</div></section>`;
    }
    const creditsState = quotaSnapshot?.reset_credits || {};
    const count = Number.isInteger(creditsState.available_count) && creditsState.available_count >= 0
      ? creditsState.available_count
      : null;
    if (count === null) {
      return `<section class="quota-section">${heading}${intro}<p class="quota-reset">Данные о сохранённых сбросах не получены</p></section>`;
    }
    if (count === 0) {
      return `<section class="quota-section">${heading}${intro}<p class="quota-reset">Доступных сохранённых сбросов нет</p></section>`;
    }
    const detailed = sortedResetCredits(creditsState.credits);
    const rows = detailed.map((credit) => resetCreditRow(accountSlug, credit));
    const usableDetailed = detailed.filter((credit) => {
      const expiry = resetCreditExpiryState(credit?.expires_at);
      return typeof credit?.credit_id === "string" && credit.credit_id.length > 0 && expiry.status !== "expired";
    }).length;
    const unknownCount = Math.max(0, count - usableDetailed);
    const genericActionKey = unknownCount > 0 ? registerResetCreditAction(accountSlug, null, true) : "";
    const countOnly = !Array.isArray(creditsState.credits);
    return `
      <section class="quota-section">
        ${heading}
        ${intro}
        ${rows.length ? `<div class="reset-credit-list">${rows.join("")}</div>` : ""}
        ${countOnly ? `<p class="reset-credit-partial">Доступно сбросов: ${count}</p><p class="reset-credit-partial">Подробные сроки действия не получены</p>` : ""}
        ${!countOnly && unknownCount > 0 ? `<p class="reset-credit-partial">Ещё без подробностей: ${unknownCount}</p>` : ""}
        ${genericActionKey
          ? `<button class="button button-secondary" type="button" data-reset-credit-action="${escapeHtml(genericActionKey)}">Использовать доступный сброс</button>`
          : ""}
      </section>`;
  }

  function accountCard(account, authStatus, quotaSnapshot) {
    const [authText, authTone] = authStatusText(account, authStatus, quotaSnapshot);
    const freshness = state.quotaFreshnessByAccount.get(account.account_slug) || null;
    const quotaRefreshPending = freshness?.status === "pending";
    return `
      <article class="card" data-account-card="${escapeHtml(account.account_slug)}">
        <div class="card-body">
          <div class="card-header">
            <div>
              <p class="eyebrow">Codex</p>
              <h3 class="card-title">${escapeHtml(account.label || account.account_slug)}</h3>
              <p class="card-subtitle">${escapeHtml(account.account_slug)}</p>
            </div>
            ${statusPill(authText, authTone)}
          </div>

          ${accountQuotaBlock(quotaSnapshot, freshness)}
          ${resetCreditsBlock(account.account_slug, quotaSnapshot, freshness)}
        </div>
        <div class="card-actions">
          <button class="button button-secondary" type="button" data-refresh-quota="${escapeHtml(account.account_slug)}" ${account.has_auth_json && !quotaRefreshPending ? "" : "disabled"}>Обновить лимиты</button>
        </div>
      </article>`;
  }

  async function loadProjects({ background = false, refreshRuntimeSelection = false } = {}) {
    if (state.authStatus !== "ready") return false;
    setNotice(elements.projectsStatus, "");
    if (!background && !state.projectsLoaded) {
      renderSkeleton(elements.projectsList, 1);
    }
    try {
      const [projectsResponse, settingsResponse, runtimeHealthResponse] = await Promise.all([
        refreshRuntimeSelection
          ? runtimeSelectionRefresh()
          : apiRequest("/api/sessions/projects"),
        apiRequest("/api/sessions/project-settings"),
        apiRequest("/api/runtime/bindings/health"),
      ]);
      if (background && shouldDeferProjectUpdate()) {
        state.pendingProjectUpdate = { projectsResponse, settingsResponse, runtimeHealthResponse };
        return;
      }
      applyProjectPayload(projectsResponse, settingsResponse, { background, runtimeHealthResponse });
      if (!background) scheduleProjectPoll();
      return true;
    } catch (error) {
      if (!background) elements.projectsList.innerHTML = "";
      if (isAuthUnavailableError(error)) return false;
      setNotice(elements.projectsStatus, `Не удалось загрузить проекты: ${error.message}`, "error");
      return false;
    }
  }

  async function loadAccountDetails(account) {
    const [authResult, quotaResult] = await Promise.allSettled([
      apiRequest(`/api/providers/codex/accounts/${encodeURIComponent(account.account_slug)}/auth-status`),
      apiRequest(`/api/providers/codex/accounts/${encodeURIComponent(account.account_slug)}/quota`),
    ]);
    return {
      account,
      auth: authResult.status === "fulfilled" ? authResult.value : null,
      quota: quotaResult.status === "fulfilled" ? quotaResult.value : null,
    };
  }

  function storeAccountDetails(details) {
    for (const detail of details) {
      state.accountDetails.set(detail.account.account_slug, {
        auth: detail.auth,
        quota: detail.quota,
      });
    }
  }

  function renderAccountCards() {
    state.resetCreditActions.clear();
    state.resetCreditActionCounter = 0;
    elements.accountsList.innerHTML = state.accounts.map((account) => {
      const detail = state.accountDetails.get(account.account_slug) || {};
      return accountCard(account, detail.auth || null, detail.quota || null);
    }).join("");
  }

  function setQuotaFreshness(accountSlug, status, quotaSnapshot = null) {
    state.quotaFreshnessByAccount.set(accountSlug, {
      status,
      fetchedAt: quotaSnapshot?.fetched_at || state.accountDetails.get(accountSlug)?.quota?.fetched_at || null,
    });
  }

  function applyAccountQuotaSnapshot(accountSlug, quotaSnapshot, freshnessStatus = "fresh") {
    const current = state.accountDetails.get(accountSlug) || {};
    state.accountDetails.set(accountSlug, {
      ...current,
      quota: quotaSnapshot,
    });
    setQuotaFreshness(accountSlug, freshnessStatus, quotaSnapshot);
    renderAccountCards();
    renderProjects();
  }

  function markQuotaRefreshFailure(accountSlug) {
    const cached = state.accountDetails.get(accountSlug)?.quota || null;
    setQuotaFreshness(accountSlug, cached?.status === "ok" ? "stale" : "unavailable", cached);
  }

  async function loadCachedQuotaForAccount(accountSlug) {
    const quota = await apiRequest(`/api/providers/codex/accounts/${encodeURIComponent(accountSlug)}/quota`);
    applyAccountQuotaSnapshot(accountSlug, quota, quota?.status === "ok" ? "fresh" : "unavailable");
    return quota;
  }

  async function loadAccounts({ initialQuotaRefresh = false } = {}) {
    if (state.authStatus !== "ready") return false;
    setNotice(elements.accountsStatus, "");
    renderSkeleton(elements.accountsList, 1);
    try {
      const response = await apiRequest("/api/providers/codex/accounts");
      state.accounts = response.accounts || [];
      if (initialQuotaRefresh) {
        for (const account of authorizedAccounts()) {
          setQuotaFreshness(account.account_slug, "pending");
        }
      }
      if (!response.accounts?.length) {
        state.accountDetails.clear();
        renderEmpty(elements.accountsList, "Аккаунтов пока нет", "Добавьте первый аккаунт через штатную авторизацию Codex.");
        renderProjects();
        return true;
      }
      const details = await Promise.all(response.accounts.map(loadAccountDetails));
      if (state.authStatus !== "ready") {
        elements.accountsList.replaceChildren();
        return false;
      }
      storeAccountDetails(details);
      renderAccountCards();
      renderProjects();
      return true;
    } catch (error) {
      elements.accountsList.innerHTML = "";
      if (isAuthUnavailableError(error)) return false;
      setNotice(elements.accountsStatus, `Не удалось загрузить аккаунты: ${error.message}`, "error");
      return false;
    }
  }

  function boundedRefreshError(error) {
    const message = error instanceof Error ? error.message : "Неизвестная ошибка";
    return message.length > 160 ? `${message.slice(0, 157)}…` : message;
  }

  function quotaRefreshPath(accountSlug) {
    return `/api/providers/codex/accounts/${encodeURIComponent(accountSlug)}/quota/refresh`;
  }

  function updateRefreshButtonContext() {
    const label = state.activeTab === "accounts"
      ? "Обновить лимиты всех аккаунтов"
      : "Обновить состояние проектов";
    elements.refreshButton.setAttribute("aria-label", label);
    elements.refreshButton.title = label;
    elements.refreshButton.disabled = state.authStatus !== "ready"
      || state.topRefreshInFlight
      || (state.activeTab === "accounts" && state.quotaInitialRefreshInFlight)
      || (state.activeTab === "projects" && state.runtimeSelectionInitialRefreshInFlight);
  }

  async function runBoundedWorkers(items, worker, concurrency = 2) {
    let nextIndex = 0;
    async function boundedWorker() {
      while (nextIndex < items.length) {
        const item = items[nextIndex];
        nextIndex += 1;
        await worker(item);
      }
    }
    const workerCount = Math.min(concurrency, items.length);
    await Promise.all(Array.from({ length: workerCount }, () => boundedWorker()));
  }

  async function refreshInitialAccountQuotas(generation) {
    const selected = authorizedAccounts();
    try {
      await runBoundedWorkers(selected, async (account) => {
        if (state.authStatus !== "ready" || state.authReadyGeneration !== generation) return;
        try {
          const quota = await apiRequest(quotaRefreshPath(account.account_slug), {
            method: "POST",
          });
          if (state.authStatus !== "ready" || state.authReadyGeneration !== generation) return;
          applyAccountQuotaSnapshot(account.account_slug, quota, "fresh");
        } catch (error) {
          if (
            isAuthUnavailableError(error)
            || state.authStatus !== "ready"
            || state.authReadyGeneration !== generation
          ) return;
          markQuotaRefreshFailure(account.account_slug);
          renderAccountCards();
        }
      }, 2);
    } finally {
      if (state.quotaInitialRefreshGeneration === generation) {
        state.quotaInitialRefreshInFlight = false;
        updateRefreshButtonContext();
      }
    }
  }

  async function refreshAllAccountQuotas() {
    if (state.topRefreshInFlight || state.quotaInitialRefreshInFlight) return;
    state.topRefreshInFlight = true;
    updateRefreshButtonContext();
    const scrollX = window.scrollX;
    const scrollY = window.scrollY;
    try {
      await refreshUpgradeDiscovery();
      const discovered = await loadAccounts();
      if (!discovered) return;
      const selected = authorizedAccounts();
      let successCount = 0;
      const failures = [];
      for (const account of selected) {
        setQuotaFreshness(account.account_slug, "pending");
      }
      renderAccountCards();
      await runBoundedWorkers(selected, async (account) => {
        if (state.authStatus !== "ready") return;
        try {
          const quota = await apiRequest(quotaRefreshPath(account.account_slug), {
            method: "POST",
          });
          if (state.authStatus !== "ready") return;
          applyAccountQuotaSnapshot(account.account_slug, quota, "fresh");
          successCount += 1;
        } catch (error) {
          if (isAuthUnavailableError(error) || state.authStatus !== "ready") return;
          markQuotaRefreshFailure(account.account_slug);
          renderAccountCards();
          failures.push(`${account.label || account.account_slug}: ${boundedRefreshError(error)}`);
        }
      }, 2);
      const summary = [`Лимиты обновлены: ${successCount}`, `Не удалось обновить: ${failures.length}`];
      if (failures.length) {
        const visibleFailures = failures.slice(0, 10);
        if (failures.length > visibleFailures.length) {
          visibleFailures.push(`Ещё ошибок: ${failures.length - visibleFailures.length}`);
        }
        summary.push(visibleFailures.join("; "));
      }
      setNotice(elements.accountsStatus, summary.join(" · "), failures.length ? "error" : "success");
    } finally {
      state.topRefreshInFlight = false;
      updateRefreshButtonContext();
      requestAnimationFrame(() => window.scrollTo(scrollX, scrollY));
    }
  }

  function runtimeSelectionRefresh() {
    return apiRequest("/api/sessions/runtime-selection/refresh", { method: "POST" });
  }

  async function refreshInitialRuntimeSelections(generation) {
    try {
      const projectsResponse = await runtimeSelectionRefresh();
      if (
        state.authStatus === "ready"
        && state.authReadyGeneration === generation
        && state.activeTab === "projects"
        && state.projectsLoaded
      ) {
        const settingsResponse = await apiRequest("/api/sessions/project-settings");
        applyProjectPayload(projectsResponse, settingsResponse, { background: true });
      }
    } catch (error) {
      if (
        state.authStatus === "ready"
        && state.authReadyGeneration === generation
        && state.activeTab === "projects"
        && !isAuthUnavailableError(error)
      ) {
        setNotice(elements.projectsStatus, "Текущие параметры Codex определить не удалось.", "error");
      }
    } finally {
      if (state.runtimeSelectionInitialRefreshGeneration === generation) {
        state.runtimeSelectionInitialRefreshInFlight = false;
        updateRefreshButtonContext();
      }
    }
  }

  async function refreshProjectsState() {
    if (state.topRefreshInFlight) return;
    state.topRefreshInFlight = true;
    updateRefreshButtonContext();
    try {
      const [projectsResponse, settingsResponse] = await Promise.all([
        refreshUpgradeDiscovery().then(() => runtimeSelectionRefresh()),
        apiRequest("/api/sessions/project-settings"),
      ]);
      if (shouldDeferProjectUpdate()) {
        state.pendingProjectUpdate = { projectsResponse, settingsResponse };
      } else {
        applyProjectPayload(projectsResponse, settingsResponse, { background: true });
      }
    } catch (error) {
      if (!isAuthUnavailableError(error)) {
        setNotice(elements.projectsStatus, `Не удалось обновить текущие параметры: ${error.message}`, "error");
      }
    } finally {
      state.topRefreshInFlight = false;
      updateRefreshButtonContext();
    }
  }

  function refreshCurrentTab() {
    if (state.authStatus !== "ready" || state.topRefreshInFlight) return;
    if (state.activeTab === "accounts") {
      void refreshAllAccountQuotas();
      return;
    }
    void refreshProjectsState();
  }

  async function refreshQuota(accountSlug, button) {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "Обновление…";
    setQuotaFreshness(accountSlug, "pending");
    renderAccountCards();
    try {
      const quota = await apiRequest(quotaRefreshPath(accountSlug), { method: "POST" });
      applyAccountQuotaSnapshot(accountSlug, quota, "fresh");
    } catch (error) {
      if (isAuthUnavailableError(error)) return;
      markQuotaRefreshFailure(accountSlug);
      renderAccountCards();
      setNotice(elements.accountsStatus, `Не удалось обновить лимиты ${accountSlug}: ${error.message}`, "error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  function openResetCreditDialog(button) {
    const selection = state.resetCreditActions.get(button.dataset.resetCreditAction || "");
    if (!selection) return;
    const accountSlug = selection.accountSlug;
    const credit = { ...selection.credit };
    const expiry = resetCreditExpiryState(credit.expires_at);
    state.resetDialog = {
      accountSlug,
      credit,
      idempotencyKey: crypto.randomUUID(),
      busy: false,
    };
    setNotice(elements.resetCreditError, "");
    elements.resetCreditSummary.innerHTML = `
      <div class="confirmation-summary">
        <div><span>Аккаунт</span><strong>${escapeHtml(accountLabel(accountSlug))}</strong></div>
        <div><span>Сброс</span><strong>${escapeHtml(credit.title)}</strong></div>
        ${expiry.datetime
          ? `<div><span>Действует до</span><strong><time datetime="${escapeHtml(expiry.datetime)}">${escapeHtml(expiry.exact)}</time></strong></div>
             <div><span>Осталось</span><strong>${escapeHtml(expiry.relative)}</strong></div>`
          : ""}
      </div>
      ${selection.generic ? "<p>Будет использован следующий доступный сохранённый сброс.</p>" : ""}
      <p>Действие применяется ко всему аккаунту и всем проектам, которые используют этот аккаунт.</p>
      <p>При подтверждении один сохранённый сброс может быть израсходован.</p>
      <p>Запрос не будет автоматически повторён с новым ключом операции.</p>`;
    elements.resetCreditDialog.classList.remove("is-hidden");
  }

  function closeResetCreditDialog() {
    if (state.resetDialog.busy) return;
    state.resetDialog = { accountSlug: "", credit: null, idempotencyKey: null, busy: false };
    elements.resetCreditDialog.classList.add("is-hidden");
  }

  async function confirmResetCredit() {
    if (
      !state.resetDialog.accountSlug
      || !state.resetDialog.idempotencyKey
      || state.resetDialog.busy
    ) return;
    const accountSlug = state.resetDialog.accountSlug;
    state.resetDialog.busy = true;
    state.mutationInFlight = true;
    elements.confirmResetCreditButton.disabled = true;
    setNotice(elements.resetCreditError, "");
    try {
      const response = await apiRequest(`/api/providers/codex/accounts/${encodeURIComponent(accountSlug)}/rate-limit-reset-credits/consume`, {
        method: "POST",
        body: JSON.stringify({
          idempotency_key: state.resetDialog.idempotencyKey,
          credit_id: state.resetDialog.credit?.credit_id || null,
        }),
      });
      if (!response.success) {
        setNotice(elements.resetCreditError, response.outcome === "noCredit" ? "Нет доступного кредита сброса." : "Сейчас нечего сбрасывать.", "error");
        return;
      }
      state.resetDialog.busy = false;
      closeResetCreditDialog();
      if (response.quota) {
        applyAccountQuotaSnapshot(accountSlug, response.quota, "fresh");
      } else {
        try {
          await loadCachedQuotaForAccount(accountSlug);
        } catch (error) {
          if (!isAuthUnavailableError(error)) {
            markQuotaRefreshFailure(accountSlug);
            renderAccountCards();
            setNotice(elements.accountsStatus, "Сброс применён, но обновить сохранённые данные не удалось.", "error");
          }
        }
      }
    } catch (error) {
      setNotice(elements.resetCreditError, error.message, "error");
    } finally {
      state.resetDialog.busy = false;
      state.mutationInFlight = false;
      elements.confirmResetCreditButton.disabled = false;
    }
  }

  function archiveActionAvailable(project, archived) {
    return Boolean(
      project?.project_id
      && project.inventory_identity_kind
      && (!archived || (project.archive_available && !project.tmux_session_running))
    );
  }

  function renderArchiveDialog() {
    const { project, archived, busy, error } = state.archiveDialog;
    if (!project) return;
    const available = archiveActionAvailable(project, archived);
    elements.archiveDialogTitle.textContent = archived ? "Отправить проект в Архив" : "Вернуть проект";
    elements.confirmArchiveButton.textContent = archived ? "Отправить проект в Архив" : "Вернуть в рабочие проекты";
    elements.confirmArchiveButton.disabled = busy || !available;
    elements.cancelArchiveButton.disabled = busy;
    elements.archiveSummary.innerHTML = archived
      ? `
        <div class="confirmation-summary">
          <div><span>Проект</span><strong>${escapeHtml(project.topic_name || "Проект")}</strong></div>
        </div>
        <p>Проект исчезнет из рабочих списков ЦУС и появится в разделе „Архивные проекты“.</p>
        <p>Telegram-тема, файлы, настройки запуска, tmux-состояние и транскрипты не удаляются.</p>
        ${available ? "" : `<p class="archive-guidance">${escapeHtml(project.archive_unavailable_reason || "Архивирование сейчас недоступно.")}</p>`}`
      : `
        <div class="confirmation-summary">
          <div><span>Проект</span><strong>${escapeHtml(project.topic_name || "Проект")}</strong></div>
        </div>
        <p>Проект снова появится среди рабочих проектов ЦУС.</p>`;
    setNotice(elements.archiveError, error, "error");
  }

  function openArchiveDialog(projectKeyValue, archived) {
    const project = projectByKey(projectKeyValue);
    if (!project) {
      setNotice(elements.projectsStatus, "Проект не найден в текущем списке. Обновите проекты и повторите.", "error");
      return;
    }
    state.archiveDialog = {
      projectKey: projectKeyValue,
      archived,
      project,
      busy: false,
      error: "",
    };
    renderArchiveDialog();
    elements.archiveDialog.classList.remove("is-hidden");
    showTelegramBackButton(closeArchiveDialog);
  }

  function closeArchiveDialog({ force = false } = {}) {
    if (state.archiveDialog.busy && !force) return;
    state.archiveDialog = { projectKey: "", archived: false, project: null, busy: false, error: "" };
    elements.archiveDialog.classList.add("is-hidden");
    hideTelegramBackButton();
  }

  async function confirmArchiveDialog() {
    const { project, archived } = state.archiveDialog;
    if (!archiveActionAvailable(project, archived)) return;
    const scrollAnchor = captureProjectScrollAnchor();
    state.archiveDialog.busy = true;
    state.archiveDialog.error = "";
    state.mutationInFlight = true;
    setClosingConfirmation(true);
    renderArchiveDialog();
    try {
      await apiRequest(`/api/sessions/project-inventory/${encodeURIComponent(project.project_id)}`, {
        method: "PUT",
        body: JSON.stringify({
          project_path: project.inventory_identity_kind === "project_path" ? project.project_path : null,
          identity_kind: project.inventory_identity_kind,
          archived,
          expected_revision: project.archive_revision ?? null,
          reason: archived ? "manual_archive" : "manual_unarchive",
        }),
      });
      closeArchiveDialog({ force: true });
      await loadProjects();
      restoreProjectScrollAnchor(scrollAnchor);
    } catch (error) {
      if (error.code === "revision_conflict") {
        await loadProjects();
        restoreProjectScrollAnchor(scrollAnchor);
        state.archiveDialog.error = "Архив изменился в другом окне. Проверьте свежий список и повторите.";
      } else {
        state.archiveDialog.error = error.message;
      }
      renderArchiveDialog();
    } finally {
      state.archiveDialog.busy = false;
      state.mutationInFlight = false;
      setClosingConfirmation(false);
      renderArchiveDialog();
    }
  }

  function preferredAccount(project) {
    return project.account_profile
      || project.runtime_account_profile
      || project.account_slug
      || authorizedAccounts()[0]?.account_slug
      || "";
  }

  function currentCapabilities(accountSlug) {
    return state.capabilityCache.get(accountSlug) || null;
  }

  function modelsFromCapabilities(snapshot) {
    return snapshot?.models?.items || [];
  }

  function visibleModels(project, snapshot) {
    const savedModel = project.model || project.runtime_model || "";
    return modelsFromCapabilities(snapshot).filter((model) => model.hidden !== true || model.model_id === savedModel);
  }

  function sortEfforts(values) {
    return [...new Set(values.filter(Boolean))].sort((a, b) => {
      const aIndex = effortOrder.indexOf(a);
      const bIndex = effortOrder.indexOf(b);
      if (aIndex !== -1 && bIndex !== -1) return aIndex - bIndex;
      if (aIndex !== -1) return -1;
      if (bIndex !== -1) return 1;
      return a.localeCompare(b);
    });
  }

  function selectedModelItem(project) {
    const snapshot = currentCapabilities(state.settingsDialog.selections.account);
    return visibleModels(project, snapshot).find((model) => model.model_id === state.settingsDialog.selections.model) || null;
  }

  function setSettingsBusy(isBusy) {
    state.settingsDialog.busy = isBusy;
    const controls = [
      elements.settingsPathInput,
      elements.bindProjectPathButton,
      elements.settingsAccountSelect,
      elements.settingsModelSelect,
      elements.settingsReasoningSelect,
      elements.refreshCapabilitiesButton,
      elements.openApplyConfirmationButton,
      elements.confirmApplySettingsButton,
      elements.backToSettingsEditButton,
    ];
    controls.forEach((control) => { control.disabled = isBusy; });
    updateApplyAvailability();
  }

  function setClosingConfirmation(enabled) {
    const telegram = getTelegramWebApp();
    if (!telegram) return;
    if (enabled && typeof telegram.enableClosingConfirmation === "function") {
      telegram.enableClosingConfirmation();
    }
    if (!enabled && typeof telegram.disableClosingConfirmation === "function") {
      telegram.disableClosingConfirmation();
    }
  }

  function showTelegramBackButton(handler) {
    const telegram = getTelegramWebApp();
    const backButton = telegram?.BackButton;
    if (!backButton || typeof backButton.show !== "function") return;
    if (state.settingsBackButtonHandler && typeof backButton.offClick === "function") {
      backButton.offClick(state.settingsBackButtonHandler);
    }
    state.settingsBackButtonHandler = handler;
    if (typeof backButton.onClick === "function") backButton.onClick(handler);
    backButton.show();
  }

  function hideTelegramBackButton() {
    const telegram = getTelegramWebApp();
    const backButton = telegram?.BackButton;
    if (!backButton) return;
    if (state.settingsBackButtonHandler && typeof backButton.offClick === "function") {
      backButton.offClick(state.settingsBackButtonHandler);
    }
    state.settingsBackButtonHandler = null;
    if (typeof backButton.hide === "function") backButton.hide();
  }

  async function loadCapabilities(accountSlug, { force = false } = {}) {
    if (!accountSlug) return null;
    if (!force && state.capabilityCache.has(accountSlug)) {
      return state.capabilityCache.get(accountSlug);
    }
    const path = force
      ? `/api/providers/codex/accounts/${encodeURIComponent(accountSlug)}/capabilities/refresh`
      : `/api/providers/codex/accounts/${encodeURIComponent(accountSlug)}/capabilities`;
    const snapshot = await apiRequest(path, { method: force ? "POST" : "GET" });
    state.capabilityCache.set(accountSlug, snapshot);
    return snapshot;
  }

  async function ensureCapabilities(accountSlug) {
    setCapabilityLoading(true);
    try {
      const snapshot = await loadCapabilities(accountSlug);
      setNotice(elements.settingsEditNotice, "");
      return snapshot;
    } catch (error) {
      const cached = currentCapabilities(accountSlug);
      const message = cached
        ? `Не удалось обновить список моделей. Показан последний сохранённый список. ${error.message}`
        : `Нет доступного списка моделей. Нажмите «Обновить модели». ${error.message}`;
      setNotice(elements.settingsEditNotice, message, cached ? "warning" : "error");
      return cached;
    } finally {
      setCapabilityLoading(false);
    }
  }

  function setTopicOnboardingBusy(busy) {
    state.topicOnboarding.busy = busy;
    const controlsDisabled = busy || state.topicOnboarding.record?.state === "CONFIGURED";
    [
      elements.onboardingProjectPath,
      elements.onboardingAccount,
      elements.onboardingModel,
      elements.onboardingReasoning,
      elements.onboardingRefreshCapabilities,
    ].forEach((control) => { control.disabled = controlsDisabled; });
    updateTopicOnboardingAvailability();
  }

  function topicOnboardingModels() {
    const account = state.topicOnboarding.selections.account;
    return modelsFromCapabilities(currentCapabilities(account)).filter((model) => model.hidden !== true);
  }

  function renderTopicOnboardingReasoning() {
    const model = topicOnboardingModels().find(
      (item) => item.model_id === state.topicOnboarding.selections.model,
    );
    const efforts = sortEfforts(
      model?.supported_reasoning_efforts?.length
        ? model.supported_reasoning_efforts
        : model?.default_reasoning_effort
        ? [model.default_reasoning_effort]
        : [],
    );
    const selected = efforts.includes(state.topicOnboarding.selections.reasoning)
      ? state.topicOnboarding.selections.reasoning
      : model?.default_reasoning_effort || efforts[0] || "";
    elements.onboardingReasoning.innerHTML = efforts.map((effort) => (
      `<option value="${escapeHtml(effort)}">${escapeHtml(effortLabels[effort] || effort)}</option>`
    )).join("");
    elements.onboardingReasoning.value = selected;
    state.topicOnboarding.selections.reasoning = selected;
    elements.onboardingReasoningHelp.textContent = efforts.length
      ? "Варианты ограничены выбранной моделью."
      : "Для модели не указаны поддерживаемые уровни reasoning.";
    updateTopicOnboardingAvailability();
  }

  function renderTopicOnboardingModels() {
    const models = topicOnboardingModels();
    const selected = models.some(
      (model) => model.model_id === state.topicOnboarding.selections.model,
    )
      ? state.topicOnboarding.selections.model
      : models.find((model) => model.is_default)?.model_id || models[0]?.model_id || "";
    elements.onboardingModel.innerHTML = models.map((model) => {
      const label = model.display_name
        ? `${model.display_name} (${model.model_id})`
        : model.model_id;
      return `<option value="${escapeHtml(model.model_id)}">${escapeHtml(label)}</option>`;
    }).join("");
    elements.onboardingModel.value = selected;
    state.topicOnboarding.selections.model = selected;
    elements.onboardingModelHelp.textContent = models.length
      ? "Модель подтверждена capabilities выбранного аккаунта."
      : "Список моделей пуст. Обновите capabilities или выберите другой аккаунт.";
    renderTopicOnboardingReasoning();
  }

  async function loadTopicOnboardingCapabilities({ force = false } = {}) {
    const account = state.topicOnboarding.selections.account;
    if (!account) {
      renderTopicOnboardingModels();
      return;
    }
    setTopicOnboardingBusy(true);
    setNotice(elements.onboardingError, "");
    try {
      await loadCapabilities(account, { force });
      renderTopicOnboardingModels();
      if (force) setNotice(elements.onboardingNotice, "Список моделей обновлён.", "success");
    } catch (error) {
      renderTopicOnboardingModels();
      setNotice(elements.onboardingError, `Не удалось загрузить модели: ${error.message}`, "error");
    } finally {
      setTopicOnboardingBusy(false);
    }
  }

  function renderTopicOnboardingAccounts() {
    const accounts = authorizedAccounts();
    elements.onboardingAccount.innerHTML = accounts.map((account) => (
      `<option value="${escapeHtml(account.account_slug)}">${escapeHtml(account.label || account.account_slug)}</option>`
    )).join("");
    state.topicOnboarding.selections.account = accounts[0]?.account_slug || "";
    elements.onboardingAccount.value = state.topicOnboarding.selections.account;
    if (!accounts.length) {
      setNotice(
        elements.onboardingError,
        "Нет авторизованного аккаунта Codex. Добавьте аккаунт в CUS и откройте настройку снова.",
        "error",
      );
    }
  }

  function renderTopicOnboardingProjectPaths(projects) {
    const paths = [...new Set((projects || []).map((project) => project.project_path).filter(Boolean))]
      .sort((left, right) => left.localeCompare(right));
    elements.onboardingProjectPaths.replaceChildren(...paths.map((path) => {
      const option = document.createElement("option");
      option.value = path;
      return option;
    }));
  }

  function updateTopicOnboardingAvailability() {
    const selections = state.topicOnboarding.selections;
    const completed = state.topicOnboarding.record?.state === "CONFIGURED";
    elements.onboardingApply.disabled = completed || state.topicOnboarding.busy || !(
      elements.onboardingProjectPath.value.trim()
      && selections.account
      && selections.model
      && selections.reasoning
    );
    elements.onboardingApply.textContent = completed
      ? "Настройка завершена"
      : state.topicOnboarding.busy
      ? "Применяем…"
      : "Применить настройки";
  }

  function closeTopicOnboarding() {
    const telegram = getTelegramWebApp();
    if (telegram && typeof telegram.close === "function") {
      telegram.close();
      return;
    }
    setNotice(
      elements.onboardingNotice,
      "Настройка завершена. Вернитесь в Telegram-топик и начинайте работу.",
      "success",
    );
  }

  async function finishConfiguredTopicOnboarding() {
    setTopicOnboardingBusy(true);
    setNotice(elements.onboardingError, "");
    try {
      await apiRequest(`/api/onboarding/${encodeURIComponent(topicOnboardingToken)}/complete`, {
        method: "POST",
      });
      closeTopicOnboarding();
    } catch (error) {
      setNotice(elements.onboardingError, error.message, "error");
    } finally {
      setTopicOnboardingBusy(false);
    }
  }

  async function loadTopicOnboarding() {
    setTopicOnboardingBusy(true);
    setNotice(elements.onboardingError, "");
    const [recordResult, accountsResult, projectsResult] = await Promise.allSettled([
      apiRequest(`/api/onboarding/${encodeURIComponent(topicOnboardingToken)}`),
      apiRequest("/api/providers/codex/accounts"),
      apiRequest("/api/sessions/projects"),
    ]);
    try {
      if (recordResult.status === "rejected") throw recordResult.reason;
      const record = recordResult.value;
      state.topicOnboarding.record = record;
      elements.onboardingTopicName.textContent = record.topic_name;
      elements.onboardingState.textContent = record.state === "CONFIGURED"
        ? "Настройки применены"
        : "Ожидает настройки";
      if (record.state === "CONFIGURED") {
        if (record.completion_notified) {
          closeTopicOnboarding();
        } else {
          await finishConfiguredTopicOnboarding();
        }
        return;
      }
      if (accountsResult.status === "rejected") throw accountsResult.reason;
      state.accounts = accountsResult.value.accounts || [];
      renderTopicOnboardingAccounts();
      if (projectsResult.status === "fulfilled") {
        renderTopicOnboardingProjectPaths(projectsResult.value.projects || []);
      }
      await loadTopicOnboardingCapabilities();
    } catch (error) {
      setNotice(elements.onboardingError, error.message, "error");
    } finally {
      setTopicOnboardingBusy(false);
    }
  }

  async function applyTopicOnboarding() {
    const record = state.topicOnboarding.record;
    if (!record || state.topicOnboarding.busy) return;
    setTopicOnboardingBusy(true);
    setClosingConfirmation(true);
    setNotice(elements.onboardingError, "");
    setNotice(elements.onboardingNotice, "Применяем настройки в Runtime…");
    try {
      const result = await apiRequest(
        `/api/onboarding/${encodeURIComponent(topicOnboardingToken)}/apply`,
        {
          method: "POST",
          body: JSON.stringify({
            onboarding_id: record.onboarding_id,
            project_path: elements.onboardingProjectPath.value.trim(),
            provider: "codex",
            account_profile: state.topicOnboarding.selections.account,
            model: state.topicOnboarding.selections.model,
            reasoning_effort: state.topicOnboarding.selections.reasoning,
          }),
        },
      );
      if (result.status !== "CONFIGURED" || result.close_miniapp !== true) {
        throw new ApiError("Backend не подтвердил завершение настройки.");
      }
      state.topicOnboarding.record = {
        ...record,
        state: "CONFIGURED",
        completion_notified: true,
      };
      elements.onboardingState.textContent = "Настройка завершена";
      setNotice(elements.onboardingNotice, "Настройка завершена. Возвращаемся в Telegram…", "success");
      closeTopicOnboarding();
    } catch (error) {
      setNotice(elements.onboardingNotice, "");
      setNotice(elements.onboardingError, error.message, "error");
    } finally {
      setClosingConfirmation(false);
      setTopicOnboardingBusy(false);
    }
  }

  function setCapabilityLoading(isLoading) {
    elements.settingsAccountSelect.disabled = isLoading || state.settingsDialog.busy;
    elements.settingsModelSelect.disabled = isLoading || state.settingsDialog.busy;
    elements.settingsReasoningSelect.disabled = isLoading || state.settingsDialog.busy;
    elements.refreshCapabilitiesButton.disabled = isLoading || state.settingsDialog.busy;
    elements.settingsModelHelp.textContent = isLoading ? "Загружаем модели…" : elements.settingsModelHelp.textContent;
    updateApplyAvailability();
  }

  function renderAccountOptions(project) {
    const candidates = authorizedAccounts();
    const selected = state.settingsDialog.selections.account;
    const hasSelected = candidates.some((account) => account.account_slug === selected);
    state.settingsDialog.unavailable.account = Boolean(selected && !hasSelected);
    const options = [];
    if (state.settingsDialog.unavailable.account) {
      options.push(`<option value="${escapeHtml(selected)}">${escapeHtml(accountLabel(selected))} — недоступен</option>`);
    }
    options.push(...candidates.map((account) =>
      `<option value="${escapeHtml(account.account_slug)}">${escapeHtml(account.label || account.account_slug)}</option>`
    ));
    elements.settingsAccountSelect.innerHTML = options.join("");
    elements.settingsAccountSelect.value = selected || candidates[0]?.account_slug || "";
    state.settingsDialog.selections.account = elements.settingsAccountSelect.value;
    if (!candidates.length) {
      setNotice(elements.settingsEditError, "Нет читаемых авторизованных аккаунтов Codex.", "error");
    }
    elements.settingsProjectTopic.textContent = project.topic_name || "Проект";
    elements.settingsProjectPath.textContent = project.project_path || "Путь не указан";
    elements.settingsRuntimeStatus.textContent = project.onboarding_state === "UNCONFIGURED"
      ? "Первый запуск после следующего сообщения"
      : "Настройки следующего запуска";
  }

  function renderModelOptions(project) {
    const account = state.settingsDialog.selections.account;
    const snapshot = currentCapabilities(account);
    const models = visibleModels(project, snapshot);
    let selected = state.settingsDialog.selections.model;
    if (!selected) {
      selected = models.find((model) => model.is_default)?.model_id || models[0]?.model_id || "";
    }
    const hasSelected = models.some((model) => model.model_id === selected);
    state.settingsDialog.unavailable.model = Boolean(selected && !hasSelected);
    const options = [];
    if (state.settingsDialog.unavailable.model) {
      options.push(`<option value="${escapeHtml(selected)}">${escapeHtml(selected)} — недоступна</option>`);
    }
    options.push(...models.map((model) => {
      const label = model.display_name ? `${model.display_name} (${model.model_id})` : model.model_id;
      return `<option value="${escapeHtml(model.model_id)}">${escapeHtml(label)}</option>`;
    }));
    elements.settingsModelSelect.innerHTML = options.join("");
    elements.settingsModelSelect.value = selected;
    state.settingsDialog.selections.model = selected;
    const status = snapshot?.models?.status;
    if (!snapshot || !models.length || status !== "ok") {
      elements.settingsModelHelp.textContent = "Список моделей пуст или требует обновления.";
    } else if (state.settingsDialog.unavailable.model) {
      elements.settingsModelHelp.textContent = "Сохранённая модель недоступна. Выберите доступную модель явно.";
    } else {
      elements.settingsModelHelp.textContent = "Модель берётся из capabilities выбранного аккаунта.";
    }
  }

  function renderReasoningOptions(project) {
    const model = selectedModelItem(project);
    const selectedModel = state.settingsDialog.selections.model;
    let selected = state.settingsDialog.selections.reasoning;
    let efforts = [];
    if (model?.supported_reasoning_efforts?.length) {
      efforts = sortEfforts(model.supported_reasoning_efforts);
    } else if (model?.default_reasoning_effort) {
      efforts = [model.default_reasoning_effort];
    }
    if (!selected) {
      selected = model?.default_reasoning_effort || efforts[0] || "";
    }
    const hasSelected = efforts.includes(selected);
    state.settingsDialog.unavailable.reasoning = Boolean(selected && !hasSelected);
    const options = [];
    if (state.settingsDialog.unavailable.reasoning) {
      options.push(`<option value="${escapeHtml(selected)}">${escapeHtml(effortLabels[selected] || selected)} — недоступен</option>`);
    }
    options.push(...efforts.map((effort) =>
      `<option value="${escapeHtml(effort)}">${escapeHtml(effortLabels[effort] || effort)}</option>`
    ));
    if (!options.length && selectedModel) {
      options.push('<option value="">Нет поддерживаемых уровней</option>');
    }
    elements.settingsReasoningSelect.innerHTML = options.join("");
    elements.settingsReasoningSelect.value = selected;
    state.settingsDialog.selections.reasoning = selected;
    if (!model && selectedModel) {
      elements.settingsReasoningHelp.textContent = "Сначала выберите доступную модель.";
    } else if (!efforts.length) {
      elements.settingsReasoningHelp.textContent = "Для модели не указан поддерживаемый reasoning.";
    } else if (state.settingsDialog.unavailable.reasoning) {
      elements.settingsReasoningHelp.textContent = "Сохранённый reasoning недоступен. Выберите поддерживаемое значение явно.";
    } else {
      elements.settingsReasoningHelp.textContent = "Варианты ограничены выбранной моделью.";
    }
  }

  function updateApplyAvailability() {
    const project = state.settingsDialog.project;
    if (!project) return;
    const hasUnavailable = Object.values(state.settingsDialog.unavailable).some(Boolean);
    const complete = Boolean(
      state.settingsDialog.selections.account
      && state.settingsDialog.selections.model
      && state.settingsDialog.selections.reasoning
      && !hasUnavailable,
    );
    elements.openApplyConfirmationButton.disabled = state.settingsDialog.busy || !complete;
    elements.confirmApplySettingsButton.disabled = state.settingsDialog.busy || !complete;
  }

  async function renderSettingsForm(project) {
    renderAccountOptions(project);
    await ensureCapabilities(state.settingsDialog.selections.account);
    renderModelOptions(project);
    renderReasoningOptions(project);
    updateApplyAvailability();
  }

  async function openProjectSettingsDialog(projectKeyValue) {
    const project = projectByKey(projectKeyValue);
    const onboarding = project?.onboarding_state === "UNCONFIGURED";
    const managedBinding = project?.runtime_binding_health?.lifecycle_status === "CONFIGURED";
    if (!project || (!onboarding && !managedBinding && (project.agent !== "codex" || !project.project_path))) return;
    state.settingsDialog.project = project;
    state.settingsDialog.path = project.project_path || "";
    setNotice(elements.settingsPathError, "");
    setNotice(elements.settingsEditNotice, "");
    setNotice(elements.settingsEditError, "");
    setNotice(elements.settingsConfirmError, "");
    elements.projectSettingsDialog.classList.remove("is-hidden");
    showTelegramBackButton(handleSettingsBack);
    if (onboarding && !project.project_path) {
      state.settingsDialog.step = "path";
      elements.settingsPathInput.value = "";
      elements.settingsPathStep.classList.remove("is-hidden");
      elements.settingsEditStep.classList.add("is-hidden");
      elements.settingsConfirmStep.classList.add("is-hidden");
      setTimeout(() => elements.settingsPathInput.focus(), 30);
      return;
    }
    if (!authorizedAccounts().length) {
      closeProjectSettingsDialog({ force: true });
      setNotice(elements.projectsStatus, "Нет читаемого авторизованного аккаунта Codex. Сначала добавьте его во вкладке «Аккаунты».");
      switchTab("accounts");
      return;
    }
    state.settingsDialog.step = "edit";
    state.settingsDialog.validation = null;
    state.settingsDialog.unavailable = { account: false, model: false, reasoning: false };
    state.settingsDialog.selections = {
      account: managedBinding ? project.runtime_binding_health.desired.account_profile : preferredAccount(project),
      model: project.model || project.runtime_model || "",
      reasoning: project.reasoning_effort || project.runtime_reasoning_effort || "",
    };
    elements.settingsPathStep.classList.add("is-hidden");
    elements.settingsEditStep.classList.remove("is-hidden");
    elements.settingsConfirmStep.classList.add("is-hidden");
    await renderSettingsForm(project);
    setTimeout(() => elements.settingsAccountSelect.focus(), 30);
  }

  function closeProjectSettingsDialog({ force = false } = {}) {
    if (state.settingsDialog.busy && !force) return;
    state.settingsDialog.project = null;
    state.settingsDialog.path = "";
    state.settingsDialog.validation = null;
    elements.projectSettingsDialog.classList.add("is-hidden");
    hideTelegramBackButton();
  }

  function handleSettingsBack() {
    if (state.settingsDialog.busy) return;
    if (state.settingsDialog.step === "confirm") {
      showSettingsEditStep();
      return;
    }
    closeProjectSettingsDialog();
  }

  function showSettingsEditStep() {
    state.settingsDialog.step = "edit";
    elements.settingsConfirmStep.classList.add("is-hidden");
    elements.settingsPathStep.classList.add("is-hidden");
    elements.settingsEditStep.classList.remove("is-hidden");
  }

  async function bindProjectPath() {
    const project = state.settingsDialog.project;
    if (!project || state.settingsDialog.busy) return;
    const projectPath = elements.settingsPathInput.value.trim();
    setSettingsBusy(true);
    setNotice(elements.settingsPathError, "");
    try {
      const response = await apiRequest("/api/sessions/project-onboarding/bind-path", {
        method: "POST",
        body: JSON.stringify({ project_id: project.project_id, project_path: projectPath }),
      });
      if (response.status !== "bound") {
        throw new ApiError("Backend не подтвердил привязку пути.", { code: "PATH_INVALID" });
      }
      closeProjectSettingsDialog({ force: true });
      await loadProjects();
      mergeProjectView();
      const rebound = state.projectView.find((item) => (
        item.chat_id === response.chat_id
        && item.thread_id === response.thread_id
        && item.project_path === response.project_path
      ));
      if (!rebound) {
        setNotice(elements.projectsStatus, "Путь привязан. Обновите состояние проектов для продолжения.", "warning");
        return;
      }
      if (!authorizedAccounts().length) {
        setNotice(elements.projectsStatus, "Путь привязан. Теперь добавьте авторизованный аккаунт Codex.", "success");
        switchTab("accounts");
        return;
      }
      await openProjectSettingsDialog(projectKey(rebound));
    } catch (error) {
      setNotice(elements.settingsPathError, error.message, "error");
    } finally {
      setSettingsBusy(false);
    }
  }

  function settingsPayload() {
    const project = state.settingsDialog.project;
    return {
      project_path: project.project_path,
      provider_id: "codex",
      account_profile: state.settingsDialog.selections.account,
      model: state.settingsDialog.selections.model,
      reasoning_effort: state.settingsDialog.selections.reasoning,
    };
  }

  async function validateSettingsPayload() {
    const validation = await apiRequest("/api/sessions/project-settings/validate", {
      method: "POST",
      body: JSON.stringify(settingsPayload()),
    });
    if (validation.status !== "valid") {
      throw new ApiError("Проверьте выбранные настройки: backend не подтвердил их как valid.", { code: "provider_settings_invalid" });
    }
    state.settingsDialog.validation = validation;
    return validation;
  }

  function selectedAccountHasCurrentCapabilities() {
    const snapshot = currentCapabilities(state.settingsDialog.selections.account);
    return snapshot?.models?.status === "ok" && (snapshot.models.items || []).length > 0 && snapshot.status !== "not_refreshed";
  }

  async function validateForApplyWithCurrentCapabilities() {
    let validation = await validateSettingsPayload();
    if (validation.capability_source === "current" && selectedAccountHasCurrentCapabilities()) {
      return validation;
    }
    await loadCapabilities(state.settingsDialog.selections.account, { force: true });
    const project = state.settingsDialog.project;
    renderModelOptions(project);
    renderReasoningOptions(project);
    updateApplyAvailability();
    validation = await validateSettingsPayload();
    if (validation.capability_source !== "current") {
      throw new ApiError("Для применения нужен свежий список моделей. Обновите модели и повторите.", {
        code: "capabilities_current_required",
      });
    }
    return validation;
  }

  function renderConfirmSummary() {
    const project = state.settingsDialog.project;
    const rows = [
      ["Проект", project.topic_name || project.project_path],
      ["Аккаунт", accountLabel(state.settingsDialog.selections.account)],
      ["Модель", state.settingsDialog.selections.model],
      ["Reasoning", effortLabels[state.settingsDialog.selections.reasoning] || state.settingsDialog.selections.reasoning],
    ];
    elements.settingsConfirmSummary.replaceChildren(...rows.map(([label, value]) => {
      const row = document.createElement("div");
      const key = document.createElement("span");
      const val = document.createElement("strong");
      key.textContent = label;
      val.textContent = value || "—";
      row.append(key, val);
      return row;
    }));
    const onboarding = project.onboarding_state === "UNCONFIGURED";
    const managedBinding = project.runtime_binding_health?.lifecycle_status === "CONFIGURED";
    elements.settingsConfirmPrimaryCopy.textContent = onboarding
      ? "После подтверждения runtime spec будет подготовлен без запуска Codex."
      : managedBinding
      ? "ЦУС сохранит выбор для этого топика; Runtime применит его после IDLE."
      : "После подтверждения проект будет запущен с выбранными настройками.";
    elements.settingsConfirmSecondaryCopy.textContent = onboarding
      ? "Первую tmux-сессию запустит следующее сообщение в этом Telegram-топике."
      : managedBinding
      ? "Активная задача не прервётся. Новая Codex-сессия запустится при следующем сообщении."
      : "Текущая задача и очередь проекта, если они есть, будут прерваны.";
  }

  async function openApplyConfirmation() {
    if (state.settingsDialog.busy) return;
    setSettingsBusy(true);
    setNotice(elements.settingsEditError, "");
    try {
      await validateForApplyWithCurrentCapabilities();
      renderConfirmSummary();
      state.settingsDialog.step = "confirm";
      elements.settingsEditStep.classList.add("is-hidden");
      elements.settingsConfirmStep.classList.remove("is-hidden");
    } catch (error) {
      setNotice(elements.settingsEditError, error.message, "error");
    } finally {
      setSettingsBusy(false);
    }
  }

  async function confirmApplySettings() {
    const project = state.settingsDialog.project;
    if (!project || state.settingsDialog.busy) return;
    setSettingsBusy(true);
    setClosingConfirmation(true);
    setNotice(elements.settingsConfirmError, "");
    try {
      const onboarding = project.onboarding_state === "UNCONFIGURED";
      const binding = project.runtime_binding_health?.lifecycle_status === "CONFIGURED"
        ? project.runtime_binding_health
        : null;
      if (binding) {
        const applyResponse = await apiRequest(
          `/api/runtime/bindings/${encodeURIComponent(binding.binding_id)}/selection`,
          {
            method: "POST",
            body: JSON.stringify({
              expected_generation_id: binding.desired.generation_id,
              expected_revision: binding.desired.revision,
              expected_spec_digest: binding.desired.spec_digest,
              account_profile: state.settingsDialog.selections.account,
              model: state.settingsDialog.selections.model,
              reasoning_effort: state.settingsDialog.selections.reasoning,
            }),
          },
        );
        if (!["unchanged", "pending_idle_or_launch", "pending_runtime"].includes(applyResponse.status)) {
          throw new ApiError("ЦУС не подтвердил сохранение настроек binding.", { code: "apply_status_unexpected" });
        }
        closeProjectSettingsDialog({ force: true });
        await loadProjects();
        setNotice(
          elements.projectsStatus,
          applyResponse.status === "unchanged"
            ? "Для этого топика уже выбраны такие настройки."
            : applyResponse.status === "pending_runtime"
            ? "Выбор сохранён в ЦУС. Runtime пока недоступен; синхронизация ожидается."
            : "Выбор сохранён. Активная задача не прервётся; Runtime применит настройки после IDLE.",
          applyResponse.status === "pending_runtime" ? "warning" : "success",
        );
        return;
      }
      const endpoint = onboarding
        ? "/api/sessions/project-onboarding/apply"
        : "/api/sessions/project-settings/apply-selection";
      const applyResponse = await apiRequest(endpoint, {
        method: "POST",
        body: JSON.stringify({
          ...settingsPayload(),
          expected_settings_revision: project.revision ?? null,
          ...(onboarding
            ? { confirm: true }
            : { confirm_interrupt: true, allow_last_known_good: false }),
        }),
      });
      if (!["applied", "no_change", "configured"].includes(applyResponse.status)) {
        throw new ApiError("Backend не подтвердил применение настроек.", { code: "apply_status_unexpected" });
      }
      closeProjectSettingsDialog({ force: true });
      const refreshed = await loadProjects({ refreshRuntimeSelection: true });
      if (!refreshed) {
        setNotice(
          elements.projectsStatus,
          "Настройки применены, но карточку не удалось обновить. Нажмите «Обновить состояние проектов».",
          "warning",
        );
        return;
      }
      setNotice(
        elements.projectsStatus,
        applyResponse.status === "configured"
          ? "Настройка завершена. Отправьте следующее сообщение в Telegram-топике — оно запустит Codex с выбранными параметрами."
          : applyResponse.status === "no_change"
          ? "Выбранные настройки уже применены."
          : "Настройки применены. Проект запущен с выбранными параметрами.",
        "success",
      );
    } catch (error) {
      await loadProjects();
      const criticalCodes = [
        "apply_failed_rollback_failed",
        "runtime_apply_recovery_required",
        "settings_apply_rollback_failed",
        "rollback_failed",
      ];
      setNotice(elements.settingsConfirmError, error.message, criticalCodes.includes(error.code) ? "critical" : "error");
      if (criticalCodes.includes(error.code)) {
        setNotice(elements.projectsStatus, error.message, "critical");
      }
    } finally {
      setClosingConfirmation(false);
      setSettingsBusy(false);
    }
  }

  async function handleAccountSelectionChange() {
    state.settingsDialog.selections.account = elements.settingsAccountSelect.value;
    state.settingsDialog.selections.model = "";
    state.settingsDialog.selections.reasoning = "";
    state.settingsDialog.unavailable = { account: false, model: false, reasoning: false };
    await renderSettingsForm(state.settingsDialog.project);
  }

  function handleModelSelectionChange() {
    state.settingsDialog.selections.model = elements.settingsModelSelect.value;
    state.settingsDialog.selections.reasoning = "";
    state.settingsDialog.unavailable.model = false;
    renderReasoningOptions(state.settingsDialog.project);
    updateApplyAvailability();
  }

  function handleReasoningSelectionChange() {
    state.settingsDialog.selections.reasoning = elements.settingsReasoningSelect.value;
    state.settingsDialog.unavailable.reasoning = false;
    updateApplyAvailability();
  }

  async function refreshSelectedCapabilities() {
    const account = state.settingsDialog.selections.account;
    if (!account) return;
    setSettingsBusy(true);
    setNotice(elements.settingsEditError, "");
    try {
      await loadCapabilities(account, { force: true });
      renderModelOptions(state.settingsDialog.project);
      renderReasoningOptions(state.settingsDialog.project);
      setNotice(elements.settingsEditNotice, "Список моделей обновлён.");
    } catch (error) {
      const cached = currentCapabilities(account);
      setNotice(
        elements.settingsEditNotice,
        cached
          ? `Обновить модели не удалось. Последний сохранённый список оставлен на экране. ${error.message}`
          : `Обновить модели не удалось. ${error.message}`,
        "error",
      );
    } finally {
      setSettingsBusy(false);
    }
  }

  function switchTab(tabName) {
    if (state.authStatus !== "ready") return;
    state.activeTab = tabName;
    elements.tabs.forEach((tab) => tab.classList.toggle("is-active", tab.dataset.tab === tabName));
    Object.entries(elements.panels).forEach(([name, panel]) => panel.classList.toggle("is-active", name === tabName));
    updateRefreshButtonContext();
    if (tabName === "projects" && !state.projectsLoaded) void loadProjects();
    scheduleProjectPoll();
  }

  function canPollProjects() {
    return state.authStatus === "ready"
      && state.activeTab === "projects"
      && document.visibilityState === "visible"
      && !state.mutationInFlight
      && !projectDialogOpen()
      && !state.settingsDialog.busy
      && !state.resetDialog.busy
      && !state.archiveDialog.busy
      && !state.projectInteractionActive;
  }

  function scheduleProjectPoll() {
    stopProjectPolling();
    if (
      state.authStatus !== "ready"
      || document.visibilityState !== "visible"
      || state.activeTab !== "projects"
      || !state.projectsLoaded
    ) return;
    state.projectPoll = setTimeout(pollProjects, 10000);
  }

  async function pollProjects() {
    if (canPollProjects()) {
      await loadProjects({ background: true });
    }
    scheduleProjectPoll();
  }

  function markProjectInteraction() {
    state.projectInteractionActive = true;
    clearTimeout(state.projectInteractionTimer);
    state.projectInteractionTimer = setTimeout(() => {
      state.projectInteractionActive = false;
      applyPendingProjectUpdate();
    }, 350);
  }

  function openAccountDialog() {
    elements.accountSlugInput.value = "";
    elements.accountLabelInput.value = "";
    setNotice(elements.accountCreateError, "");
    setNotice(elements.accountEnrollError, "");
    elements.accountCreateStep.classList.remove("is-hidden");
    elements.accountEnrollStep.classList.add("is-hidden");
    elements.accountDialog.classList.remove("is-hidden");
    setTimeout(() => elements.accountSlugInput.focus(), 30);
  }

  async function closeAccountDialog() {
    if (state.enrollmentId) {
      await cancelEnrollment(false);
    }
    elements.accountDialog.classList.add("is-hidden");
  }

  function validateSlug(value) {
    return /^[a-z][a-z0-9_-]{1,63}$/.test(value);
  }

  async function startEnrollment() {
    const accountSlug = elements.accountSlugInput.value.trim().toLowerCase();
    const label = elements.accountLabelInput.value.trim();
    setNotice(elements.accountCreateError, "");

    if (!validateSlug(accountSlug)) {
      setNotice(elements.accountCreateError, "Код должен начинаться с латинской буквы и содержать 2–64 символа: a-z, 0-9, _ или -.", "error");
      return;
    }

    elements.startEnrollmentButton.disabled = true;
    elements.startEnrollmentButton.textContent = "Запуск…";
    try {
      const enrollment = await apiRequest("/api/providers/codex/account-enrollments", {
        method: "POST",
        body: JSON.stringify({ account_slug: accountSlug, label: label || null }),
      });
      state.enrollmentId = enrollment.enrollment_id;
      renderEnrollment(enrollment);
      elements.accountCreateStep.classList.add("is-hidden");
      elements.accountEnrollStep.classList.remove("is-hidden");
      scheduleEnrollmentPoll();
    } catch (error) {
      setNotice(elements.accountCreateError, error.message, "error");
    } finally {
      elements.startEnrollmentButton.disabled = false;
      elements.startEnrollmentButton.textContent = "Авторизовать";
    }
  }

  function renderEnrollment(enrollment) {
    elements.enrollmentUserCode.textContent = enrollment.user_code || "Ожидание кода";
    elements.enrollmentLink.href = enrollment.verification_url || "#";
    elements.enrollmentLink.classList.toggle("is-hidden", !enrollment.verification_url);
    const statusMap = {
      starting: "Запускаем штатную авторизацию Codex…",
      waiting_for_user: "Ожидаем завершения авторизации…",
      completed: "Аккаунт успешно добавлен.",
      failed: "Авторизация завершилась ошибкой.",
      cancelled: "Авторизация отменена.",
      expired: "Время авторизации истекло.",
    };
    elements.enrollmentStatusText.textContent = statusMap[enrollment.status] || enrollment.status;
    setNotice(elements.accountEnrollError, enrollment.error_message || "", "error");
  }

  function scheduleEnrollmentPoll() {
    clearTimeout(state.enrollmentPoll);
    state.enrollmentPoll = setTimeout(pollEnrollment, 1500);
  }

  async function pollEnrollment() {
    if (!state.enrollmentId) return;
    try {
      const enrollment = await apiRequest(`/api/providers/codex/account-enrollments/${encodeURIComponent(state.enrollmentId)}`);
      renderEnrollment(enrollment);
      if (["completed", "failed", "cancelled", "expired"].includes(enrollment.status)) {
        state.enrollmentId = null;
        clearTimeout(state.enrollmentPoll);
        if (enrollment.status === "completed") {
          setTimeout(async () => {
            elements.accountDialog.classList.add("is-hidden");
            await loadAccounts();
          }, 900);
        }
        return;
      }
      scheduleEnrollmentPoll();
    } catch (error) {
      setNotice(elements.accountEnrollError, error.message, "error");
      scheduleEnrollmentPoll();
    }
  }

  async function cancelEnrollment(showError = true) {
    clearTimeout(state.enrollmentPoll);
    if (!state.enrollmentId) return;
    const enrollmentId = state.enrollmentId;
    state.enrollmentId = null;
    try {
      await apiRequest(`/api/providers/codex/account-enrollments/${encodeURIComponent(enrollmentId)}`, { method: "DELETE" });
    } catch (error) {
      if (showError) setNotice(elements.accountEnrollError, error.message, "error");
    }
  }

  async function copyEnrollmentCode() {
    const code = elements.enrollmentUserCode.textContent.trim();
    if (!code || code === "—" || code === "Ожидание кода") return;
    try {
      await navigator.clipboard.writeText(code);
      elements.copyCodeButton.textContent = "Скопировано";
      setTimeout(() => { elements.copyCodeButton.textContent = "Копировать код"; }, 1200);
    } catch {
      setNotice(elements.accountEnrollError, "Не удалось скопировать код. Выделите его вручную.", "error");
    }
  }

  function applyTelegramTheme() {
    const telegram = getTelegramWebApp();
    if (!telegram) return;
    try {
      if (typeof telegram.ready === "function") telegram.ready();
      if (typeof telegram.expand === "function") telegram.expand();
    } catch {
      return;
    }
    const theme = telegram.themeParams || {};
    const root = document.documentElement;
    if (theme.bg_color) root.style.setProperty("--bg", theme.bg_color);
    if (theme.secondary_bg_color) root.style.setProperty("--surface", theme.secondary_bg_color);
    if (theme.text_color) root.style.setProperty("--text", theme.text_color);
    if (theme.hint_color) root.style.setProperty("--text-muted", theme.hint_color);
    if (theme.button_color) root.style.setProperty("--primary", theme.button_color);
    if (theme.button_text_color) root.style.setProperty("--button-text", theme.button_text_color);
  }

  function handleTelegramActivated() {
    if (state.authStatus === "unavailable" || state.authStatus === "rejected") {
      void bootstrapTelegramAuth();
    }
  }

  function syncTelegramActivatedHandler() {
    const current = getTelegramWebApp();
    if (state.telegramEventSource === current) return;
    if (state.telegramEventSource && typeof state.telegramEventSource.offEvent === "function") {
      try {
        state.telegramEventSource.offEvent("activated", handleTelegramActivated);
      } catch {
        // The former SDK object is no longer usable; continue with the current one.
      }
    }
    state.telegramEventSource = null;
    if (current && typeof current.onEvent === "function") {
      try {
        current.onEvent("activated", handleTelegramActivated);
        state.telegramEventSource = current;
      } catch {
        state.telegramEventSource = null;
      }
    }
  }

  function telegramAuthDiagnostic(authStatus, attempt) {
    const telegram = getTelegramWebApp();
    console.info("CUS_TELEGRAM_AUTH_BOOTSTRAP", {
      sdk_object_present: Boolean(telegram),
      sdk_init_data_present: Boolean(getTelegramSdkInitData()),
      url_init_data_present: Boolean(getLaunchUrlInitData()),
      telegram_version_present: Boolean(telegram?.version),
      platform_present: Boolean(telegram?.platform),
      auth_status: authStatus,
      attempt,
    });
  }

  function waitForTelegramAuth() {
    const startedAt = Date.now();
    return new Promise((resolve) => {
      function check() {
        syncTelegramActivatedHandler();
        const initData = getCurrentTelegramInitData();
        if (initData) {
          state.authWaitTimer = null;
          resolve(initData);
          return;
        }
        if (Date.now() - startedAt >= AUTH_WAIT_TIMEOUT_MS) {
          state.authWaitTimer = null;
          resolve("");
          return;
        }
        state.authWaitTimer = setTimeout(check, AUTH_POLL_INTERVAL_MS);
      }
      check();
    });
  }

  async function loadPrivateDataForCurrentAuth() {
    const generation = state.authReadyGeneration;
    if (state.loadedAuthGeneration === generation) return;
    state.loadedAuthGeneration = generation;
    if (topicOnboardingToken) {
      await loadTopicOnboarding();
      return;
    }
    void refreshUpgradeDiscovery();
    scheduleUpgradeRefresh();
    const initialQuotaRefresh = state.quotaAutoRefreshGeneration !== generation;
    const initialRuntimeSelectionRefresh = state.runtimeSelectionAutoRefreshGeneration !== generation;
    if (initialQuotaRefresh) {
      state.quotaAutoRefreshGeneration = generation;
      state.quotaInitialRefreshInFlight = true;
      state.quotaInitialRefreshGeneration = generation;
      updateRefreshButtonContext();
    }
    if (initialRuntimeSelectionRefresh) {
      state.runtimeSelectionAutoRefreshGeneration = generation;
      state.runtimeSelectionInitialRefreshInFlight = true;
      state.runtimeSelectionInitialRefreshGeneration = generation;
      updateRefreshButtonContext();
    }
    const accountsLoaded = await loadAccounts({ initialQuotaRefresh });
    if (initialQuotaRefresh) {
      if (
        accountsLoaded
        && state.authStatus === "ready"
        && state.authReadyGeneration === generation
      ) {
        void refreshInitialAccountQuotas(generation);
      } else if (state.quotaInitialRefreshGeneration === generation) {
        state.quotaInitialRefreshInFlight = false;
        updateRefreshButtonContext();
      }
    }
    if (initialRuntimeSelectionRefresh) {
      if (state.authStatus === "ready" && state.authReadyGeneration === generation) {
        void refreshInitialRuntimeSelections(generation);
      } else if (state.runtimeSelectionInitialRefreshGeneration === generation) {
        state.runtimeSelectionInitialRefreshInFlight = false;
        updateRefreshButtonContext();
      }
    }
    if (state.authStatus !== "ready") return;
    if (state.activeTab === "projects") {
      await loadProjects();
    }
  }

  function bootstrapTelegramAuth() {
    if (state.authBootstrapPromise) return state.authBootstrapPromise;
    state.authAttempt += 1;
    const attempt = state.authAttempt;
    setAuthStatus("pending");
    applyTelegramTheme();
    syncTelegramActivatedHandler();
    state.authBootstrapPromise = (async () => {
      const initData = previewToken ? "preview" : await waitForTelegramAuth();
      if (!initData) {
        setAuthStatus("unavailable");
        telegramAuthDiagnostic("unavailable", attempt);
        return false;
      }
      state.authReadyGeneration += 1;
      setAuthStatus("ready");
      telegramAuthDiagnostic("ready", attempt);
      await loadPrivateDataForCurrentAuth();
      return state.authStatus === "ready";
    })().finally(() => {
      clearTimeout(state.authWaitTimer);
      state.authWaitTimer = null;
      state.authBootstrapPromise = null;
      renderAuthRecovery();
    });
    return state.authBootstrapPromise;
  }

  function handleVisibilityChange() {
    if (document.visibilityState !== "visible") {
      stopProjectPolling();
      return;
    }
    applyTelegramTheme();
    syncTelegramActivatedHandler();
    if (state.authStatus === "unavailable" || state.authStatus === "rejected") {
      void bootstrapTelegramAuth();
      return;
    }
    if (state.authStatus === "ready") {
      if (topicOnboardingToken) return;
      applyPendingProjectUpdate();
      scheduleProjectPoll();
    }
  }

  function cleanupPage() {
    if (state.cleanupComplete) return;
    state.cleanupComplete = true;
    stopProjectPolling();
    clearTimeout(state.authWaitTimer);
    clearTimeout(state.projectInteractionTimer);
    clearTimeout(state.enrollmentPoll);
    document.removeEventListener("visibilitychange", handleVisibilityChange);
    if (state.telegramEventSource && typeof state.telegramEventSource.offEvent === "function") {
      try {
        state.telegramEventSource.offEvent("activated", handleTelegramActivated);
      } catch {
        // Page teardown must remain best-effort.
      }
    }
    state.telegramEventSource = null;
    clearInterval(state.upgradeRefreshTimer);
    state.upgradeRefreshTimer = null;
  }

  elements.tabs.forEach((tab) => tab.addEventListener("click", () => switchTab(tab.dataset.tab)));
  elements.upgradeActionButton.addEventListener("click", runUpgradeAction);
  elements.refreshButton.addEventListener("click", refreshCurrentTab);
  elements.addAccountButton.addEventListener("click", openAccountDialog);
  elements.startEnrollmentButton.addEventListener("click", startEnrollment);
  elements.cancelEnrollmentButton.addEventListener("click", () => cancelEnrollment(true));
  elements.copyCodeButton.addEventListener("click", copyEnrollmentCode);
  elements.closeDialogButtons.forEach((button) => button.addEventListener("click", closeAccountDialog));
  elements.projectsList.addEventListener("click", (event) => {
    const archiveToggle = event.target.closest("[data-toggle-archive]");
    if (archiveToggle) {
      toggleArchiveDisclosure(archiveToggle);
      return;
    }
    const button = event.target.closest("[data-configure-project]");
    if (button) openProjectSettingsDialog(button.dataset.configureProject);
    const focusAccount = event.target.closest("[data-focus-account]");
    if (focusAccount?.dataset.focusAccount) {
      switchTab("accounts");
      setTimeout(() => {
        document.querySelector(`[data-account-card="${CSS.escape(focusAccount.dataset.focusAccount)}"]`)?.scrollIntoView({ block: "center" });
      }, 50);
    }
    const archive = event.target.closest("[data-archive-project]");
    if (archive) openArchiveDialog(archive.dataset.archiveProject, true);
    const unarchive = event.target.closest("[data-unarchive-project]");
    if (unarchive) openArchiveDialog(unarchive.dataset.unarchiveProject, false);
  });
  elements.accountsList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-refresh-quota]");
    if (button) refreshQuota(button.dataset.refreshQuota, button);
    const reset = event.target.closest("[data-reset-credit-action]");
    if (reset) openResetCreditDialog(reset);
  });
  elements.confirmResetCreditButton.addEventListener("click", confirmResetCredit);
  elements.cancelResetCreditButton.addEventListener("click", closeResetCreditDialog);
  elements.confirmArchiveButton.addEventListener("click", confirmArchiveDialog);
  elements.cancelArchiveButton.addEventListener("click", closeArchiveDialog);
  elements.settingsAccountSelect.addEventListener("change", handleAccountSelectionChange);
  elements.settingsModelSelect.addEventListener("change", handleModelSelectionChange);
  elements.settingsReasoningSelect.addEventListener("change", handleReasoningSelectionChange);
  elements.refreshCapabilitiesButton.addEventListener("click", refreshSelectedCapabilities);
  elements.openApplyConfirmationButton.addEventListener("click", openApplyConfirmation);
  elements.confirmApplySettingsButton.addEventListener("click", confirmApplySettings);
  elements.bindProjectPathButton.addEventListener("click", bindProjectPath);
  elements.backToSettingsEditButton.addEventListener("click", showSettingsEditStep);
  elements.retryAuthButton.addEventListener("click", () => {
    if (state.authStatus === "pending") return;
    void bootstrapTelegramAuth();
  });
  elements.onboardingProjectPath.addEventListener("input", updateTopicOnboardingAvailability);
  elements.onboardingAccount.addEventListener("change", () => {
    state.topicOnboarding.selections.account = elements.onboardingAccount.value;
    state.topicOnboarding.selections.model = "";
    state.topicOnboarding.selections.reasoning = "";
    void loadTopicOnboardingCapabilities();
  });
  elements.onboardingModel.addEventListener("change", () => {
    state.topicOnboarding.selections.model = elements.onboardingModel.value;
    state.topicOnboarding.selections.reasoning = "";
    renderTopicOnboardingReasoning();
  });
  elements.onboardingReasoning.addEventListener("change", () => {
    state.topicOnboarding.selections.reasoning = elements.onboardingReasoning.value;
    updateTopicOnboardingAvailability();
  });
  elements.onboardingRefreshCapabilities.addEventListener("click", () => {
    void loadTopicOnboardingCapabilities({ force: true });
  });
  elements.onboardingApply.addEventListener("click", applyTopicOnboarding);
  elements.closeSettingsDialogButtons.forEach((button) => button.addEventListener("click", closeProjectSettingsDialog));
  elements.accountDialog.addEventListener("click", (event) => {
    if (event.target === elements.accountDialog) closeAccountDialog();
  });
  elements.projectSettingsDialog.addEventListener("click", (event) => {
    if (event.target === elements.projectSettingsDialog) closeProjectSettingsDialog();
  });
  elements.archiveDialog.addEventListener("click", (event) => {
    if (event.target === elements.archiveDialog) closeArchiveDialog();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !elements.accountDialog.classList.contains("is-hidden")) closeAccountDialog();
    if (event.key === "Escape" && !elements.projectSettingsDialog.classList.contains("is-hidden")) handleSettingsBack();
    if (event.key === "Escape" && !elements.resetCreditDialog.classList.contains("is-hidden")) closeResetCreditDialog();
    if (event.key === "Escape" && !elements.archiveDialog.classList.contains("is-hidden")) closeArchiveDialog();
  });
  document.addEventListener("pointerdown", markProjectInteraction, { passive: true });
  document.addEventListener("touchstart", markProjectInteraction, { passive: true });
  document.addEventListener("scroll", markProjectInteraction, { passive: true });
  document.addEventListener("visibilitychange", handleVisibilityChange);
  window.addEventListener("pagehide", cleanupPage);
  window.addEventListener("beforeunload", cleanupPage);

  updateRefreshButtonContext();
  void bootstrapTelegramAuth();
})();
