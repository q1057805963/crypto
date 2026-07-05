    const mainEl = document.getElementById("dashboard-main");
    const detailDrawerBackdropEl = document.getElementById("detail-drawer-backdrop");
    const detailDrawerEl = document.getElementById("detail-drawer");
    const detailCloseBtn = document.getElementById("detail-close-btn");
    const symbolsEl = document.getElementById("symbols");
    const eventsEl = document.getElementById("events");
    const eventsCollapseBtn = document.getElementById("events-collapse-btn");
    const sideScrollEl = document.getElementById("side-scroll");
    const updatedEl = document.getElementById("updated");
    const countEl = document.getElementById("count");
    const alertCountEl = document.getElementById("alert-count");
    const symbolInputEl = document.getElementById("symbol-input");
    const saveSymbolsEl = document.getElementById("save-symbols");
    const btnTheme = document.getElementById("btn-theme");
    const userScopeEl = document.getElementById("user-scope");
    const detailEl = document.getElementById("detail");
    const sourceLabelEl = document.getElementById("source-label");
    const sourceHealthEl = document.getElementById("source-health");
    const telegramModal = document.getElementById("telegram-modal");
    const aiModal = document.getElementById("ai-modal");
    const signalsModal = document.getElementById("signals-modal");
    const thresholdModal = document.getElementById("threshold-modal");
    const profileModal = document.getElementById("profile-modal");
    const thresholdSymbolEl = document.getElementById("threshold-symbol");
    const thresholdInputEl = document.getElementById("threshold-input");
    const thresholdHintEl = document.getElementById("threshold-hint");
    const thresholdTriggerMode = document.getElementById("threshold-trigger-mode");
    const profileNameEl = document.getElementById("profile-name");
    const profileRoleEl = document.getElementById("profile-role");
    const profileUserIdEl = document.getElementById("profile-user-id");
    const profileAuthEl = document.getElementById("profile-auth");
    const profileSymbolCountEl = document.getElementById("profile-symbol-count");
    const profileThemeEl = document.getElementById("profile-theme");
    const profileSymbolsEl = document.getElementById("profile-symbols");
    const profileUsersSectionEl = document.getElementById("profile-users-section");
    const profileUsersMetaEl = document.getElementById("profile-users-meta");
    const profileUsersEl = document.getElementById("profile-users");
    const authScreen = document.getElementById("auth-screen");
    const authTitle = document.getElementById("auth-title");
    const authHint = document.getElementById("auth-hint");
    const authUsername = document.getElementById("auth-username");
    const authPassword = document.getElementById("auth-password");
    const authError = document.getElementById("auth-error");
    const authSubmit = document.getElementById("auth-submit");
    const authSwitch = document.getElementById("auth-switch");

    let selectedSymbol = null;
    let inputTouched = false;
    let symbolThresholds = {};
    let globalThreshold = 60;
    let thresholdEditingSymbol = null;
    let aiResults = {};
    let aiRequestedAt = {};
    let aiMeta = {};
    let authStatus = { enabled: false, has_users: false, allow_registration: true };
    let authMode = "login";
    let currentUser = null;
    let authToken = localStorage.getItem("cfm_auth_token") || "";
    let refreshTimer = null;
    let lastSymbols = [];
    let lastEvents = [];
    let drawerScrollBySymbol = {};
    let aiScrollBySymbol = {};
    let timeframeAnalysisCache = {};
    let timeframeRequestedAt = {};
    let timeframeMeta = {};
    let timeframeConfluenceCache = {};
    let timeframeConfluenceRequestedAt = {};
    let timeframeConfluenceMeta = {};
    let detailInteractionUntil = 0;
    let detailInteractionTimer = null;
    let pendingDetailRefresh = false;
    let pendingAIRefreshSymbol = null;
    let symbolOrderDrag = null;
    const DETAIL_INTERACTION_LOCK_MS = 900;
    const AI_ANALYSIS_CACHE_VERSION = "scenario-v3";
    const TIMEFRAME_OPTIONS = ["5m", "15m", "1h", "4h", "1d"];
    const DETAIL_PERIOD_OPTIONS = [...TIMEFRAME_OPTIONS];
    const thresholdTriggerFields = {
      score: [document.getElementById("threshold-trigger-score"), document.getElementById("threshold-trigger-score-value")],
      quote_volume_1m: [document.getElementById("threshold-trigger-volume"), document.getElementById("threshold-trigger-volume-value")],
      volume_multiplier: [document.getElementById("threshold-trigger-multiplier"), document.getElementById("threshold-trigger-multiplier-value")],
      price_move_pct_1m_abs: [document.getElementById("threshold-trigger-price"), document.getElementById("threshold-trigger-price-value")],
      oi_change_pct_5m_abs: [document.getElementById("threshold-trigger-oi"), document.getElementById("threshold-trigger-oi-value")],
      liquidation_total_quote_1m: [document.getElementById("threshold-trigger-liquidation"), document.getElementById("threshold-trigger-liquidation-value")],
      depth_imbalance_abs: [document.getElementById("threshold-trigger-imbalance"), document.getElementById("threshold-trigger-imbalance-value")],
      depth_drop_pct_1m: [document.getElementById("threshold-trigger-depth-drop"), document.getElementById("threshold-trigger-depth-drop-value")],
      spread_bps: [document.getElementById("threshold-trigger-spread"), document.getElementById("threshold-trigger-spread-value")]
    };

    const directionText = {
      up: "向上异动",
      down: "向下异动",
      mixed: "混合异常",
      waiting: "等待数据"
    };

    function createUserId() {
      if (window.crypto && window.crypto.randomUUID) {
        return `u_${window.crypto.randomUUID().replace(/-/g, "")}`;
      }
      return `u_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 14)}`;
    }

    function getUserId() {
      const key = "cfm_user_id";
      let value = localStorage.getItem(key);
      if (!value) {
        value = createUserId();
        localStorage.setItem(key, value);
      }
      return value;
    }

    const userId = getUserId();
    userScopeEl.textContent = `个人配置 ${userId.slice(-6)}`;

    function storageKey(name) {
      const scope = currentUser && currentUser.user_id ? currentUser.user_id : userId;
      return `${name}_${scope}`;
    }

    function requestHeaders(extra = {}) {
      const headers = Object.assign({ "X-CFM-User": userId }, extra);
      if (authToken) headers.Authorization = `Bearer ${authToken}`;
      return headers;
    }

    async function apiFetch(url, options = {}) {
      const response = await fetch(url, Object.assign({}, options, {
        headers: requestHeaders(options.headers || {})
      }));
      if (response.status === 401 && authStatus.enabled) {
        localStorage.removeItem("cfm_auth_token");
        authToken = "";
        currentUser = null;
        stopRefreshTimer();
        resetViewState();
        showAuth("login");
      }
      return response;
    }

    function stopRefreshTimer() {
      if (refreshTimer) {
        clearInterval(refreshTimer);
        refreshTimer = null;
      }
    }

    function isDrawerOpen() {
      return detailDrawerEl.classList.contains("open");
    }

    function setDrawerOpen(open) {
      mainEl.classList.toggle("drawer-open", open);
      detailDrawerEl.classList.toggle("open", open);
    }

    function openDetailDrawer(symbol = null) {
      if (symbol) selectedSymbol = symbol;
      if (!selectedSymbol) return;
      setDrawerOpen(true);
    }

    function closeDetailDrawer(clearSelection = true) {
      setDrawerOpen(false);
      if (clearSelection) {
        clearDeferredDetailState();
        selectedSymbol = null;
        renderSymbols(lastSymbols);
        renderDetail(lastSymbols);
      }
    }

    function resetViewState() {
      if (symbolOrderDrag) {
        symbolOrderDrag.row.classList.remove("dragging");
        symbolOrderDrag = null;
        cleanupSymbolDrag();
      }
      selectedSymbol = null;
      inputTouched = false;
      symbolThresholds = {};
      globalThreshold = 60;
      thresholdEditingSymbol = null;
      lastSymbols = [];
      aiResults = {};
      aiRequestedAt = {};
      aiMeta = {};
      timeframeAnalysisCache = {};
      timeframeRequestedAt = {};
      timeframeMeta = {};
      timeframeConfluenceCache = {};
      timeframeConfluenceRequestedAt = {};
      timeframeConfluenceMeta = {};
      drawerScrollBySymbol = {};
      aiScrollBySymbol = {};
      clearDeferredDetailState();
      symbolInputEl.value = "";
      symbolsEl.innerHTML = "";
      eventsEl.innerHTML = `<div class="empty">暂无报警</div>`;
      delete detailEl.dataset.symbol;
      detailEl.innerHTML = `
        <div class="drawer-empty">
          <div class="drawer-empty-title">点击左侧合约展开详情</div>
          <div class="drawer-empty-copy">右侧会先给你价格、量能、OI 图，再切到 AI、结构位和流动性视图。</div>
        </div>
      `;
      countEl.textContent = "0 个合约";
      alertCountEl.textContent = "0";
      setDrawerOpen(false);
    }

    function applyTheme(theme) {
      const light = theme === "light";
      document.body.classList.toggle("light", light);
      btnTheme.textContent = light ? "夜间" : "白天";
      localStorage.setItem(storageKey("cfm_theme"), light ? "light" : "dark");
    }

    applyTheme(localStorage.getItem(storageKey("cfm_theme")) || "dark");
    btnTheme.addEventListener("click", () => {
      applyTheme(document.body.classList.contains("light") ? "dark" : "light");
    });

    function updateAuthUser(user) {
      currentUser = user;
      if (user && user.username) {
        userScopeEl.textContent = `${user.username} · ${user.role || "user"}`;
        userScopeEl.title = "打开个人配置";
      } else {
        userScopeEl.textContent = `个人配置 ${userId.slice(-6)}`;
        userScopeEl.title = "打开个人配置";
      }
    }

    function renderProfileModal() {
      const theme = document.body.classList.contains("light") ? "白天" : "夜间";
      profileNameEl.textContent = currentUser && currentUser.username ? currentUser.username : "本地用户";
      profileRoleEl.textContent = currentUser && currentUser.role ? currentUser.role : (authStatus.enabled ? "未登录" : "local");
      profileUserIdEl.textContent = currentUser && currentUser.user_id ? currentUser.user_id : userId;
      profileAuthEl.textContent = authStatus.enabled ? "JWT 已启用" : "本地模式";
      profileSymbolCountEl.textContent = `${lastSymbols.length} 个合约`;
      profileThemeEl.textContent = theme;
      profileSymbolsEl.innerHTML = lastSymbols.length
        ? lastSymbols.map((symbol) => `<span class="chip">${esc(symbol.symbol || symbol)}</span>`).join("")
        : `<span class="muted">暂无监控对象</span>`;
      const isAdmin = currentUser && currentUser.role === "admin";
      profileUsersSectionEl.classList.toggle("visible", Boolean(isAdmin));
      if (isAdmin) {
        profileUsersMetaEl.textContent = "正在加载系统用户...";
        profileUsersEl.innerHTML = "";
      } else {
        profileUsersMetaEl.textContent = "仅管理员可见";
        profileUsersEl.innerHTML = "";
      }
    }

    function profileTimeText(ts) {
      const value = Number(ts || 0);
      if (!value) return "--";
      const ms = value > 1e12 ? value : value * 1000;
      return new Date(ms).toLocaleString();
    }

    function renderSystemUsers(users) {
      const list = Array.isArray(users) ? users : [];
      profileUsersMetaEl.textContent = `${list.length} 个系统用户`;
      if (!list.length) {
        profileUsersEl.innerHTML = `<div class="profile-users-meta">暂无用户</div>`;
        return;
      }
      profileUsersEl.innerHTML = list.map((user) => {
        const role = String(user.role || "user");
        const symbols = Array.isArray(user.symbols) ? user.symbols : [];
        const telegramText = user.telegram_enabled
          ? `推送开 · ${Number(user.telegram_active_chat_count || 0)} 个 Chat`
          : "推送关";
        const aiText = user.ai_enabled
          ? `AI开${user.ai_key_set ? " · Key已配" : " · Key未配"}`
          : "AI关";
        const thresholdText = Number(user.symbol_threshold_count || 0) > 0
          ? `单币规则 ${Number(user.symbol_threshold_count || 0)} 个`
          : "默认规则";
        return `
          <div class="profile-user-card">
            <div class="profile-user-head">
              <div class="profile-user-name">${esc(user.username || "--")}</div>
              <div class="profile-user-role ${role === "admin" ? "admin" : ""}">${esc(role)}</div>
            </div>
            <div class="profile-user-meta">ID ${esc(user.user_id || "--")}</div>
            <div class="profile-user-meta">创建 ${esc(profileTimeText(user.created_at))} · 监控 ${Number(user.symbol_count || symbols.length || 0)} 个</div>
            <div class="profile-user-flags">${esc(telegramText)} · ${esc(aiText)} · ${esc(thresholdText)}</div>
            <div class="profile-user-flags">${symbols.length ? esc(symbols.join(", ")) : "暂无监控合约"}</div>
          </div>
        `;
      }).join("");
    }

    async function loadSystemUsers() {
      if (!currentUser || currentUser.role !== "admin") return;
      try {
        const response = await apiFetch("/api/users", { cache: "no-store" });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "用户列表加载失败");
        renderSystemUsers(data.users || []);
      } catch (error) {
        profileUsersMetaEl.textContent = error.message || "用户列表加载失败";
        profileUsersEl.innerHTML = "";
      }
    }

    function openProfileModal() {
      renderProfileModal();
      openModal(profileModal);
      loadSystemUsers();
    }

    function showAuth(mode = "login") {
      if (!authStatus.enabled) return;
      authMode = mode;
      const registerMode = authMode === "register";
      authTitle.textContent = registerMode ? "创建管理员账号" : "登录";
      authHint.textContent = registerMode
        ? "首次部署请创建管理员账号。后续用户注册默认关闭，可在配置中开启。"
        : "登录后加载你的监控列表、AI、Telegram 和阈值配置。";
      authSubmit.textContent = registerMode ? "创建并登录" : "登录";
      authSwitch.textContent = registerMode ? "返回登录" : "创建账号";
      authSwitch.style.display = authStatus.allow_registration ? "block" : "none";
      authError.textContent = "";
      authPassword.value = "";
      authScreen.classList.add("open");
      setTimeout(() => authUsername.focus(), 0);
    }

    function hideAuth() {
      authScreen.classList.remove("open");
      authError.textContent = "";
    }

    async function loadAuthStatus() {
      const response = await fetch("/api/auth/status", { cache: "no-store" });
      authStatus = await response.json();
      if (!authStatus.has_users && authStatus.enabled) {
        authStatus.allow_registration = true;
      }
    }

    async function verifyStoredToken() {
      if (!authToken) return false;
      const response = await apiFetch("/api/auth/me", { cache: "no-store" });
      if (!response.ok) return false;
      const data = await response.json();
      if (!data.ok || !data.user) return false;
      updateAuthUser(data.user);
      return true;
    }

    async function submitAuth() {
      const username = authUsername.value.trim();
      const password = authPassword.value;
      authSubmit.disabled = true;
      authSubmit.textContent = authMode === "register" ? "创建中" : "登录中";
      authError.textContent = "";
      try {
        const endpoint = authMode === "register" ? "/api/auth/register" : "/api/auth/login";
        const response = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username, password })
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "认证失败");
        authToken = data.token;
        localStorage.setItem("cfm_auth_token", authToken);
        updateAuthUser(data.user);
        hideAuth();
        startApp();
      } catch (error) {
        authError.textContent = error.message || "认证失败";
      } finally {
        authSubmit.disabled = false;
        authSubmit.textContent = authMode === "register" ? "创建并登录" : "登录";
      }
    }

    function logout() {
      localStorage.removeItem("cfm_auth_token");
      authToken = "";
      updateAuthUser(null);
      stopRefreshTimer();
      resetViewState();
      showAuth("login");
    }

    function startApp() {
      stopRefreshTimer();
      resetViewState();
      authScreen.classList.remove("open");
      applyTheme(localStorage.getItem(storageKey("cfm_theme")) || "dark");
      loadStoredAIResults();
      loadSymbolThresholds().then(refresh);
      refreshTimer = setInterval(refresh, 1000);
    }

    async function bootstrap() {
      try {
        await loadAuthStatus();
        if (!authStatus.enabled) {
          startApp();
          return;
        }
        const ok = await verifyStoredToken();
        if (ok) {
          startApp();
          return;
        }
        showAuth(authStatus.has_users ? "login" : "register");
      } catch (error) {
        updatedEl.textContent = "认证服务不可用";
        showAuth("login");
      }
    }

    authSubmit.addEventListener("click", submitAuth);
    authSwitch.addEventListener("click", () => {
      showAuth(authMode === "register" ? "login" : "register");
    });
    [authUsername, authPassword].forEach((input) => {
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") submitAuth();
      });
    });
    userScopeEl.addEventListener("click", openProfileModal);

    function loadStoredAIResults() {
      try {
        const raw = JSON.parse(localStorage.getItem(storageKey("cfm_ai_results")) || "{}");
        const now = Date.now();
        Object.entries(raw).forEach(([key, value]) => {
          if (value && value.text && now - Number(value.ts || 0) < 30 * 60 * 1000) {
            if (value.version !== AI_ANALYSIS_CACHE_VERSION) return;
            aiResults[key] = value.text;
          }
        });
      } catch (error) {}
    }

    function saveAIResult(symbol, text, period = detailPeriod()) {
      const key = aiAnalysisKey(symbol, period);
      aiResults[key] = text;
      try {
        const raw = JSON.parse(localStorage.getItem(storageKey("cfm_ai_results")) || "{}");
        raw[key] = { text, ts: Date.now(), version: AI_ANALYSIS_CACHE_VERSION };
        localStorage.setItem(storageKey("cfm_ai_results"), JSON.stringify(raw));
      } catch (error) {}
    }

    function clearStoredAIResults() {
      aiResults = {};
      aiRequestedAt = {};
      aiMeta = {};
      try {
        localStorage.removeItem(storageKey("cfm_ai_results"));
      } catch (error) {}
    }

    function esc(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[char]));
    }

    function fmtNumber(value, digits = 2) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
      return Number(value).toLocaleString(undefined, {
        maximumFractionDigits: digits,
        minimumFractionDigits: 0
      });
    }

    function formatAge(seconds) {
      const value = Number(seconds);
      if (!Number.isFinite(value)) return "暂无";
      if (value < 1) return "<1s";
      if (value < 60) return `${Math.round(value)}s`;
      const minutes = value / 60;
      if (minutes < 60) return `${Math.round(minutes)}m`;
      return `${Math.round(minutes / 60)}h`;
    }

    function renderSourceHealth(health) {
      if (!sourceHealthEl) return;
      const channels = Array.isArray(health.channels) ? health.channels : [];
      const total = Number(health.total_count || channels.length || 0);
      const active = Number(health.active_count || 0);
      const status = health.status || (total ? "degraded" : "unavailable");
      sourceHealthEl.className = `source-health ${status}`;
      sourceHealthEl.textContent = total ? `数据 ${active}/${total}` : "数据 --";
      if (!channels.length) {
        sourceHealthEl.title = "暂无数据源健康明细";
        return;
      }
      sourceHealthEl.title = channels.map((channel) => {
        const label = channel.label || channel.key || "数据";
        if (channel.status === "active") return `${label} ${formatAge(channel.age_seconds)} 前`;
        if (channel.status === "stale") return `${label} ${formatAge(channel.age_seconds)} 未更新`;
        return `${label} 暂无`;
      }).join("\n");
    }

    function fmtSignedNumber(value, digits = 2) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
      const number = Number(value);
      return `${number > 0 ? "+" : ""}${fmtNumber(number, digits)}`;
    }

    function mutedValue(text = "--") {
      return `<span class="muted">${esc(text)}</span>`;
    }

    function hasCoreTradeData(symbol) {
      return Boolean(
        symbol &&
        Number(symbol.price || 0) > 0 &&
        Number(symbol.trade_count_1m || 0) > 0 &&
        Number(symbol.updated_at || 0) > 0
      );
    }

    function hasDepthData(symbol) {
      if (!symbol) return false;
      if ((symbol.microstructure_status || "unavailable") !== "active") return false;
      const bidDepth = Number(symbol.bid_depth_notional || 0);
      const askDepth = Number(symbol.ask_depth_notional || 0);
      const spread = Number(symbol.spread_bps || 0);
      return bidDepth > 0 || askDepth > 0 || spread > 0;
    }

    function hasStructureData(symbol) {
      return hasCoreTradeData(symbol) && Number(symbol.support_price || 0) > 0 && Number(symbol.resistance_price || 0) > 0;
    }

    function hasOiData(symbol) {
      if (!symbol) return false;
      return hasCoreTradeData(symbol) && (
        Number(symbol.open_interest || 0) > 0 ||
        Math.abs(Number(symbol.oi_change_pct_5m || 0)) > 0
      );
    }

    function hasFundingData(symbol) {
      if (!symbol) return false;
      return hasCoreTradeData(symbol) && (
        Math.abs(Number(symbol.funding_rate || 0)) > 0 ||
        Number(symbol.open_interest || 0) > 0
      );
    }

    function dataState(symbol) {
      if (hasCoreTradeData(symbol)) return "live";
      if (hasDepthData(symbol)) return "partial";
      return "empty";
    }

    function dataBannerHtml(symbol) {
      const state = dataState(symbol);
      if (state === "live") return "";
      if (state === "partial") {
        return `<div class="data-banner partial">当前已接入盘口深度，但成交主数据暂未到位。价格、波动、量能和结构判断会在实时成交恢复后补齐。</div>`;
      }
      return `<div class="data-banner empty">当前未收到可用成交数据。请优先使用 WebSocket 行情源，避免 REST 受限时页面被默认值填满。</div>`;
    }

    function fmtPctMaybe(value, available = true, digits = 3) {
      if (!available) return mutedValue();
      const number = Number(value || 0);
      const cls = number > 0 ? "up" : number < 0 ? "down" : "muted";
      return `<span class="${cls}">${number >= 0 ? "+" : ""}${number.toFixed(digits)}%</span>`;
    }

    function fmtFundingMaybe(value, available = true) {
      if (!available) return mutedValue();
      const number = Number(value || 0) * 100;
      const cls = number > 0 ? "up" : number < 0 ? "down" : "muted";
      return `<span class="${cls}">${number >= 0 ? "+" : ""}${number.toFixed(4)}%</span>`;
    }

    function fmtBpsMaybe(value, available = true) {
      if (!available) return mutedValue();
      const number = Number(value || 0);
      const cls = number >= 4 ? "down" : number >= 2 ? "mixed" : "muted";
      return `<span class="${cls}">${number.toFixed(2)}</span>`;
    }

    function fmtNumberMaybe(value, digits = 2, available = true, suffix = "") {
      if (!available) return mutedValue();
      return `${fmtNumber(value, digits)}${suffix}`;
    }

    function fmtPlainPctMaybe(value, available = true, digits = 2) {
      if (!available) return "--";
      return fmtPlainPct(value, digits);
    }

    function fmtPriceMaybe(value, available = true) {
      if (!available) return "--";
      return fmtNumber(value, 8);
    }

    function clamp(value, min, max) {
      return Math.min(max, Math.max(min, value));
    }

    function seriesValues(values) {
      if (!Array.isArray(values)) return [];
      return values
        .map((value) => Number(value))
        .filter((value) => Number.isFinite(value));
    }

    function chartEmptyHtml(text = "暂无可绘制数据") {
      return `<div class="chart-empty">${esc(text)}</div>`;
    }

    function lineChartSvg(values, tone = "mixed") {
      const series = seriesValues(values);
      if (series.length < 2) return chartEmptyHtml("时序样本不足");
      const width = 100;
      const height = 44;
      const top = 4;
      const bottom = 40;
      const min = Math.min(...series);
      const max = Math.max(...series);
      const range = Math.max(max - min, 1e-9);
      const step = width / Math.max(series.length - 1, 1);
      const points = series.map((value, index) => {
        const x = index * step;
        const y = bottom - ((value - min) / range) * (bottom - top);
        return `${x.toFixed(2)},${y.toFixed(2)}`;
      });
      const area = [`0,${bottom}`, ...points, `${width},${bottom}`].join(" ");
      const lastPoint = points[points.length - 1].split(",");
      return `
        <div class="spark-wrap ${tone}">
          <svg class="spark-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
            <line class="spark-grid-line" x1="0" y1="12" x2="${width}" y2="12"></line>
            <line class="spark-grid-line" x1="0" y1="26" x2="${width}" y2="26"></line>
            <line class="spark-grid-line" x1="0" y1="40" x2="${width}" y2="40"></line>
            <polygon class="spark-area" points="${area}"></polygon>
            <polyline class="spark-line" points="${points.join(" ")}"></polyline>
            <circle class="spark-dot" cx="${lastPoint[0]}" cy="${lastPoint[1]}" r="2.8"></circle>
          </svg>
        </div>
      `;
    }

    function barChartSvg(values, tone = "blue") {
      const series = seriesValues(values);
      if (!series.length || series.every((value) => value <= 0)) return chartEmptyHtml("暂无量能柱");
      const width = 100;
      const height = 44;
      const max = Math.max(...series, 1);
      const gap = 1.2;
      const barWidth = Math.max((width - gap * (series.length - 1)) / Math.max(series.length, 1), 1.8);
      return `
        <div class="spark-wrap short ${tone}">
          <svg class="spark-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
            <line class="spark-grid-line" x1="0" y1="40" x2="${width}" y2="40"></line>
            ${series.map((value, index) => {
              const heightValue = Math.max((value / max) * 34, 2);
              const x = index * (barWidth + gap);
              const y = 40 - heightValue;
              return `<rect class="spark-bar" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${heightValue.toFixed(2)}" rx="1.8"></rect>`;
            }).join("")}
          </svg>
        </div>
      `;
    }

    function meterHtml(label, percent, display, tone = "blue", note = "") {
      const width = clamp(Number(percent) || 0, 0, 100);
      return `
        <div class="mini-meter">
          <div class="meter-head">
            <span>${esc(label)}</span>
            <strong>${display}</strong>
          </div>
          <div class="meter-track">
            <span class="meter-fill ${esc(tone)}" style="width:${width.toFixed(1)}%"></span>
          </div>
          ${note ? `<div class="meter-copy">${esc(note)}</div>` : ""}
        </div>
      `;
    }

    function aiAnalysisKey(symbol, period = detailPeriod()) {
      return `${String(symbol || "").toUpperCase()}::${period}::${AI_ANALYSIS_CACHE_VERSION}`;
    }

    function detailTab() {
      const raw = localStorage.getItem(storageKey("cfm_detail_tab")) || "overview";
      if (raw === "realtime") return "overview";
      return ["overview", "ai", "structure", "liquidity"].includes(raw) ? raw : "overview";
    }

    function setDetailTab(tab) {
      localStorage.setItem(storageKey("cfm_detail_tab"), tab);
    }

    function detailPeriod() {
      const raw = localStorage.getItem(storageKey("cfm_detail_period")) || "5m";
      return DETAIL_PERIOD_OPTIONS.includes(raw) ? raw : "5m";
    }

    function setDetailPeriod(period) {
      localStorage.setItem(storageKey("cfm_detail_period"), period);
    }

    function detailPeriodLabel(period = detailPeriod()) {
      return period === "realtime" ? "实时" : period;
    }

    function clearDeferredDetailState() {
      detailInteractionUntil = 0;
      pendingDetailRefresh = false;
      pendingAIRefreshSymbol = null;
      if (detailInteractionTimer) {
        clearTimeout(detailInteractionTimer);
        detailInteractionTimer = null;
      }
    }

    function selectedSymbolSnapshot(symbols = lastSymbols) {
      return (symbols || []).find((item) => item.symbol === selectedSymbol) || null;
    }

    function detailRefreshLocked(symbol = selectedSymbol) {
      return Boolean(symbol && detailTab() === "ai" && Date.now() < detailInteractionUntil);
    }

    function scheduleDetailRefreshFlush() {
      if (detailInteractionTimer) clearTimeout(detailInteractionTimer);
      const delay = Math.max(DETAIL_INTERACTION_LOCK_MS, detailInteractionUntil - Date.now()) + 40;
      detailInteractionTimer = setTimeout(() => {
        detailInteractionTimer = null;
        flushDeferredDetailRefresh();
      }, delay);
    }

    function noteAIInteraction() {
      detailInteractionUntil = Date.now() + DETAIL_INTERACTION_LOCK_MS;
      scheduleDetailRefreshFlush();
    }

    function shouldDeferDetailRender(symbols) {
      if (!detailRefreshLocked()) return false;
      if ((detailEl.dataset.symbol || "") !== selectedSymbol) return false;
      return Boolean(selectedSymbolSnapshot(symbols));
    }

    function flushDeferredDetailRefresh() {
      if (detailRefreshLocked()) {
        scheduleDetailRefreshFlush();
        return;
      }
      if (!selectedSymbol) {
        pendingDetailRefresh = false;
        pendingAIRefreshSymbol = null;
        return;
      }
      if (pendingDetailRefresh) {
        pendingDetailRefresh = false;
        pendingAIRefreshSymbol = null;
        renderDetail(lastSymbols);
        return;
      }
      if (pendingAIRefreshSymbol && pendingAIRefreshSymbol === selectedSymbol && detailTab() === "ai") {
        const symbol = pendingAIRefreshSymbol;
        pendingAIRefreshSymbol = null;
        refreshAIBlock(symbol, false);
      }
    }

    function captureDetailScroll(symbol = selectedSymbol) {
      if (!symbol) return { drawer: 0, ai: 0, sameSymbol: false, period: detailPeriod() };
      const sameSymbol = (detailEl.dataset.symbol || "") === symbol;
      const aiBlock = document.getElementById("ai-block");
      const drawerTop = sideScrollEl ? sideScrollEl.scrollTop : (drawerScrollBySymbol[symbol] || 0);
      const aiStorageKey = aiAnalysisKey(symbol);
      const aiTop = aiBlock ? aiBlock.scrollTop : (aiScrollBySymbol[aiStorageKey] || 0);
      drawerScrollBySymbol[symbol] = drawerTop;
      aiScrollBySymbol[aiStorageKey] = aiTop;
      return { drawer: drawerTop, ai: aiTop, sameSymbol, period: detailPeriod() };
    }

    function applyScrollRestore(element, top) {
      if (!element) return;
      const nextTop = Math.max(0, Number(top) || 0);
      if (Math.abs(element.scrollTop - nextTop) < 1) return;
      element.scrollTop = nextTop;
      requestAnimationFrame(() => {
        if (element && element.isConnected && Math.abs(element.scrollTop - nextTop) >= 1) {
          element.scrollTop = nextTop;
        }
      });
    }

    function restoreDetailScroll(symbol, preserved, activeTab) {
      requestAnimationFrame(() => {
        if (sideScrollEl) {
          const drawerTop = preserved && preserved.sameSymbol
            ? preserved.drawer
            : (drawerScrollBySymbol[symbol] || 0);
          applyScrollRestore(sideScrollEl, drawerTop);
        }
        const aiBlock = document.getElementById("ai-block");
        if (aiBlock && activeTab === "ai") {
          const aiStorageKey = aiAnalysisKey(symbol);
          const aiTop = preserved && preserved.sameSymbol && preserved.period === detailPeriod()
            ? preserved.ai
            : (aiScrollBySymbol[aiStorageKey] || 0);
          applyScrollRestore(aiBlock, aiTop);
        }
      });
    }

    function refreshAIBlock(symbol, deferIfLocked = true) {
      const aiBlock = document.getElementById("ai-block");
      if (!aiBlock) return;
      const scrollTop = aiBlock.scrollTop;
      const key = aiAnalysisKey(symbol);
      aiScrollBySymbol[key] = scrollTop;
      if (deferIfLocked && selectedSymbol === symbol && detailRefreshLocked(symbol)) {
        pendingAIRefreshSymbol = symbol;
        scheduleDetailRefreshFlush();
        return;
      }
      pendingAIRefreshSymbol = null;
      aiBlock.innerHTML = renderAIBlock(symbol);
      const nextBlock = document.getElementById("ai-block");
      applyScrollRestore(nextBlock, aiScrollBySymbol[key] || 0);
      syncAICopyButton(symbol);
    }

    function structureMarkerHtml(marker) {
      const classes = ["range-rail-marker", marker.tone || "muted", `lane-${marker.lane || 0}`];
      if (marker.compact) classes.push("compact");
      const shift = Number(marker.shift || 0);
      if (shift < 0) classes.push("has-shift", "shift-left");
      if (shift > 0) classes.push("has-shift", "shift-right");
      return `
        <div class="${classes.join(" ")}" style="left:${marker.left.toFixed(2)}%; --label-offset:${shift.toFixed(0)}px; --leader-width:${Math.abs(shift).toFixed(0)}px">
          <strong>${esc(marker.label)}</strong>
          <span class="marker-leader"></span>
          <span class="marker-stem"></span>
        </div>
      `;
    }

    function structureMarkerPriority(label) {
      if (label === "支撑") return 0;
      if (label === "VWAP") return 1;
      if (label === "现价") return 2;
      if (label === "压力") return 3;
      return 4;
    }

    function sortStructureMarkers(left, right) {
      const diff = Number(left.left || 0) - Number(right.left || 0);
      if (Math.abs(diff) >= 0.01) return diff;
      return structureMarkerPriority(left.label) - structureMarkerPriority(right.label);
    }

    function applyStructureCluster(cluster) {
      if (!cluster.length) return;
      const averageLeft = cluster.reduce((sum, marker) => sum + Number(marker.left || 0), 0) / cluster.length;
      const compact = cluster.length >= 3;
      const step = compact ? 54 : 42;
      cluster.forEach((marker) => {
        marker.compact = compact;
        marker.lane = 0;
        marker.shift = 0;
      });

      if (averageLeft >= 82) {
        const ordered = [...cluster].sort((left, right) => {
          const diff = Number(right.left || 0) - Number(left.left || 0);
          if (Math.abs(diff) >= 0.01) return diff;
          return structureMarkerPriority(right.label) - structureMarkerPriority(left.label);
        });
        ordered.forEach((marker, index) => {
          marker.shift = -(24 + index * step);
        });
        return;
      }

      if (averageLeft <= 18) {
        const ordered = [...cluster].sort(sortStructureMarkers);
        ordered.forEach((marker, index) => {
          marker.shift = 24 + index * step;
        });
        return;
      }

      const ordered = [...cluster].sort(sortStructureMarkers);
      const centerIndex = (ordered.length - 1) / 2;
      ordered.forEach((marker, index) => {
        marker.shift = Math.round((index - centerIndex) * step);
      });
    }

    function structureMarkers(symbol, rangePos, vwapPos) {
      const markers = [
        { label: "支撑", left: 0, tone: "up", lane: 0 },
        { label: "VWAP", left: vwapPos, tone: "mixed", lane: 0 },
        { label: "现价", left: rangePos, tone: valueClass(symbol.price_move_pct !== undefined ? symbol.price_move_pct : symbol.price_move_pct_1m), lane: 0 },
        { label: "压力", left: 100, tone: "down", lane: 0 }
      ];
      const closenessThreshold = 9;
      markers.forEach((marker) => {
        if (marker.left <= 6) marker.shift = 24;
        if (marker.left >= 94) marker.shift = -24;
      });
      const sorted = [...markers].sort(sortStructureMarkers);
      const clusters = [];
      sorted.forEach((marker) => {
        const current = clusters[clusters.length - 1];
        if (!current || Math.abs(marker.left - current[current.length - 1].left) >= closenessThreshold) {
          clusters.push([marker]);
          return;
        }
        current.push(marker);
      });
      clusters.filter((cluster) => cluster.length > 1).forEach(applyStructureCluster);
      return markers.map((marker) => structureMarkerHtml(marker)).join("");
    }

    function fmtPct(value) {
      const number = Number(value || 0);
      const cls = number > 0 ? "up" : number < 0 ? "down" : "muted";
      return `<span class="${cls}">${number >= 0 ? "+" : ""}${number.toFixed(3)}%</span>`;
    }

    function fmtFunding(value) {
      const number = Number(value || 0) * 100;
      const cls = number > 0 ? "up" : number < 0 ? "down" : "muted";
      return `<span class="${cls}">${number >= 0 ? "+" : ""}${number.toFixed(4)}%</span>`;
    }

    function fmtBps(value) {
      const number = Number(value || 0);
      const cls = number >= 4 ? "down" : number >= 2 ? "mixed" : "muted";
      return `<span class="${cls}">${number.toFixed(2)}</span>`;
    }

    function fmtPriceLevel(value) {
      const number = Number(value || 0);
      if (!number) return "--";
      return fmtNumber(number, 8);
    }

    function fmtPlainPct(value, digits = 2) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
      return `${Number(value).toFixed(digits)}%`;
    }

    function liquidationStatusText(symbol) {
      const status = symbol.liquidation_data_status || "unavailable";
      if (status === "recent_event") return "有强平";
      if (status === "no_recent_event") return "近1m无";
      return "未接入";
    }

    function liquidationStatusClass(symbol) {
      const status = symbol.liquidation_data_status || "unavailable";
      if (status === "recent_event") return "down";
      if (status === "no_recent_event") return "muted";
      return "mixed";
    }

    function liquidationTotalHtml(symbol) {
      if ((symbol.liquidation_data_status || "unavailable") === "unavailable") {
        return `<span class="mixed">未接入</span>`;
      }
      if ((symbol.liquidation_data_status || "") === "no_recent_event") {
        return `<span class="muted">近1m无</span>`;
      }
      return `<span class="down">${fmtNumber(symbol.liquidation_total_quote_1m, 0)}</span>`;
    }

    function liquidationSideHtml(symbol, key) {
      if ((symbol.liquidation_data_status || "unavailable") === "unavailable") {
        return `<span class="mixed">未接入</span>`;
      }
      return fmtNumber(symbol[key], 0);
    }

    function periodLiquidationHtml(data) {
      if (!data || (data.period_liquidation_data_status || "unavailable") === "unavailable") {
        return `<span class="mixed">未接入</span>`;
      }
      if ((data.period_liquidation_data_status || "") === "no_recent_event") {
        return `<span class="muted">本周期无</span>`;
      }
      const longQuote = Number(data.period_long_liquidation_quote || 0);
      const shortQuote = Number(data.period_short_liquidation_quote || 0);
      if (longQuote > 0 && shortQuote > 0) {
        return `<span class="mixed">多 ${fmtNumber(longQuote, 0)} / 空 ${fmtNumber(shortQuote, 0)}</span>`;
      }
      if (longQuote > 0) return `<span class="down">多 ${fmtNumber(longQuote, 0)}</span>`;
      if (shortQuote > 0) return `<span class="up">空 ${fmtNumber(shortQuote, 0)}</span>`;
      return `<span class="muted">本周期无</span>`;
    }

    function microstructureStatusHtml(symbol) {
      if ((symbol.microstructure_status || "unavailable") === "active") {
        return `<span class="up">流活跃</span>`;
      }
      return `<span class="mixed">未接入</span>`;
    }

    function riskClass(level) {
      if (level && level.includes("高")) return "risk-high";
      if (level && level.includes("中")) return "risk-mid";
      return "risk-low";
    }

    function shortBias(bias) {
      const text = bias || "观察";
      if (text.includes("偏多")) return "偏多";
      if (text.includes("偏空")) return "偏空";
      if (text.includes("拥挤")) return "拥挤";
      if (text.includes("波动")) return "波动";
      return "观察";
    }

    function biasClass(bias) {
      const text = shortBias(bias);
      if (text === "偏多") return "bias-up";
      if (text === "偏空") return "bias-down";
      if (text === "拥挤" || text === "波动") return "bias-crowded";
      return "bias-watch";
    }

    function signalTag(symbol) {
      if (Number(symbol.score || 0) >= 60) return "报警";
      if (Number(symbol.score || 0) >= 45) return "关注";
      if (Math.abs(Number(symbol.price_move_pct_1m || 0)) >= 0.6) return "急动";
      if (Number(symbol.volume_multiplier || 0) >= 2.2) return "放量";
      if (Math.abs(Number(symbol.oi_change_pct_5m || 0)) >= 0.8) return "OI";
      if ((symbol.liquidation_data_status || "") === "recent_event" && Number(symbol.liquidation_total_quote_1m || 0) >= 75000) return "爆仓";
      if (Number(symbol.spread_bps || 0) >= 3 || Number(symbol.depth_drop_pct_1m || 0) >= 15) return "盘口";
      if (Math.abs(Number(symbol.funding_rate || 0)) >= 0.0003) return "费率";
      if (Number(symbol.score || 0) > 0) return "监测";
      return "静默";
    }

    function rowClass(symbol) {
      if (symbol.direction === "up") return "up";
      if (symbol.direction === "down") return "down";
      if (symbol.direction === "mixed") return "mixed";
      return "muted";
    }

    function valueClass(value) {
      const number = Number(value || 0);
      if (number > 0) return "up";
      if (number < 0) return "down";
      return "muted";
    }

    function currentThresholdText(symbol) {
      const config = symbolThresholds[symbol] || {};
      const hasRules = hasEnabledThresholdRules(config.push_rules);
      if (hasRules && config.anomaly_score !== undefined && config.anomaly_score !== null) {
        return `规则 + ${fmtNumber(config.anomaly_score, 1)} 分`;
      }
      if (hasRules) {
        return "组合规则";
      }
      if (config && config.anomaly_score !== undefined && config.anomaly_score !== null) {
        return `${fmtNumber(config.anomaly_score, 1)} 分`;
      }
      return `全局 ${fmtNumber(globalThreshold, 1)} 分`;
    }

    function thresholdButtonText(symbol) {
      const config = symbolThresholds[symbol] || {};
      const hasRules = hasEnabledThresholdRules(config.push_rules);
      const hasScore = config.anomaly_score !== undefined && config.anomaly_score !== null;
      if (hasRules && hasScore) return "自定义";
      if (hasRules) return "组合";
      if (hasScore) return "分数";
      return "规则";
    }

    function structureState(symbol) {
      const regime = String(symbol.structure_regime || "");
      const regimeLabels = {
        support_lost: "支撑失守",
        resistance_breakout: "压力突破",
        support_test: "测试支撑",
        resistance_test: "测试压力",
        value_area_rotation: "价值区轮动",
        upper_acceptance: "上方接受",
        lower_acceptance: "下方接受",
        balanced: "区间均衡"
      };
      if (regimeLabels[regime]) return regimeLabels[regime];
      const supportDistance = Number(symbol.support_distance_pct || 0);
      const resistanceDistance = Number(symbol.resistance_distance_pct || 0);
      const rangePosition = Number(symbol.range_position_pct || 50);
      const vwapDeviation = Number(symbol.vwap_deviation_pct || 0);
      if (supportDistance <= 0.12 && resistanceDistance <= 0.12) return "区间极窄";
      if (supportDistance <= 0.18 && supportDistance <= resistanceDistance) return "贴近支撑";
      if (resistanceDistance <= 0.18 && resistanceDistance < supportDistance) return "贴近压力";
      if (rangePosition >= 80) return "区间上沿";
      if (rangePosition <= 20) return "区间下沿";
      if (vwapDeviation >= 0.35) return "高于 VWAP";
      if (vwapDeviation <= -0.35) return "低于 VWAP";
      return "区间中部";
    }

    function structureMeta(symbol) {
      const supportDistance = fmtPlainPct(symbol.support_distance_pct, 2);
      const resistanceDistance = fmtPlainPct(symbol.resistance_distance_pct, 2);
      return `距撑 ${supportDistance} / 距压 ${resistanceDistance}`;
    }

    function levelSourceText(source) {
      const labels = {
        swing_volume_cluster: "摆动点+成交密集",
        volume_profile_cluster: "成交密集区",
        swing_cluster: "摆动点聚类",
        touch_cluster: "触碰聚类",
        range_low: "阶段低点",
        range_high: "阶段高点",
        low: "K线低点",
        high: "K线高点"
      };
      return labels[source] || "结构聚类";
    }

    function levelStatusText(status) {
      const labels = {
        valid: "有效",
        testing: "测试中",
        bounce_watch: "反抽确认",
        rejection_watch: "遇阻确认",
        lost_soft: "轻微跌破",
        lost_confirmed: "放量失守",
        breakout_soft: "轻微突破",
        breakout_confirmed: "放量突破",
        unknown: "待确认"
      };
      return labels[status] || "待确认";
    }

    function levelEvidenceText(data, side) {
      const source = levelSourceText(data[`${side}_source`]);
      const touches = Number(data[`${side}_touch_count`] || 0);
      const pivots = Number(data[`${side}_pivot_count`] || 0);
      const strength = Number(data[`${side}_strength`] || 0);
      const score = Number(data[`${side}_confluence_score`] || 0);
      const status = levelStatusText(data[`${side}_status`]);
      const sampleCount = Number(data.structure_sample_count || 0);
      const parts = [source];
      if (touches > 0) parts.push(`${touches} 次触碰`);
      if (pivots > 0) parts.push(`${pivots} 个摆动点`);
      if (score > 0) parts.push(`评分 ${fmtNumber(score, 0)}`);
      else if (strength > 0) parts.push(`强度 ${fmtNumber(strength, 1)}`);
      if (data[`${side}_status`]) parts.push(status);
      if (sampleCount > 0) parts.push(`${sampleCount} 根K线`);
      return parts.join(" · ");
    }

    function structureNarrative(symbol) {
      const state = structureState(symbol);
      const support = fmtPriceLevel(symbol.support_price);
      const resistance = fmtPriceLevel(symbol.resistance_price);
      const supportDistance = fmtPlainPct(symbol.support_distance_pct, 2);
      const resistanceDistance = fmtPlainPct(symbol.resistance_distance_pct, 2);
      const bidWall = fmtPriceLevel(symbol.bid_wall_price);
      const askWall = fmtPriceLevel(symbol.ask_wall_price);
      if (state === "区间极窄") {
        return `价格挤在 ${support} - ${resistance} 的窄区间里，短线更容易先走假突破，先等放量确认。`;
      }
      if (state === "贴近支撑") {
        return `当前更靠近支撑 ${support}，距支撑约 ${supportDistance}；若买盘墙 ${bidWall} 继续承接，短线更利于反抽观察。`;
      }
      if (state === "贴近压力") {
        return `当前更靠近压力 ${resistance}，距压力约 ${resistanceDistance}；若卖盘墙 ${askWall} 持续压制，短线更容易先遇阻。`;
      }
      if (state === "区间上沿") {
        return `价格运行在区间上半段，离压力更近，重点看是否放量站上 ${resistance}。`;
      }
      if (state === "区间下沿") {
        return `价格运行在区间下半段，离支撑更近，重点看 ${support} 是否继续被动承接。`;
      }
      if (state === "高于 VWAP") {
        return `当前价格在区间 VWAP 上方，说明短线均价偏强，但若无法继续抬高压力位，容易回归均价。`;
      }
      if (state === "低于 VWAP") {
        return `当前价格在区间 VWAP 下方，说明短线均价偏弱，除非快速收回 VWAP，否则更偏震荡偏弱。`;
      }
      return `当前处在区间中部，支撑 ${support} 与压力 ${resistance} 都还有效，优先观察哪一侧先被放量突破。`;
    }

    function flowNarrative(symbol) {
      const buyRatio = Number(symbol.taker_buy_ratio_1m || 0) * 100;
      const depthDrop = Number(symbol.depth_drop_pct_1m || 0);
      const imbalance = Number(symbol.depth_imbalance || 0) * 100;
      const spread = Number(symbol.spread_bps || 0);
      if (depthDrop >= 15 && spread >= 3) {
        return `盘口明显变薄，点差 ${spread.toFixed(2)} bps，当前更要防插针和瞬时滑点。`;
      }
      if (buyRatio >= 65 && imbalance >= 10) {
        return `主动买入和买盘深度都偏强，若价格还能站稳 VWAP，上冲延续性会更好。`;
      }
      if (buyRatio <= 35 && imbalance <= -10) {
        return `主动卖出与卖盘深度都偏强，除非快速收回均价，否则下压更占优。`;
      }
      return `当前流向没有形成极端单边，先把盘口墙、VWAP 和区间边界一起看。`;
    }

    function structureStateClass(state) {
      if (state.includes("失守") || state.includes("压力") || state.includes("下方")) return "down";
      if (state.includes("突破") || state.includes("支撑") || state.includes("上方")) return "up";
      if (state.includes("价值区") || state.includes("均衡") || state.includes("上沿") || state.includes("下沿") || state.includes("窄")) return "mixed";
      return "muted";
    }

    function hasEnabledThresholdRules(rules) {
      const conditions = rules && rules.conditions ? rules.conditions : {};
      return Object.values(conditions).some((cfg) => Boolean(cfg && cfg.enabled));
    }

    const aiTriggerLabels = {
      score: ["异常分", "", 1],
      quote_volume_1m: ["1分钟成交额", " USDT", 0],
      volume_multiplier: ["量能倍数", "x", 2],
      price_move_pct_1m_abs: ["1分钟波动", "%", 3],
      oi_change_pct_5m_abs: ["OI 5分钟", "%", 3],
      liquidation_total_quote_1m: ["1分钟爆仓额", " USDT", 0],
      depth_imbalance_abs: ["盘口失衡", "%", 1],
      depth_drop_pct_1m: ["深度下降", "%", 1],
      spread_bps: ["盘口点差", " bps", 2]
    };

    function aiReasonText(reason) {
      return {
        "ai trigger not met": "触发条件未满足",
        "retry cooldown": "失败冷却中",
        "missing api key": "缺少 API Key",
        "ai disabled": "AI 未开启",
        "analysis timeout": "分析超时",
        "analysis failed": "分析失败",
        "no data": "暂无数据"
      }[reason] || reason || "暂无分析";
    }

    function triggerCheckText(check) {
      const spec = aiTriggerLabels[check.key] || [check.key || "条件", "", 2];
      const digits = spec[2];
      const value = Number(check.value || 0);
      const threshold = Number(check.threshold || 0);
      const format = (number) => Number(number).toLocaleString(undefined, {
        maximumFractionDigits: digits,
        minimumFractionDigits: 0
      });
      return `${spec[0]} ${format(value)}${spec[1]} / ${format(threshold)}${spec[1]}`;
    }

    function aiTriggerStatusText(trigger) {
      if (!trigger || !Array.isArray(trigger.checks) || !trigger.checks.length) return "";
      const matchedChecks = trigger.checks.filter((check) => check && check.matched);
      const visibleChecks = (matchedChecks.length ? matchedChecks : trigger.checks).slice(0, 2);
      const modeText = trigger.mode === "all" ? "全部条件" : "任一条件";
      const resultText = trigger.matched ? "已满足" : "未满足";
      return `${modeText}${resultText}: ${visibleChecks.map(triggerCheckText).join("; ")}`;
    }

    function applyThresholdRuleForm(rules, fallbackScore = globalThreshold) {
      const conditions = rules && rules.conditions ? rules.conditions : {};
      thresholdTriggerMode.value = rules && rules.mode === "all" ? "all" : "any";
      Object.entries(thresholdTriggerFields).forEach(([key, fields]) => {
        const cfg = conditions[key] || {};
        fields[0].checked = Boolean(cfg.enabled);
        const defaultValue = key === "score" ? fallbackScore : fields[1].value;
        fields[1].value = cfg.threshold ?? defaultValue;
      });
    }

    function collectThresholdRules() {
      const conditions = {};
      Object.entries(thresholdTriggerFields).forEach(([key, fields]) => {
        conditions[key] = {
          enabled: fields[0].checked,
          threshold: Number(fields[1].value) || 0
        };
      });
      return { mode: thresholdTriggerMode.value || "any", conditions };
    }

    function aiStatusLine(symbol) {
      const meta = aiMeta[aiAnalysisKey(symbol)];
      if (!meta) return "";
      const timeText = meta.ts ? new Date(meta.ts).toLocaleTimeString() : "";
      return `<div class="ai-status"><strong>${esc(meta.status)}</strong><span>${esc(timeText)}</span></div>`;
    }

    function aiSections(text) {
      const lines = String(text || "").split("\n").map((line) => line.trim()).filter(Boolean);
      if (!lines.length) return [];
      const sections = [];
      let current = null;

      function flushCurrent() {
        if (!current) return;
        const body = current.bodyLines.join(" ").trim();
        sections.push({ title: current.title, body: body || "等待补充内容" });
        current = null;
      }

      lines.forEach((line) => {
        const markdownHeading = line.match(/^(?:\d+[\.、]\s*)?\*\*(.+?)\*\*[:：]?\s*(.*)$/);
        if (markdownHeading) {
          flushCurrent();
          current = { title: markdownHeading[1], bodyLines: [] };
          if (markdownHeading[2]) current.bodyLines.push(markdownHeading[2]);
          return;
        }

        const titledLine = line.match(/^(?:\d+[\.、]\s*)?([^:：]{2,20})[:：]\s*(.+)$/);
        if (titledLine) {
          flushCurrent();
          sections.push({ title: titledLine[1], body: titledLine[2] });
          return;
        }

        const numberedLine = line.match(/^(\d+)[\.、]\s*(.+)$/);
        if (numberedLine) {
          flushCurrent();
          sections.push({ title: `要点 ${numberedLine[1]}`, body: numberedLine[2] });
          return;
        }

        if (current) {
          current.bodyLines.push(line);
          return;
        }

        sections.push({ title: sections.length ? `补充 ${sections.length + 1}` : "AI 摘要", body: line });
      });

      flushCurrent();
      return sections;
    }

    function aiOpinionText(symbol, period = detailPeriod()) {
      const text = String(aiResults[aiAnalysisKey(symbol, period)] || "").trim();
      if (!text) return "";
      const sections = aiSections(text);
      if (!sections.length) return text;
      const header = `${String(symbol || "").toUpperCase()} ${detailPeriodLabel(period)} AI 观点`;
      return [
        header,
        "",
        ...sections.map((item, index) => `${index + 1}. ${item.title}\n${item.body}`)
      ].join("\n\n");
    }

    function hasAIAnalysis(symbol, period = detailPeriod()) {
      return Boolean(String(aiResults[aiAnalysisKey(symbol, period)] || "").trim());
    }

    function syncAICopyButton(symbol = selectedSymbol) {
      const button = document.getElementById("ai-copy-btn");
      if (!button || button.dataset.copyFeedback === "1") return;
      button.disabled = !hasAIAnalysis(symbol);
    }

    function writeClipboardText(text) {
      if (navigator.clipboard && window.isSecureContext) {
        return navigator.clipboard.writeText(text);
      }
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand("copy");
        return Promise.resolve();
      } finally {
        document.body.removeChild(area);
      }
    }

    async function copyAIOpinions(symbol) {
      const text = aiOpinionText(symbol);
      const button = document.getElementById("ai-copy-btn");
      if (!text) {
        updatedEl.textContent = "暂无可复制的 AI 观点";
        return;
      }
      try {
        await writeClipboardText(text);
        updatedEl.textContent = "AI 观点已复制";
        if (button) {
          const original = button.textContent;
          button.textContent = "已复制";
          button.disabled = true;
          button.dataset.copyFeedback = "1";
          setTimeout(() => {
            if (button.isConnected) {
              button.textContent = original || "复制观点";
              delete button.dataset.copyFeedback;
              syncAICopyButton(symbol);
            }
          }, 1200);
        }
      } catch (error) {
        updatedEl.textContent = "AI 观点复制失败";
      }
    }

    function renderAIBlock(symbol) {
      const period = detailPeriod();
      const periodLabel = detailPeriodLabel(period);
      const key = aiAnalysisKey(symbol, period);
      const text = aiResults[key];
      if (!text) {
        return `${aiStatusLine(symbol)}<div class="detail-placeholder">等待 AI 基于当前 ${esc(periodLabel)} 档位生成观察建议。</div>`;
      }
      const sections = aiSections(text);
      const cardsHtml = sections.length
        ? `<div class="ai-grid">${sections.map((item) => `
            <div class="ai-card">
              <div class="ai-card-title">${esc(item.title)}</div>
              <div class="ai-card-copy">${esc(item.body)}</div>
            </div>
          `).join("")}</div>`
        : `<div class="detail-placeholder">AI 返回了空内容，请稍后重试。</div>`;
      return aiStatusLine(symbol) + cardsHtml;
    }

    function detailTabButton(tab, label) {
      return `<button class="detail-tab ${detailTab() === tab ? "active" : ""}" data-detail-tab="${esc(tab)}" type="button">${esc(label)}</button>`;
    }

    function insightCard(kicker, value, copy, tone = "muted") {
      return `
        <div class="insight-card">
          <div class="insight-kicker">${esc(kicker)}</div>
          <div class="insight-value ${esc(tone)}">${esc(value)}</div>
          <div class="insight-copy">${esc(copy)}</div>
        </div>
      `;
    }

    function chartCard(title, meta, body, extraClass = "") {
      return `
        <div class="chart-card ${extraClass}">
          <div class="chart-head">
            <div class="chart-title">${esc(title)}</div>
            <div class="chart-meta">${meta}</div>
          </div>
          ${body}
        </div>
      `;
    }

    function activeTimeframeData(symbol) {
      if (!symbol || !symbol.symbol || detailPeriod() === "realtime") return null;
      return cachedTimeframeAnalysis(symbol.symbol, detailPeriod());
    }

    function activeConfluenceData(symbol) {
      if (!symbol || !symbol.symbol) return null;
      return cachedTimeframeConfluence(symbol.symbol);
    }

    function confluenceTone(data) {
      if (!data) return "muted";
      if (data.direction === "up") return "up";
      if (data.direction === "down") return "down";
      return "mixed";
    }

    function confluenceLabel(data) {
      if (!data) return "等待共振";
      return `${data.label || "多周期共振"} ${fmtNumber(data.score, 1)}`;
    }

    function confluenceCopy(data) {
      if (!data) return "正在汇总 5m、15m、1h、4h 的结构与动能。";
      const conflicts = Array.isArray(data.conflicts) && data.conflicts.length
        ? `分歧：${data.conflicts.slice(0, 1).join("；")}`
        : "暂无明显周期冲突";
      return `${data.summary || ""} ${conflicts}`.trim();
    }

    function timeframeAmplitudePct(data) {
      if (!data) return null;
      const low = Number(data.low_price || 0);
      const high = Number(data.high_price || 0);
      if (low <= 0 || high <= 0 || high < low) return null;
      return (high / low - 1) * 100;
    }

    function timeframeImpulseLabel(data) {
      if (!data) return "等待周期分析";
      const move = Number(data.price_move_pct || 0);
      const volume = Number(data.volume_multiplier || 0);
      if (move >= 1 && volume >= 1.5) return "放量上攻";
      if (move <= -1 && volume >= 1.5) return "放量下压";
      if (volume >= 2) return move >= 0 ? "放量试高" : "放量试低";
      if (Math.abs(move) <= 0.3 && volume <= 0.9) return "缩量整理";
      return move >= 0 ? "偏强震荡" : "偏弱震荡";
    }

    function timeframeImpulseCopy(data) {
      const amplitude = timeframeAmplitudePct(data);
      const amplitudeText = amplitude === null ? "--" : fmtPlainPct(amplitude, 2);
      return `${data.period_label} 涨跌 ${fmtPlainPct(data.price_move_pct, 2)}，量能 ${fmtNumber(data.volume_multiplier, 2)}x，振幅 ${amplitudeText}。`;
    }

    function timeframeMarkLabel(data) {
      if (!data || data.mark_premium_bps === null || data.mark_premium_bps === undefined) return "标记价待补齐";
      const premium = Number(data.mark_premium_bps || 0);
      if (premium >= 4) return "明显升水";
      if (premium <= -4) return "明显贴水";
      return "贴近现价";
    }

    function timeframeMarkCopy(data) {
      if (!data || data.mark_premium_bps === null || data.mark_premium_bps === undefined) {
        return "当前还没有足够的标记价序列。";
      }
      const markMove = data.mark_move_pct === null || data.mark_move_pct === undefined
        ? "--"
        : fmtPlainPct(data.mark_move_pct, 2);
      return `${data.period_label} 标记价偏离 ${fmtSignedNumber(data.mark_premium_bps, 1)} bps，区间涨跌 ${markMove}。`;
    }

    function timeframeFocusItems(data) {
      if (!data) return [];
      const items = [
        `${data.period_label}涨跌 ${fmtPlainPct(data.price_move_pct, 2)}`,
        `量能 ${fmtNumber(data.volume_multiplier, 2)}x`,
        `结构 ${structureState(data)}`
      ];
      const amplitude = timeframeAmplitudePct(data);
      if (amplitude !== null) items.push(`振幅 ${fmtPlainPct(amplitude, 2)}`);
      if (data.mark_premium_bps !== null && data.mark_premium_bps !== undefined) {
        items.push(`标记偏离 ${fmtSignedNumber(data.mark_premium_bps, 1)} bps`);
      }
      return items.slice(0, 4);
    }

    function timeframeCandleStatus(data) {
      return data && data.candle_confirmed ? "已收线" : "进行中";
    }

    function renderOverviewPanel(symbol) {
      const periodData = activeTimeframeData(symbol);
      const confluence = activeConfluenceData(symbol);
      if (periodData) {
        const period = periodData.period_label || detailPeriod();
        const focusItems = timeframeFocusItems(periodData);
        return `
          <div class="detail-panel">
            <div class="insight-grid">
              ${insightCard(`${period} 动能`, timeframeImpulseLabel(periodData), timeframeImpulseCopy(periodData), valueClass(periodData.price_move_pct))}
              ${insightCard("结构", structureState(periodData), timeframeNarrative(periodData), structureStateClass(structureState(periodData)))}
              ${insightCard("标记价", timeframeMarkLabel(periodData), timeframeMarkCopy(periodData), valueClass(periodData.mark_premium_bps))}
              ${insightCard("多周期", confluenceLabel(confluence), confluenceCopy(confluence), confluenceTone(confluence))}
            </div>
            <div>
              <div class="detail-title">${esc(period)} 关注点</div>
              <div class="reason-pills">
                ${focusItems.map((item) => `<span class="reason-pill">${esc(item)}</span>`).join("")}
              </div>
            </div>
          </div>
        `;
      }
      const tradeLive = hasCoreTradeData(symbol);
      const depthLive = hasDepthData(symbol);
      const structureLive = hasStructureData(symbol);
      const reasons = (symbol.reasons || []).length ? symbol.reasons : ["暂无明确触发项"];
      const structureText = structureLive ? structureNarrative(symbol) : "等待成交数据后补齐结构判断。";
      const flowText = depthLive ? flowNarrative(symbol) : "盘口深度暂未到位，先以价格与量能为主。";
      const liquidationText = (symbol.liquidation_data_status || "unavailable") === "recent_event"
        ? `近 1 分钟爆仓 ${fmtNumber(symbol.liquidation_total_quote_1m, 0)} USDT`
        : (symbol.liquidation_data_status || "unavailable") === "no_recent_event"
          ? "近 1 分钟暂无可识别爆仓"
          : "当前数据源未给到有效爆仓流";
      return `
        <div class="detail-panel">
          <div class="insight-grid">
            ${insightCard("结构", structureLive ? structureState(symbol) : "等待结构", structureText, structureLive ? structureStateClass(structureState(symbol)) : "muted")}
            ${insightCard("盘口", shortBias(symbol.bias || "观察"), flowText, rowClass(symbol))}
            ${insightCard("爆仓", liquidationStatusText(symbol), liquidationText, liquidationStatusClass(symbol))}
            ${insightCard("多周期", confluenceLabel(confluence), confluenceCopy(confluence), confluenceTone(confluence))}
          </div>
          <div>
            <div class="detail-title">本次关注点</div>
            <div class="reason-pills">
              ${reasons.map((item) => `<span class="reason-pill">${esc(item)}</span>`).join("")}
            </div>
          </div>
        </div>
      `;
    }

    function renderStructurePanel(symbol) {
      const periodData = activeTimeframeData(symbol);
      if (periodData) {
        const available = Number(periodData.support_price || 0) > 0 && Number(periodData.resistance_price || 0) > 0;
        if (!available) {
          return `<div class="detail-panel"><div class="detail-placeholder">等待 ${esc(detailPeriod())} 周期结构数据稳定后，再展示支撑、压力、VWAP 与区间位置。</div></div>`;
        }
        const period = periodData.period_label || detailPeriod();
        const support = Number(periodData.support_price || 0);
        const resistance = Number(periodData.resistance_price || 0);
        const price = Number(periodData.price || 0);
        const vwap = Number(periodData.window_vwap || 0);
        const spread = Math.max(resistance - support, 1e-9);
        const rangePos = clamp(Number(periodData.range_position_pct || 50), 0, 100);
        const vwapPos = clamp(((vwap - support) / spread) * 100, 0, 100);
        return `
          <div class="detail-panel">
            <div class="range-rail-card">
              <div class="range-rail-head">
                <div class="range-rail-title">${esc(period)} 支撑 / 压力结构带</div>
                <div class="range-rail-meta">${esc(structureState(periodData))}</div>
              </div>
              <div class="range-rail">
                <div class="range-rail-track"></div>
                ${structureMarkers(periodData, rangePos, vwapPos)}
              </div>
              <div class="range-rail-footer">
                <div class="rail-stat">
                  <div class="rail-stat-label">结构支撑</div>
                  <div class="rail-stat-value">${fmtPriceLevel(support)}</div>
                </div>
                <div class="rail-stat">
                  <div class="rail-stat-label">${periodData.candle_confirmed ? esc(period) + " 收盘" : esc(period) + " 最新"}</div>
                  <div class="rail-stat-value">${fmtPriceLevel(price)}</div>
                </div>
                <div class="rail-stat">
                  <div class="rail-stat-label">VWAP</div>
                  <div class="rail-stat-value">${fmtPriceLevel(vwap)}</div>
                </div>
                <div class="rail-stat">
                  <div class="rail-stat-label">结构压力</div>
                  <div class="rail-stat-value">${fmtPriceLevel(resistance)}</div>
                </div>
              </div>
            </div>
            <div class="metric-grid compact">
              <div class="metric">
                <div class="metric-label">距支撑</div>
                <div class="metric-value up">${fmtPlainPct(periodData.support_distance_pct, 2)}</div>
                <div class="metric-copy">${esc(levelEvidenceText(periodData, "support"))}</div>
              </div>
              <div class="metric">
                <div class="metric-label">距压力</div>
                <div class="metric-value down">${fmtPlainPct(periodData.resistance_distance_pct, 2)}</div>
                <div class="metric-copy">${esc(levelEvidenceText(periodData, "resistance"))}</div>
              </div>
              <div class="metric">
                <div class="metric-label">区间位置</div>
                <div class="metric-value">${fmtPlainPct(periodData.range_position_pct, 2)}</div>
                <div class="metric-copy">${esc(structureState(periodData))}</div>
              </div>
              <div class="metric">
                <div class="metric-label">VWAP 偏离</div>
                <div class="metric-value ${valueClass(periodData.vwap_deviation_pct)}">${fmtPlainPct(periodData.vwap_deviation_pct, 3)}</div>
                <div class="metric-copy">${esc(period)} 均价偏离度</div>
              </div>
              <div class="metric">
                <div class="metric-label">成交密集 POC</div>
                <div class="metric-value">${fmtPriceLevel(periodData.profile_poc_price)}</div>
                <div class="metric-copy">成交额 ${fmtNumber(periodData.profile_poc_quote_volume, 0)} USDT</div>
              </div>
              <div class="metric">
                <div class="metric-label">价值区间</div>
                <div class="metric-value">${fmtPriceLevel(periodData.value_area_low)} / ${fmtPriceLevel(periodData.value_area_high)}</div>
                <div class="metric-copy">约 70% 成交额集中的接受区</div>
              </div>
            </div>
            <div class="period-note">${esc(timeframeNarrative(periodData))}</div>
          </div>
        `;
      }
      const available = hasStructureData(symbol);
      if (!available) {
        return `<div class="detail-panel"><div class="detail-placeholder">等待成交序列稳定后，再展示支撑、压力、VWAP 与区间位置。</div></div>`;
      }
      const support = Number(symbol.support_price || 0);
      const resistance = Number(symbol.resistance_price || 0);
      const price = Number(symbol.price || 0);
      const vwap = Number(symbol.window_vwap || 0);
      const spread = Math.max(resistance - support, 1e-9);
      const rangePos = clamp(Number(symbol.range_position_pct || 50), 0, 100);
      const vwapPos = clamp(((vwap - support) / spread) * 100, 0, 100);
      return `
        <div class="detail-panel">
          <div class="range-rail-card">
            <div class="range-rail-head">
              <div class="range-rail-title">支撑 / 压力结构带</div>
              <div class="range-rail-meta">${esc(structureState(symbol))}</div>
            </div>
            <div class="range-rail">
              <div class="range-rail-track"></div>
              ${structureMarkers(symbol, rangePos, vwapPos)}
            </div>
            <div class="range-rail-footer">
              <div class="rail-stat">
                <div class="rail-stat-label">支撑位</div>
                <div class="rail-stat-value">${fmtPriceLevel(support)}</div>
              </div>
              <div class="rail-stat">
                <div class="rail-stat-label">现价</div>
                <div class="rail-stat-value">${fmtPriceLevel(price)}</div>
              </div>
              <div class="rail-stat">
                <div class="rail-stat-label">VWAP</div>
                <div class="rail-stat-value">${fmtPriceLevel(vwap)}</div>
              </div>
              <div class="rail-stat">
                <div class="rail-stat-label">压力位</div>
                <div class="rail-stat-value">${fmtPriceLevel(resistance)}</div>
              </div>
            </div>
          </div>
          <div class="metric-grid compact">
            <div class="metric">
              <div class="metric-label">距支撑</div>
              <div class="metric-value up">${fmtPlainPct(symbol.support_distance_pct, 2)}</div>
              <div class="metric-copy">越短说明越接近下沿承接区</div>
            </div>
            <div class="metric">
              <div class="metric-label">距压力</div>
              <div class="metric-value down">${fmtPlainPct(symbol.resistance_distance_pct, 2)}</div>
              <div class="metric-copy">越短说明越接近上沿抛压区</div>
            </div>
            <div class="metric">
              <div class="metric-label">区间位置</div>
              <div class="metric-value">${fmtPlainPct(symbol.range_position_pct, 2)}</div>
              <div class="metric-copy">${esc(structureState(symbol))}</div>
            </div>
            <div class="metric">
              <div class="metric-label">VWAP 偏离</div>
              <div class="metric-value ${valueClass(symbol.vwap_deviation_pct)}">${fmtPlainPct(symbol.vwap_deviation_pct, 3)}</div>
              <div class="metric-copy">短线均价偏离度</div>
            </div>
            <div class="metric">
              <div class="metric-label">买盘墙</div>
              <div class="metric-value">${fmtPriceLevel(symbol.bid_wall_price)}</div>
              <div class="metric-copy">金额 ${fmtNumber(symbol.bid_wall_notional, 0)}</div>
            </div>
            <div class="metric">
              <div class="metric-label">卖盘墙</div>
              <div class="metric-value">${fmtPriceLevel(symbol.ask_wall_price)}</div>
              <div class="metric-copy">金额 ${fmtNumber(symbol.ask_wall_notional, 0)}</div>
            </div>
          </div>
        </div>
      `;
    }

    function renderLiquidityPanel(symbol) {
      const periodData = activeTimeframeData(symbol);
      const depthLive = hasDepthData(symbol);
      const tradeLive = hasCoreTradeData(symbol);
      const totalDepth = Number(symbol.bid_depth_notional || 0) + Number(symbol.ask_depth_notional || 0);
      const bidPct = totalDepth > 0 ? (Number(symbol.bid_depth_notional || 0) / totalDepth) * 100 : 50;
      const askPct = 100 - bidPct;
      if (periodData) {
        const period = periodData.period_label || detailPeriod();
        const amplitude = timeframeAmplitudePct(periodData);
        return `
          <div class="detail-panel">
            <div class="detail-title">${esc(period)} 流动性节奏</div>
            <div class="metric-grid compact">
              <div class="metric"><div class="metric-label">${esc(period)} 成交额</div><div class="metric-value">${fmtNumber(periodData.quote_volume, 0)} USDT</div><div class="metric-copy">该周期最新一根 K 线成交额</div></div>
              <div class="metric"><div class="metric-label">量能 / 均值</div><div class="metric-value ${Number(periodData.volume_multiplier || 0) >= 1.5 ? "up" : "muted"}">${fmtNumber(periodData.volume_multiplier, 2)}x</div><div class="metric-copy">相对近邻 K 线的放量程度</div></div>
              <div class="metric"><div class="metric-label">高低振幅</div><div class="metric-value ${amplitude !== null && amplitude >= 2 ? "mixed" : "muted"}">${amplitude === null ? "--" : fmtPlainPct(amplitude, 2)}</div><div class="metric-copy">${esc(timeframeCandleStatus(periodData))}</div></div>
              <div class="metric"><div class="metric-label">标记价偏离</div><div class="metric-value ${valueClass(periodData.mark_premium_bps)}">${periodData.mark_premium_bps === null || periodData.mark_premium_bps === undefined ? "--" : `${fmtSignedNumber(periodData.mark_premium_bps, 1)} bps`}</div><div class="metric-copy">${esc(timeframeMarkLabel(periodData))}</div></div>
              <div class="metric"><div class="metric-label">成交密集 POC</div><div class="metric-value">${fmtPriceLevel(periodData.profile_poc_price)}</div><div class="metric-copy">成交额 ${fmtNumber(periodData.profile_poc_quote_volume, 0)} USDT</div></div>
              <div class="metric"><div class="metric-label">价值区间</div><div class="metric-value">${fmtPriceLevel(periodData.value_area_low)} / ${fmtPriceLevel(periodData.value_area_high)}</div><div class="metric-copy">判断市场接受区</div></div>
            </div>
            <div class="period-note">${esc(`${period} 周期里优先看成交额、量能倍数和标记价是否同步放大；这些更能说明这一档周期的推动质量。`)}</div>
            <div class="detail-title" style="margin-top:14px">实时补充</div>
            ${depthLive ? `
              <div class="depth-card" style="margin-top:10px">
                <div class="chart-head">
                  <div class="chart-title">买卖盘深度对比</div>
                  <div class="chart-meta">${fmtBps(symbol.spread_bps)} bps</div>
                </div>
                <div class="depth-split">
                  <div class="depth-track">
                    <span style="width:${bidPct.toFixed(1)}%"></span>
                    <span style="width:${askPct.toFixed(1)}%"></span>
                  </div>
                  <div class="depth-meta">
                    <span>买盘 ${fmtNumber(symbol.bid_depth_notional, 0)}</span>
                    <span>卖盘 ${fmtNumber(symbol.ask_depth_notional, 0)}</span>
                  </div>
                </div>
              </div>
            ` : `<div class="detail-placeholder" style="margin-top:10px">当前数据源没有稳定盘口深度，实时补充先保持静默。</div>`}
            <div class="metric-grid compact" style="margin-top:12px">
              <div class="metric"><div class="metric-label">强平状态</div><div class="metric-value ${liquidationStatusClass(symbol)}">${esc(liquidationStatusText(symbol))}</div><div class="metric-copy">总额 ${liquidationTotalHtml(symbol)}</div></div>
              <div class="metric"><div class="metric-label">强平事件 1m</div><div class="metric-value">${fmtNumber(symbol.liquidation_event_count_1m, 0)}</div><div class="metric-copy">近一分钟记录数</div></div>
              <div class="metric"><div class="metric-label">主动买入</div><div class="metric-value">${tradeLive ? `${fmtNumber(Number(symbol.taker_buy_ratio_1m || 0) * 100, 1)}%` : mutedValue()}</div><div class="metric-copy">实时成交主导方向</div></div>
              <div class="metric"><div class="metric-label">盘口失衡</div><div class="metric-value">${depthLive ? `${fmtNumber(Number(symbol.depth_imbalance || 0) * 100, 1)}%` : mutedValue()}</div><div class="metric-copy">正值偏买盘，负值偏卖盘</div></div>
            </div>
          </div>
        `;
      }
      return `
        <div class="detail-panel">
          ${depthLive ? `
            <div class="depth-card">
              <div class="chart-head">
                <div class="chart-title">买卖盘深度对比</div>
                <div class="chart-meta">${fmtBps(symbol.spread_bps)} bps</div>
              </div>
              <div class="depth-split">
                <div class="depth-track">
                  <span style="width:${bidPct.toFixed(1)}%"></span>
                  <span style="width:${askPct.toFixed(1)}%"></span>
                </div>
                <div class="depth-meta">
                  <span>买盘 ${fmtNumber(symbol.bid_depth_notional, 0)}</span>
                  <span>卖盘 ${fmtNumber(symbol.ask_depth_notional, 0)}</span>
                </div>
              </div>
            </div>
          ` : `<div class="detail-placeholder">当前数据源没有稳定盘口深度，流动性看板先保持静默。</div>`}
          <div class="metric-grid compact">
            <div class="metric"><div class="metric-label">强平状态</div><div class="metric-value ${liquidationStatusClass(symbol)}">${esc(liquidationStatusText(symbol))}</div><div class="metric-copy">总额 ${liquidationTotalHtml(symbol)}</div></div>
            <div class="metric"><div class="metric-label">强平事件 1m</div><div class="metric-value">${fmtNumber(symbol.liquidation_event_count_1m, 0)}</div><div class="metric-copy">近一分钟记录数</div></div>
            <div class="metric"><div class="metric-label">多头爆仓</div><div class="metric-value">${liquidationSideHtml(symbol, "long_liquidation_quote_1m")}</div><div class="metric-copy">USDT</div></div>
            <div class="metric"><div class="metric-label">空头爆仓</div><div class="metric-value">${liquidationSideHtml(symbol, "short_liquidation_quote_1m")}</div><div class="metric-copy">USDT</div></div>
            <div class="metric"><div class="metric-label">盘口点差</div><div class="metric-value">${depthLive ? `${fmtBps(symbol.spread_bps)} bps` : mutedValue()}</div><div class="metric-copy">越大越易滑点</div></div>
            <div class="metric"><div class="metric-label">深度下降</div><div class="metric-value">${depthLive ? `${fmtNumber(symbol.depth_drop_pct_1m, 1)}%` : mutedValue()}</div><div class="metric-copy">越快越易插针</div></div>
            <div class="metric"><div class="metric-label">盘口失衡</div><div class="metric-value">${depthLive ? `${fmtNumber(Number(symbol.depth_imbalance || 0) * 100, 1)}%` : mutedValue()}</div><div class="metric-copy">正值偏买盘，负值偏卖盘</div></div>
            <div class="metric"><div class="metric-label">主动买入</div><div class="metric-value">${tradeLive ? `${fmtNumber(Number(symbol.taker_buy_ratio_1m || 0) * 100, 1)}%` : mutedValue()}</div><div class="metric-copy">成交主导方向</div></div>
          </div>
        </div>
      `;
    }

    function renderRealtimeSupplement(symbol) {
      const tradeLive = hasCoreTradeData(symbol);
      const depthLive = hasDepthData(symbol);
      const oiLive = hasOiData(symbol);
      const volumeChart = tradeLive
        ? barChartSvg(symbol.volume_series_5m, rowClass(symbol))
        : chartEmptyHtml("等待成交时序");
      const oiTone = Number(symbol.oi_change_pct_5m || 0) > 0 ? "up" : Number(symbol.oi_change_pct_5m || 0) < 0 ? "down" : "mixed";
      const oiChart = oiLive
        ? lineChartSvg(symbol.oi_series_5m, oiTone)
        : chartEmptyHtml("OI 尚未稳定");
      const depthBalance = clamp((Number(symbol.depth_imbalance || 0) + 1) * 50, 0, 100);
      const totalDepth = Number(symbol.bid_depth_notional || 0) + Number(symbol.ask_depth_notional || 0);
      const bidPct = totalDepth > 0 ? (Number(symbol.bid_depth_notional || 0) / totalDepth) * 100 : 50;
      const askPct = 100 - bidPct;
      const depthBody = depthLive
        ? `
          <div class="depth-split" style="margin-top:14px">
            <div class="depth-track">
              <span style="width:${bidPct.toFixed(1)}%"></span>
              <span style="width:${askPct.toFixed(1)}%"></span>
            </div>
            <div class="depth-meta">
              <span>买盘 ${fmtNumber(symbol.bid_depth_notional, 0)}</span>
              <span>卖盘 ${fmtNumber(symbol.ask_depth_notional, 0)}</span>
            </div>
          </div>
        `
        : chartEmptyHtml("盘口深度未稳定");
      return `
        <div class="detail-title" style="margin:16px 0 10px">实时补充</div>
        <div class="visual-grid">
          ${chartCard("1分钟量能", tradeLive ? `${fmtNumber(symbol.quote_volume_1m, 0)} USDT` : mutedValue(), volumeChart)}
          ${chartCard("5分钟 OI", oiLive ? fmtPctMaybe(symbol.oi_change_pct_5m, true) : mutedValue(), oiChart)}
          ${chartCard("买卖盘深度", depthLive ? `${fmtBps(symbol.spread_bps)} bps` : mutedValue(), depthBody)}
        </div>
        <div class="meter-grid">
          ${meterHtml("主动买入", tradeLive ? Number(symbol.taker_buy_ratio_1m || 0) * 100 : 0, tradeLive ? `${fmtNumber(Number(symbol.taker_buy_ratio_1m || 0) * 100, 1)}%` : "--", Number(symbol.taker_buy_ratio_1m || 0) >= 0.6 ? "up" : Number(symbol.taker_buy_ratio_1m || 0) <= 0.4 ? "down" : "mixed", "看主动成交方向是否持续。")}
          ${meterHtml("盘口失衡", depthLive ? depthBalance : 0, depthLive ? `${fmtNumber(Number(symbol.depth_imbalance || 0) * 100, 1)}%` : "--", Number(symbol.depth_imbalance || 0) >= 0.1 ? "up" : Number(symbol.depth_imbalance || 0) <= -0.1 ? "down" : "mixed", "正值偏买盘，负值偏卖盘。")}
          ${meterHtml("盘口点差", depthLive ? clamp((Number(symbol.spread_bps || 0) / 7) * 100, 0, 100) : 0, depthLive ? `${fmtNumber(symbol.spread_bps, 2)} bps` : "--", Number(symbol.spread_bps || 0) >= 3 ? "down" : Number(symbol.spread_bps || 0) >= 1.8 ? "mixed" : "blue", "越大越容易出现滑点。")}
          ${meterHtml("深度下降", depthLive ? clamp((Number(symbol.depth_drop_pct_1m || 0) / 30) * 100, 0, 100) : 0, depthLive ? `${fmtNumber(symbol.depth_drop_pct_1m, 1)}%` : "--", Number(symbol.depth_drop_pct_1m || 0) >= 15 ? "down" : Number(symbol.depth_drop_pct_1m || 0) >= 8 ? "mixed" : "blue", "下降越快，越要防瞬时插针。")}
        </div>
      `;
    }

    function eventLevel(event) {
      const score = Number(event.score || 0);
      if (score >= 60) return "风险预警";
      if (score >= 45) return "关注信号";
      return "观察信号";
    }

    function eventDirectionClass(event) {
      if (event.direction === "up") return "up";
      if (event.direction === "down") return "down";
      return "mixed";
    }

    function openModal(modal) {
      modal.classList.add("open");
    }

    function closeModal(modal) {
      modal.classList.remove("open");
    }

    function readCollapseState() {
      try {
        return JSON.parse(localStorage.getItem(storageKey("cfm_collapsed_sections")) || "{}");
      } catch (error) {
        return {};
      }
    }

    const defaultCollapsedSections = {
      detail_micro: true,
      detail_reasons: true,
      detail_ai: false,
      events: false,
    };

    function isCollapsed(section) {
      const state = readCollapseState();
      if (Object.prototype.hasOwnProperty.call(state, section)) {
        return Boolean(state[section]);
      }
      return Boolean(defaultCollapsedSections[section]);
    }

    function setCollapsed(section, collapsed) {
      const state = readCollapseState();
      state[section] = Boolean(collapsed);
      localStorage.setItem(storageKey("cfm_collapsed_sections"), JSON.stringify(state));
    }

    function updateCollapseButton(button, collapsed) {
      if (!button) return;
      button.setAttribute("aria-expanded", collapsed ? "false" : "true");
      const icon = button.querySelector(".collapse-icon");
      if (icon) icon.textContent = collapsed ? "+" : "-";
    }

    function collapseClass(section) {
      return isCollapsed(section) ? " collapsed" : "";
    }

    function collapseHead(section, title, meta = "") {
      const collapsed = isCollapsed(section);
      const metaHtml = meta ? `<span class="collapse-meta">${esc(meta)}</span>` : "";
      return `
        <button class="collapse-head" data-collapse="${esc(section)}" type="button" aria-expanded="${collapsed ? "false" : "true"}">
          <span class="collapse-main">
            <span class="collapse-title">${esc(title)}</span>
            ${metaHtml}
          </span>
          <span class="collapse-icon">${collapsed ? "+" : "-"}</span>
        </button>
      `;
    }

    function applyEventsCollapseState() {
      const collapsed = isCollapsed("events");
      eventsEl.classList.toggle("collapsed-list", collapsed);
      updateCollapseButton(eventsCollapseBtn, collapsed);
    }

    function renderMetricSummary(symbol, extraClass = "") {
      const tradeLive = hasCoreTradeData(symbol);
      const oiLive = hasOiData(symbol);
      const depthLive = hasDepthData(symbol);
      const items = [
        ["1m波动", fmtPctMaybe(symbol.price_move_pct_1m, tradeLive)],
        ["1m成交额", fmtNumberMaybe(symbol.quote_volume_1m, 0, tradeLive)],
        ["1m量能", tradeLive ? `<span class="${rowClass(symbol)}">${fmtNumber(symbol.volume_multiplier, 2)}x</span>` : mutedValue()],
        ["OI 5m", fmtPctMaybe(symbol.oi_change_pct_5m, oiLive)],
        ["主动买入", tradeLive ? `${fmtNumber(Number(symbol.taker_buy_ratio_1m || 0) * 100, 1)}%` : mutedValue()],
        ["爆仓1m", `<span class="${liquidationStatusClass(symbol)}">${esc(liquidationStatusText(symbol))}</span>`],
        ["点差", depthLive ? `${fmtBps(symbol.spread_bps)} bps` : mutedValue()]
      ];
      return `
        <div class="metric-summary ${extraClass}">
          ${items.map(([label, value]) => `
            <div class="summary-chip">
              <span class="summary-label">${esc(label)}</span>
              <span class="summary-value">${value}</span>
            </div>
          `).join("")}
        </div>
      `;
    }

    function symbolNames(symbols) {
      return (symbols || [])
        .map((symbol) => String(symbol.symbol || symbol || "").toUpperCase())
        .filter(Boolean);
    }

    function currentRowSymbolOrder() {
      return Array.from(symbolsEl.querySelectorAll("tr[data-symbol]"))
        .map((row) => String(row.dataset.symbol || "").toUpperCase())
        .filter(Boolean);
    }

    function orderSymbolsByNames(symbols, names) {
      const bySymbol = new Map((symbols || []).map((symbol) => [String(symbol.symbol || "").toUpperCase(), symbol]));
      const seen = new Set();
      const ordered = [];
      for (const name of names || []) {
        const key = String(name || "").toUpperCase();
        if (!key || seen.has(key) || !bySymbol.has(key)) continue;
        ordered.push(bySymbol.get(key));
        seen.add(key);
      }
      for (const symbol of symbols || []) {
        const key = String(symbol.symbol || "").toUpperCase();
        if (!key || seen.has(key)) continue;
        ordered.push(symbol);
        seen.add(key);
      }
      return ordered;
    }

    function rowAfterPointer(clientY) {
      const rows = Array.from(symbolsEl.querySelectorAll("tr[data-symbol]:not(.dragging)"));
      let closest = { offset: Number.NEGATIVE_INFINITY, element: null };
      rows.forEach((row) => {
        const box = row.getBoundingClientRect();
        const offset = clientY - box.top - box.height / 2;
        if (offset < 0 && offset > closest.offset) {
          closest = { offset, element: row };
        }
      });
      return closest.element;
    }

    function cleanupSymbolDrag() {
      window.removeEventListener("pointermove", moveSymbolDrag);
      window.removeEventListener("pointerup", endSymbolDrag);
      window.removeEventListener("pointercancel", cancelSymbolDrag);
      symbolsEl.classList.remove("drag-active");
    }

    function beginSymbolDrag(event) {
      if (event.button !== undefined && event.button !== 0) return;
      const row = event.target.closest("tr[data-symbol]");
      if (!row) return;
      event.preventDefault();
      event.stopPropagation();
      symbolOrderDrag = {
        row,
        moved: false,
        pointerId: event.pointerId,
      };
      row.classList.add("dragging");
      symbolsEl.classList.add("drag-active");
      try {
        event.currentTarget.setPointerCapture(event.pointerId);
      } catch (error) {
        // Pointer capture may be unavailable in older embedded browsers.
      }
      window.addEventListener("pointermove", moveSymbolDrag, { passive: false });
      window.addEventListener("pointerup", endSymbolDrag);
      window.addEventListener("pointercancel", cancelSymbolDrag);
    }

    function moveSymbolDrag(event) {
      if (!symbolOrderDrag) return;
      event.preventDefault();
      event.stopPropagation();
      const afterRow = rowAfterPointer(event.clientY);
      if (afterRow) {
        symbolsEl.insertBefore(symbolOrderDrag.row, afterRow);
      } else {
        symbolsEl.appendChild(symbolOrderDrag.row);
      }
      symbolOrderDrag.moved = true;
      lastSymbols = orderSymbolsByNames(lastSymbols, currentRowSymbolOrder());
    }

    async function saveSymbolOrder(order) {
      if (!order.length) return;
      symbolInputEl.value = order.join(", ");
      inputTouched = false;
      try {
        const response = await apiFetch("/api/symbols", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ symbols: order })
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "排序保存失败");
        updatedEl.textContent = "监控顺序已保存";
      } catch (error) {
        updatedEl.textContent = error.message || "排序保存失败";
      }
    }

    function endSymbolDrag(event) {
      if (!symbolOrderDrag) return;
      if (event) {
        event.preventDefault();
        event.stopPropagation();
      }
      const moved = symbolOrderDrag.moved;
      const row = symbolOrderDrag.row;
      row.classList.remove("dragging");
      symbolOrderDrag = null;
      cleanupSymbolDrag();

      if (!moved) return;
      const order = currentRowSymbolOrder();
      const orderedSymbols = orderSymbolsByNames(lastSymbols, order);
      renderSymbols(orderedSymbols);
      renderDetail(orderedSymbols);
      renderEvents(lastEvents);
      saveSymbolOrder(order);
    }

    function cancelSymbolDrag(event) {
      if (!symbolOrderDrag) return;
      if (event) {
        event.preventDefault();
        event.stopPropagation();
      }
      symbolOrderDrag.row.classList.remove("dragging");
      symbolOrderDrag = null;
      cleanupSymbolDrag();
      renderSymbols(lastSymbols);
    }

    function renderSymbols(symbols) {
      lastSymbols = symbols || [];
      countEl.textContent = `${symbols.length} 个合约`;
      if (selectedSymbol && !symbols.some((symbol) => symbol.symbol === selectedSymbol)) {
        selectedSymbol = symbols[0] ? symbols[0].symbol : null;
        if (!selectedSymbol) setDrawerOpen(false);
      }
      if (!inputTouched) {
        symbolInputEl.value = symbolNames(symbols).join(", ");
      }

      symbolsEl.innerHTML = symbols.map((symbol) => `
        <tr data-symbol="${esc(symbol.symbol)}" class="${symbol.symbol === selectedSymbol ? "selected" : ""}">
          <td data-label="合约">
            <div class="symbol-cell">
              <button class="drag-handle js-drag-handle" type="button" aria-label="拖拽调整顺序" title="拖拽调整顺序"></button>
              <div class="symbol-text">
                <div class="symbol">${esc(symbol.symbol)}</div>
                <div class="symbol-meta">
                  <span class="sub-pill">${esc(signalTag(symbol))}</span>
                  <button class="row-action-btn js-threshold ${thresholdButtonText(symbol.symbol) !== "规则" ? "active" : ""}" data-symbol="${esc(symbol.symbol)}" type="button" title="${esc(currentThresholdText(symbol.symbol))}">${esc(thresholdButtonText(symbol.symbol))}</button>
                </div>
              </div>
            </div>
          </td>
          <td data-label="异常分"><span class="score">${fmtNumber(symbol.score, 1)}</span></td>
          <td data-label="风险"><span class="risk ${riskClass(symbol.risk_level)}">${esc(symbol.risk_level || "低风险")}</span></td>
          <td data-label="倾向"><span class="tag ${biasClass(symbol.bias)}">${esc(shortBias(symbol.bias))}</span></td>
          <td data-label="价格">${fmtNumber(symbol.price, 8)}</td>
          <td data-label="1分钟">${fmtPct(symbol.price_move_pct_1m)}</td>
          <td data-label="5分钟">${fmtPct(symbol.price_move_pct_5m)}</td>
          <td data-label="1分钟成交额">${fmtNumber(symbol.quote_volume_1m, 0)}</td>
          <td data-label="放大倍数" class="${rowClass(symbol)}">${fmtNumber(symbol.volume_multiplier, 2)}x</td>
          <td data-label="OI 5分钟">${fmtPct(symbol.oi_change_pct_5m)}</td>
          <td data-label="爆仓1m">${liquidationTotalHtml(symbol)}</td>
          <td data-label="点差">${fmtBps(symbol.spread_bps)}</td>
        </tr>
      `).join("");

      symbolsEl.querySelectorAll("tr").forEach((row) => {
        row.addEventListener("click", () => {
          selectedSymbol = row.dataset.symbol;
          openDetailDrawer(selectedSymbol);
          renderSymbols(symbols);
          renderDetail(symbols);
          renderEvents(lastEvents);
        });
      });
      symbolsEl.querySelectorAll(".js-threshold").forEach((button) => {
        button.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          openThresholdModal(button.dataset.symbol);
        });
      });
      symbolsEl.querySelectorAll(".js-drag-handle").forEach((handle) => {
        handle.addEventListener("pointerdown", beginSymbolDrag);
        handle.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
        });
      });
    }

    function renderDetail(symbols) {
      const symbol = symbols.find((item) => item.symbol === selectedSymbol);
      if (!symbol) {
        delete detailEl.dataset.symbol;
        detailEl.innerHTML = `
          <div class="drawer-empty">
            <div class="drawer-empty-title">点击左侧合约展开详情</div>
            <div class="drawer-empty-copy">这里会先看周期分析，再切到 AI、结构位和流动性视图。</div>
          </div>
        `;
        return;
      }

      selectedSymbol = symbol.symbol;
      const preservedScroll = captureDetailScroll(symbol.symbol);
      const tradeLive = hasCoreTradeData(symbol);
      const activeTab = detailTab();
      maybeLoadAIAnalysis(symbol);
      maybeLoadTimeframeAnalysis(symbol.symbol);
      maybeLoadTimeframeConfluence(symbol.symbol);
      detailEl.innerHTML = `
        <div class="detail-head">
          <div class="detail-meta">
            <div class="detail-title-row">
              <div class="detail-symbol">${esc(symbol.symbol)}</div>
              <div class="detail-tools">
                <span class="risk ${riskClass(symbol.risk_level)}">${esc(symbol.risk_level || "低风险")}</span>
                <span class="tag ${biasClass(symbol.bias)}">${esc(shortBias(symbol.bias || ""))}</span>
              </div>
            </div>
            <div class="detail-price-wrap">
              <div class="detail-price-label">当前价格</div>
              <div class="detail-price ${valueClass(symbol.price_move_pct_1m)}">${tradeLive ? fmtNumber(symbol.price, 8) : "--"}</div>
            </div>
            <div class="detail-bias">${esc(symbol.bias || "观察：暂无明确方向")}</div>
          </div>
          <div>
            <span class="score">${fmtNumber(symbol.score, 1)}</span>
            <div class="cell-sub" style="margin-top:8px;text-align:right">置信度 ${tradeLive ? `${fmtNumber(symbol.confidence, 1)}%` : "--"}</div>
          </div>
        </div>
        ${dataBannerHtml(symbol)}
        ${renderMetricSummary(symbol, "always-visible")}
        ${renderPeriodFloatingSwitch()}
        ${renderTimeframeSection(symbol)}
        <div class="detail-tabs">
          ${detailTabButton("overview", "总览")}
          ${detailTabButton("ai", "AI 分析")}
          ${detailTabButton("structure", "结构位")}
          ${detailTabButton("liquidity", "流动性")}
        </div>
        ${activeTab === "ai" ? `
          <div class="detail-panel collapsible${collapseClass("detail_ai")}">
            <div class="ai-panel-head">
              ${collapseHead("detail_ai", `AI 会优先基于当前 ${detailPeriodLabel()} 档位生成分析`)}
              <div class="ai-actions">
                <button class="inline-link" id="ai-copy-btn" type="button" ${hasAIAnalysis(symbol.symbol) ? "" : "disabled"}>复制观点</button>
                <button class="inline-link" id="ai-refresh-btn" type="button">刷新</button>
              </div>
            </div>
            <div class="detail-list ai-inline collapsible-body" id="ai-block">${renderAIBlock(symbol.symbol)}</div>
          </div>
        ` : ""}
        ${activeTab === "overview" ? renderOverviewPanel(symbol) : ""}
        ${activeTab === "structure" ? renderStructurePanel(symbol) : ""}
        ${activeTab === "liquidity" ? renderLiquidityPanel(symbol) : ""}
      `;
      detailEl.dataset.symbol = symbol.symbol;
      pendingDetailRefresh = false;
      if (activeTab === "ai") pendingAIRefreshSymbol = null;
      restoreDetailScroll(symbol.symbol, preservedScroll, activeTab);
    }

    function renderEvents(events) {
      const visibleEvents = selectedSymbol
        ? (events || []).filter((event) => String(event.symbol || "").toUpperCase() === selectedSymbol)
        : (events || []);
      alertCountEl.textContent = String(visibleEvents.length);
      applyEventsCollapseState();
      if (!visibleEvents.length) {
        eventsEl.innerHTML = `<div class="empty">${selectedSymbol ? "暂无该合约报警" : "暂无报警"}</div>`;
        return;
      }
      function verdictText(verdict) {
        return {
          validated: "验证",
          failed: "失败",
          faded: "回吐",
          neutral: "中性"
        }[verdict] || "待判";
      }
      function followupTone(item) {
        if (!item || item.status !== "resolved") return "pending";
        if (item.verdict === "validated") return "up";
        if (item.verdict === "failed") return "down";
        return "mixed";
      }
      function followupText(item) {
        if (!item) return "";
        const label = item.label || `${item.horizon_minutes || 0}m`;
        if (item.status !== "resolved") return `${label} 待回写`;
        const directional = item.directional_close_bps === undefined ? item.close_bps : item.directional_close_bps;
        return `${label} ${verdictText(item.verdict)} ${fmtSignedNumber(directional, 1)}bp · 顺${fmtNumber(item.max_favorable_bps, 1)} · 逆${fmtNumber(item.max_adverse_bps, 1)}`;
      }
      function decisionRow(event) {
        const decision = event.decision || {};
        if (!decision.directional || !decision.invalidation_price) return "";
        const direction = String(event.direction || "");
        const invalidationSign = direction === "up" ? "跌破" : "站上";
        const targetText = decision.target_price
          ? `目标 ${fmtPriceLevel(decision.target_price)}${decision.reward_risk ? ` · RR ${fmtNumber(decision.reward_risk, 2)}` : ""}`
          : "目标等待结构确认";
        const qualityText = {
          high: "高质量",
          medium: "中等质量",
          low: "低质量"
        }[decision.boundary_quality] || "";
        const basisText = decision.invalidation_basis
          ? ` · 依据 ${decision.invalidation_basis}${qualityText ? ` / ${qualityText}` : ""}`
          : "";
        return `
          <div class="event-row">
            <span class="event-label">边界</span>
            <span class="event-text">${esc(invalidationSign)} ${fmtPriceLevel(decision.invalidation_price)} 失效 · 风险 ${fmtNumber(decision.invalidation_bps, 1)}bp · ${esc(targetText + basisText)}</span>
          </div>
        `;
      }
      function statsRow(event) {
        const stats = event.signal_stats || {};
        const sampleCount = Number(stats.sample_count || 0);
        if (!sampleCount) return "";
        const sampleTone = sampleCount >= 20 ? "" : " muted";
        return `
          <div class="event-row">
            <span class="event-label">同币</span>
            <span class="event-text${sampleTone}">${esc(stats.label || "15m")} 同币同向样本 ${sampleCount} · 胜率 ${fmtNumber(stats.win_rate, 1)}% · 均值 ${fmtSignedNumber(stats.avg_close_bps, 1)}bp · 顺${fmtNumber(stats.avg_favorable_bps, 1)} / 逆${fmtNumber(stats.avg_adverse_bps, 1)}</span>
          </div>
        `;
      }
      function comboStatsRow(event) {
        const combo = event.trigger_combo || {};
        const stats = event.combo_stats || {};
        const comboLabel = combo.label || stats.combo_label || "";
        const sampleCount = Number(stats.sample_count || 0);
        if (!comboLabel && !sampleCount) return "";
        const sampleTone = sampleCount >= 20 ? "" : " muted";
        const statsText = sampleCount
          ? `${esc(stats.label || "15m")} 同组合样本 ${sampleCount} · 胜率 ${fmtNumber(stats.win_rate, 1)}% · 均值 ${fmtSignedNumber(stats.avg_close_bps, 1)}bp · 顺${fmtNumber(stats.avg_favorable_bps, 1)} / 逆${fmtNumber(stats.avg_adverse_bps, 1)}`
          : "同组合样本不足，先只作为分类观察";
        return `
          <div class="event-row">
            <span class="event-label">组合</span>
            <span class="event-text${sampleTone}">${comboLabel ? `${esc(comboLabel)} · ` : ""}${statsText}</span>
          </div>
        `;
      }
      function followupRow(event) {
        const items = event.followups || [];
        if (!items.length) return "";
        return `
          <div class="event-row">
            <span class="event-label">复盘</span>
            <span class="followup-pills">${items.map((item) => `<span class="followup-pill ${followupTone(item)}">${esc(followupText(item))}</span>`).join("")}</span>
          </div>
        `;
      }
      eventsEl.innerHTML = visibleEvents.map((event) => {
        const reasons = (event.reasons || []).join("; ") || "暂无明确触发项";
        const suggestions = (event.suggestions || []).join("; ") || "继续观察盘口与量价变化";
        const aiSummary = (event.ai_summary || []).join("; ").trim();
        const aiAnalysis = String(event.ai_analysis || "").trim();
        const aiObservation = aiSummary || aiAnalysis;
        const observation = aiObservation ? `${suggestions}; AI: ${aiObservation}` : suggestions;
        const directionClass = eventDirectionClass(event);
        const directionLabel = directionText[event.direction] || event.direction || "异常";
        return `
          <div class="event">
            <div class="event-head">
              <div class="event-main">
                <div class="event-title ${directionClass}">${esc(event.symbol)} <span class="event-score-text">${fmtNumber(event.score, 1)}/100</span></div>
                <div class="event-meta">
                  <span>${esc(event.created_at || "")}</span>
                  <span>${esc(eventLevel(event))}</span>
                  <span>${esc(event.risk_level || "")}</span>
                  <span>${esc(event.bias || "")}</span>
                </div>
              </div>
              <span class="event-badge ${directionClass}">${esc(directionLabel)}</span>
            </div>
            <div class="event-row"><span class="event-label">触发原因</span><span class="event-text">${esc(reasons)}</span></div>
            <div class="event-row"><span class="event-label">观察建议</span><span class="event-text ${aiObservation ? "" : "muted"}">${esc(observation)}</span></div>
            ${decisionRow(event)}
            ${statsRow(event)}
            ${comboStatsRow(event)}
            ${followupRow(event)}
          </div>
        `;
      }).join("");
    }

    async function refresh() {
      try {
        const response = await apiFetch("/api/state", { cache: "no-store" });
        if (!response.ok) return;
        const data = await response.json();
        const exchangeLabel = (data.exchange || "binance_usdm").startsWith("okx") ? "OKX" : "Binance";
        const transportLabel = data.data_source === "websocket" ? "WebSocket" : "REST";
        const symbols = data.symbols || [];
        const displaySymbols = symbolOrderDrag
          ? orderSymbolsByNames(symbols, currentRowSymbolOrder())
          : symbols;
        sourceLabelEl.textContent = `${exchangeLabel} ${transportLabel}`;
        renderSourceHealth(data.source_health || {});
        if (symbolOrderDrag) {
          lastSymbols = displaySymbols;
        } else {
          renderSymbols(displaySymbols);
        }
        if (shouldDeferDetailRender(displaySymbols)) {
          pendingDetailRefresh = true;
        } else {
          pendingDetailRefresh = false;
          pendingAIRefreshSymbol = null;
          renderDetail(displaySymbols);
        }
        lastEvents = data.events || [];
        renderEvents(lastEvents);
        const timeText = new Date().toLocaleTimeString();
        updatedEl.textContent = data.source_note ? `${data.source_note} · ${timeText}` : `已更新 ${timeText}`;
      } catch (error) {
        updatedEl.textContent = "面板连接中断";
      }
    }

    async function saveSymbols() {
      const symbols = symbolInputEl.value
        .split(/[\s,，;；]+/)
        .map((symbol) => symbol.trim())
        .filter(Boolean);

      saveSymbolsEl.disabled = true;
      saveSymbolsEl.textContent = "保存中";
      try {
        const response = await apiFetch("/api/symbols", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ symbols })
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "保存失败");
        inputTouched = false;
        await refresh();
        updatedEl.textContent = "监控列表已更新";
      } catch (error) {
        updatedEl.textContent = error.message || "保存失败";
      } finally {
        saveSymbolsEl.disabled = false;
        saveSymbolsEl.textContent = "保存监控";
      }
    }

    symbolInputEl.addEventListener("input", () => { inputTouched = true; });
    symbolInputEl.addEventListener("keydown", (event) => {
      if (event.key === "Enter") saveSymbols();
    });
    saveSymbolsEl.addEventListener("click", saveSymbols);

    const btnTelegram = document.getElementById("btn-telegram");
    const btnAI = document.getElementById("btn-ai");
    const btnSignals = document.getElementById("btn-signals");
    const tgToggle = document.getElementById("tg-toggle");
    const tgToggleLabel = document.getElementById("tg-toggle-label");
    const tgUsersEl = document.getElementById("tg-users");
    const tgAddUserBtn = document.getElementById("tg-add-user-btn");
    const tgSaveBtn = document.getElementById("tg-save-btn");
    const tgTestBtn = document.getElementById("tg-test-btn");
    let tgUsers = [];
    let tgEnabled = false;

    function setToggle(toggle, label, enabled) {
      toggle.classList.toggle("active", enabled);
      label.textContent = enabled ? "开启" : "关闭";
    }

    const signalFields = {
      liquidation: {
        toggle: document.getElementById("signal-liquidation-toggle"),
        label: document.getElementById("signal-liquidation-label"),
        input: document.getElementById("signal-liquidation-threshold")
      },
      spread: {
        toggle: document.getElementById("signal-spread-toggle"),
        label: document.getElementById("signal-spread-label"),
        input: document.getElementById("signal-spread-threshold")
      },
      depth_imbalance: {
        toggle: document.getElementById("signal-depth-imbalance-toggle"),
        label: document.getElementById("signal-depth-imbalance-label"),
        input: document.getElementById("signal-depth-imbalance-threshold")
      },
      depth_drop: {
        toggle: document.getElementById("signal-depth-drop-toggle"),
        label: document.getElementById("signal-depth-drop-label"),
        input: document.getElementById("signal-depth-drop-threshold")
      }
    };
    const signalSaveBtn = document.getElementById("signals-save-btn");
    let signalSettings = {};

    function applySignalSettings(settings) {
      signalSettings = settings || {};
      Object.entries(signalFields).forEach(([key, field]) => {
        const item = signalSettings[key] || {};
        const enabled = Boolean(item.enabled ?? true);
        setToggle(field.toggle, field.label, enabled);
        field.input.value = item.threshold ?? field.input.value;
      });
    }

    function collectSignalSettings() {
      const payload = {};
      Object.entries(signalFields).forEach(([key, field]) => {
        payload[key] = {
          enabled: field.toggle.classList.contains("active"),
          threshold: Number(field.input.value) || 0
        };
      });
      return payload;
    }

    async function loadSignalSettings() {
      try {
        const response = await apiFetch("/api/signal_settings", { cache: "no-store" });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "读取失败");
        applySignalSettings(data);
      } catch (error) {
        updatedEl.textContent = error.message || "信号设置读取失败";
      }
    }

    async function saveSignalSettings() {
      signalSaveBtn.disabled = true;
      signalSaveBtn.textContent = "保存中";
      try {
        const response = await apiFetch("/api/signal_settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(collectSignalSettings())
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "保存失败");
        applySignalSettings(data.settings || {});
        updatedEl.textContent = "信号评分设置已保存";
        closeModal(signalsModal);
        await refresh();
      } catch (error) {
        updatedEl.textContent = error.message || "信号设置保存失败";
      } finally {
        signalSaveBtn.disabled = false;
        signalSaveBtn.textContent = "保存设置";
      }
    }

    btnSignals.addEventListener("click", async () => {
      await loadSignalSettings();
      openModal(signalsModal);
    });
    Object.values(signalFields).forEach((field) => {
      field.toggle.addEventListener("click", () => {
        const enabled = !field.toggle.classList.contains("active");
        setToggle(field.toggle, field.label, enabled);
      });
    });
    signalSaveBtn.addEventListener("click", saveSignalSettings);

    function renderTgUsers() {
      tgUsersEl.innerHTML = tgUsers.map((user, index) => `
        <div class="telegram-user" data-index="${index}">
          <input class="tg-user-name" placeholder="用户名" value="${esc(user.name || "")}">
          <input class="tg-user-token" type="password" placeholder="Bot Token" value="${user.bot_token_set ? "********" : esc(user.bot_token || "")}">
          <input class="tg-user-chat" placeholder="Chat ID，多个用逗号" value="${esc((user.chat_ids || []).join(", "))}">
          <button class="small-btn secondary tg-user-remove" type="button">删除</button>
        </div>
      `).join("");
      tgUsersEl.querySelectorAll(".tg-user-remove").forEach((button) => {
        button.addEventListener("click", () => {
          const row = button.closest(".telegram-user");
          tgUsers.splice(Number(row.dataset.index), 1);
          renderTgUsers();
        });
      });
    }

    async function loadTelegramConfig() {
      try {
        const response = await apiFetch("/api/telegram", { cache: "no-store" });
        const data = await response.json();
        tgEnabled = Boolean(data.enabled);
        tgUsers = data.users || [];
        if (!tgUsers.length) {
          tgUsers = [{ name: "默认用户", enabled: true, bot_token: "", chat_ids: [] }];
        }
        setToggle(tgToggle, tgToggleLabel, tgEnabled);
        renderTgUsers();
      } catch (error) {
        updatedEl.textContent = "推送配置读取失败";
      }
    }

    function collectTgUsers() {
      return Array.from(tgUsersEl.querySelectorAll(".telegram-user")).map((row, index) => {
        const chatIds = row.querySelector(".tg-user-chat").value
          .split(/[\s,，;；]+/)
          .map((item) => item.trim())
          .filter(Boolean);
        return {
          name: row.querySelector(".tg-user-name").value.trim() || `用户${index + 1}`,
          enabled: true,
          bot_token: row.querySelector(".tg-user-token").value.trim(),
          chat_ids: chatIds
        };
      });
    }

    async function saveTelegramConfig() {
      const body = { enabled: tgEnabled, users: collectTgUsers() };
      try {
        const response = await apiFetch("/api/telegram", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "保存失败");
        updatedEl.textContent = "推送设置已保存";
        closeModal(telegramModal);
      } catch (error) {
        updatedEl.textContent = error.message || "推送设置保存失败";
      }
    }

    async function testTelegramConfig() {
      const body = { enabled: tgEnabled, users: collectTgUsers() };
      tgTestBtn.disabled = true;
      tgTestBtn.textContent = "发送中";
      try {
        const response = await apiFetch("/api/telegram/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "测试失败");
        const sent = data.result && data.result.sent ? data.result.sent : 0;
        updatedEl.textContent = `测试推送已发送 ${sent} 条`;
      } catch (error) {
        updatedEl.textContent = error.message || "测试推送失败";
      } finally {
        tgTestBtn.disabled = false;
        tgTestBtn.textContent = "发送测试";
      }
    }

    function setOfStars(value) {
      return value.length > 0 && value.split("").every((char) => char === "*");
    }

    btnTelegram.addEventListener("click", async () => {
      await loadTelegramConfig();
      openModal(telegramModal);
    });
    tgToggle.addEventListener("click", () => {
      tgEnabled = !tgEnabled;
      setToggle(tgToggle, tgToggleLabel, tgEnabled);
    });
    tgAddUserBtn.addEventListener("click", () => {
      tgUsers.push({ name: `用户${tgUsers.length + 1}`, enabled: true, bot_token: "", chat_ids: [] });
      renderTgUsers();
    });
    tgSaveBtn.addEventListener("click", saveTelegramConfig);
    tgTestBtn.addEventListener("click", testTelegramConfig);

    const aiToggle = document.getElementById("ai-toggle");
    const aiToggleLabel = document.getElementById("ai-toggle-label");
    const aiProvider = document.getElementById("ai-provider");
    const aiBaseUrl = document.getElementById("ai-base-url");
    const aiKey = document.getElementById("ai-key");
    const aiModel = document.getElementById("ai-model");
    const aiThreshold = document.getElementById("ai-threshold");
    const aiRetryCooldown = document.getElementById("ai-retry-cooldown");
    const aiTimeout = document.getElementById("ai-timeout");
    const aiTriggerMode = document.getElementById("ai-trigger-mode");
    const aiTriggerFields = {
      score: [document.getElementById("ai-trigger-score"), document.getElementById("ai-trigger-score-value")],
      quote_volume_1m: [document.getElementById("ai-trigger-volume"), document.getElementById("ai-trigger-volume-value")],
      volume_multiplier: [document.getElementById("ai-trigger-multiplier"), document.getElementById("ai-trigger-multiplier-value")],
      price_move_pct_1m_abs: [document.getElementById("ai-trigger-price"), document.getElementById("ai-trigger-price-value")],
      oi_change_pct_5m_abs: [document.getElementById("ai-trigger-oi"), document.getElementById("ai-trigger-oi-value")],
      liquidation_total_quote_1m: [document.getElementById("ai-trigger-liquidation"), document.getElementById("ai-trigger-liquidation-value")]
    };
    const aiSaveBtn = document.getElementById("ai-save-btn");
    let aiEnabled = false;

    function syncAIScoreThresholdFromMain() {
      const fields = aiTriggerFields.score;
      if (!fields || !fields[0].checked) return;
      fields[1].value = aiThreshold.value || fields[1].value;
    }

    function syncAIMainThresholdFromScore() {
      const fields = aiTriggerFields.score;
      if (!fields || !fields[0].checked) return;
      aiThreshold.value = fields[1].value || aiThreshold.value;
    }

    async function loadAIConfig() {
      try {
        const response = await apiFetch("/api/ai/config", { cache: "no-store" });
        const data = await response.json();
        aiEnabled = Boolean(data.enabled);
        setToggle(aiToggle, aiToggleLabel, aiEnabled);
        aiProvider.value = data.provider || "openai";
        aiBaseUrl.value = data.base_url || "";
        aiKey.value = data.api_key || "";
        aiModel.value = data.model || "gpt-4o-mini";
        aiThreshold.value = data.activation_threshold || 60;
        aiRetryCooldown.value = data.retry_cooldown_seconds || 120;
        aiTimeout.value = data.request_timeout_seconds || 30;
        const triggers = data.triggers || {};
        const conditions = triggers.conditions || {};
        aiTriggerMode.value = triggers.mode || "any";
        Object.entries(aiTriggerFields).forEach(([key, fields]) => {
          const cfg = conditions[key] || {};
          fields[0].checked = Boolean(cfg.enabled ?? (key === "score"));
          const defaultThreshold = key === "score" ? (data.activation_threshold || fields[1].value) : fields[1].value;
          fields[1].value = cfg.threshold ?? defaultThreshold;
        });
      } catch (error) {
        updatedEl.textContent = "AI 配置读取失败";
      }
    }

    function collectAITriggers() {
      const conditions = {};
      Object.entries(aiTriggerFields).forEach(([key, fields]) => {
        conditions[key] = {
          enabled: fields[0].checked,
          threshold: Number(fields[1].value) || 0
        };
      });
      return { mode: aiTriggerMode.value || "any", conditions };
    }

    async function saveAIConfig() {
      syncAIScoreThresholdFromMain();
      const body = {
        enabled: aiEnabled,
        provider: aiProvider.value,
        base_url: aiBaseUrl.value.trim(),
        model: aiModel.value.trim() || "gpt-4o-mini",
        activation_threshold: Number(aiThreshold.value) || 60,
        retry_cooldown_seconds: Number(aiRetryCooldown.value) || 120,
        request_timeout_seconds: Math.max(5, Math.min(30, Number(aiTimeout.value) || 30)),
        triggers: collectAITriggers()
      };
      if (aiKey.value && !aiKey.value.includes("***") && setOfStars(aiKey.value) === false) {
        body.api_key = aiKey.value;
      }
      try {
        const response = await apiFetch("/api/ai/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "保存失败");
        clearStoredAIResults();
        updatedEl.textContent = "AI 设置已保存";
        closeModal(aiModal);
      } catch (error) {
        updatedEl.textContent = error.message || "AI 设置保存失败";
      }
    }

    btnAI.addEventListener("click", async () => {
      await loadAIConfig();
      openModal(aiModal);
    });
    aiToggle.addEventListener("click", () => {
      aiEnabled = !aiEnabled;
      setToggle(aiToggle, aiToggleLabel, aiEnabled);
    });
    aiThreshold.addEventListener("input", syncAIScoreThresholdFromMain);
    aiTriggerFields.score[0].addEventListener("change", syncAIScoreThresholdFromMain);
    aiTriggerFields.score[1].addEventListener("input", syncAIMainThresholdFromScore);
    aiSaveBtn.addEventListener("click", saveAIConfig);

    async function loadSymbolThresholds() {
      try {
        const response = await apiFetch("/api/symbol_thresholds", { cache: "no-store" });
        const data = await response.json();
        globalThreshold = Number(data.default_score || 60);
        symbolThresholds = data.symbol_thresholds || {};
      } catch (error) {
        symbolThresholds = {};
      }
    }

    function openThresholdModal(symbol) {
      thresholdEditingSymbol = symbol;
      const threshold = symbolThresholds[symbol] || {};
      thresholdSymbolEl.value = symbol;
      thresholdInputEl.value = threshold && threshold.anomaly_score !== undefined ? threshold.anomaly_score : "";
      thresholdInputEl.placeholder = String(globalThreshold);
      applyThresholdRuleForm(threshold.push_rules, threshold.anomaly_score ?? globalThreshold);
      thresholdHintEl.textContent = `全局默认 ${fmtNumber(globalThreshold, 1)} 分；当前 ${currentThresholdText(symbol)}。`;
      openModal(thresholdModal);
      thresholdInputEl.focus();
    }

    async function saveSymbolThreshold(symbol, config) {
      try {
        const response = await apiFetch("/api/symbol_thresholds", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ symbol, anomaly_score: config.anomaly_score, push_rules: config.push_rules })
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "保存失败");
        if (config.anomaly_score === null && !hasEnabledThresholdRules(config.push_rules)) {
          delete symbolThresholds[symbol];
        } else {
          const nextConfig = {};
          if (config.anomaly_score !== null) nextConfig.anomaly_score = Number(config.anomaly_score);
          if (hasEnabledThresholdRules(config.push_rules)) nextConfig.push_rules = config.push_rules;
          symbolThresholds[symbol] = nextConfig;
        }
        updatedEl.textContent = `${symbol} 推送规则已更新`;
        closeModal(thresholdModal);
        await refresh();
      } catch (error) {
        updatedEl.textContent = error.message || "规则保存失败";
      }
    }

    document.getElementById("threshold-save-btn").addEventListener("click", () => {
      if (!thresholdEditingSymbol) return;
      const raw = thresholdInputEl.value.trim();
      const pushRules = collectThresholdRules();
      const parsedScore = Number(raw);
      const anomalyScore = raw === "" || Number.isNaN(parsedScore)
        ? null
        : Math.max(0, Math.min(100, parsedScore));
      if (raw === "") {
        saveSymbolThreshold(thresholdEditingSymbol, { anomaly_score: null, push_rules: pushRules });
      } else {
        saveSymbolThreshold(thresholdEditingSymbol, { anomaly_score: anomalyScore, push_rules: pushRules });
      }
    });
    document.getElementById("threshold-reset-btn").addEventListener("click", () => {
      if (thresholdEditingSymbol) {
        applyThresholdRuleForm(null, globalThreshold);
        thresholdInputEl.value = "";
        saveSymbolThreshold(thresholdEditingSymbol, { anomaly_score: null, push_rules: null });
      }
    });

    async function fetchAIAnalysis(symbol, force = false, period = detailPeriod()) {
      const key = aiAnalysisKey(symbol, period);
      const periodLabel = detailPeriodLabel(period);
      const aiBlock = document.getElementById("ai-block");
      aiMeta[key] = { status: "分析中", ts: Date.now() };
      if (aiBlock && selectedSymbol === symbol && detailPeriod() === period) {
        aiScrollBySymbol[key] = aiBlock.scrollTop;
        refreshAIBlock(symbol);
      }
      aiRequestedAt[key] = Date.now();
      try {
        const url = `/api/ai/analysis?symbol=${encodeURIComponent(symbol)}&period=${encodeURIComponent(period)}${force ? "&force=1" : ""}`;
        const response = await apiFetch(url, { cache: "no-store" });
        const data = await response.json();
        if (data.analysis) {
          saveAIResult(symbol, data.analysis, period);
          aiMeta[key] = {
            status: data.cached ? "使用缓存" : "已更新",
            ts: Date.now()
          };
          if (selectedSymbol === symbol && detailPeriod() === period) refreshAIBlock(symbol);
          updatedEl.textContent = data.cached ? `AI ${periodLabel} 分析使用缓存` : `AI ${periodLabel} 分析已更新`;
        } else {
          const reason = data.reason || "暂无分析";
          const triggerText = aiTriggerStatusText(data.trigger);
          const status = triggerText ? `${aiReasonText(reason)} · ${triggerText}` : aiReasonText(reason);
          aiMeta[key] = { status, ts: Date.now() };
          if (selectedSymbol === symbol && detailPeriod() === period) refreshAIBlock(symbol);
          updatedEl.textContent = `AI ${periodLabel} ${status}`;
          return;
        }
      } catch (error) {
        aiMeta[key] = { status: "AI 请求失败", ts: Date.now() };
        if (selectedSymbol === symbol && detailPeriod() === period) {
          refreshAIBlock(symbol);
        }
        updatedEl.textContent = `AI ${periodLabel} 请求失败`;
      }
    }

    function maybeLoadAIAnalysis(symbol) {
      if (!symbol) return;
      const period = detailPeriod();
      const key = aiAnalysisKey(symbol.symbol, period);
      if (aiResults[key]) return;
      const lastRequested = aiRequestedAt[key] || 0;
      if (Date.now() - lastRequested < 60000) return;
      fetchAIAnalysis(symbol.symbol, false, period);
    }

    function timeframeKey(symbol, period = detailPeriod()) {
      return `${String(symbol || "").toUpperCase()}::${period}`;
    }

    function cachedTimeframeAnalysis(symbol, period = detailPeriod()) {
      return timeframeAnalysisCache[timeframeKey(symbol, period)] || null;
    }

    function confluenceKey(symbol) {
      return `${String(symbol || "").toUpperCase()}::multi`;
    }

    function cachedTimeframeConfluence(symbol) {
      return timeframeConfluenceCache[confluenceKey(symbol)] || null;
    }

    async function fetchTimeframeConfluence(symbol, force = false) {
      if (!symbol) return;
      const key = confluenceKey(symbol);
      if (!force && timeframeConfluenceMeta[key] && timeframeConfluenceMeta[key].loading) return;
      timeframeConfluenceMeta[key] = { loading: true, error: "", ts: Date.now() };
      timeframeConfluenceRequestedAt[key] = Date.now();
      try {
        const url = `/api/timeframe/confluence?symbol=${encodeURIComponent(symbol)}${force ? "&force=1" : ""}`;
        const response = await apiFetch(url, { cache: "no-store" });
        const data = await response.json();
        if (!response.ok || !data.ok || !data.analysis) {
          throw new Error(data.error || "多周期共振失败");
        }
        timeframeConfluenceCache[key] = data.analysis;
        timeframeConfluenceMeta[key] = { loading: false, error: "", ts: Date.now() };
      } catch (error) {
        timeframeConfluenceMeta[key] = {
          loading: false,
          error: error && error.message ? error.message : "多周期共振失败",
          ts: Date.now()
        };
      }
      if (selectedSymbol === symbol) renderDetail(lastSymbols);
    }

    function maybeLoadTimeframeConfluence(symbol, force = false) {
      if (!symbol) return;
      const key = confluenceKey(symbol);
      const cached = timeframeConfluenceCache[key];
      const lastRequested = timeframeConfluenceRequestedAt[key] || 0;
      if (!force && cached && Date.now() - Number(cached.generated_at || 0) * 1000 < 30000) return;
      if (!force && Date.now() - lastRequested < 6000) return;
      fetchTimeframeConfluence(symbol, force);
    }

    async function fetchTimeframeAnalysis(symbol, period = detailPeriod(), force = false) {
      if (!symbol) return;
      const key = timeframeKey(symbol, period);
      if (!force && timeframeMeta[key] && timeframeMeta[key].loading) return;
      timeframeMeta[key] = { loading: true, error: "", ts: Date.now() };
      timeframeRequestedAt[key] = Date.now();
      if (selectedSymbol === symbol && detailPeriod() === period) renderDetail(lastSymbols);
      try {
        const url = `/api/timeframe?symbol=${encodeURIComponent(symbol)}&period=${encodeURIComponent(period)}${force ? "&force=1" : ""}`;
        const response = await apiFetch(url, { cache: "no-store" });
        const data = await response.json();
        if (!response.ok || !data.ok || !data.analysis) {
          throw new Error(data.error || "周期分析失败");
        }
        timeframeAnalysisCache[key] = data.analysis;
        timeframeMeta[key] = { loading: false, error: "", ts: Date.now() };
      } catch (error) {
        timeframeMeta[key] = {
          loading: false,
          error: error && error.message ? error.message : "周期分析失败",
          ts: Date.now()
        };
      }
      if (selectedSymbol === symbol && detailPeriod() === period) renderDetail(lastSymbols);
    }

    function maybeLoadTimeframeAnalysis(symbol, force = false) {
      if (!symbol) return;
      const period = detailPeriod();
      if (period === "realtime") return;
      const key = timeframeKey(symbol, period);
      const cached = timeframeAnalysisCache[key];
      const lastRequested = timeframeRequestedAt[key] || 0;
      if (!force && cached && Date.now() - Number(cached.generated_at || 0) * 1000 < 30000) return;
      if (!force && Date.now() - lastRequested < 4000) return;
      fetchTimeframeAnalysis(symbol, period, force);
    }

    function periodButton(period) {
      return `<button class="period-btn ${detailPeriod() === period ? "active" : ""}" data-detail-period="${esc(period)}" type="button">${esc(detailPeriodLabel(period))}</button>`;
    }

    function renderPeriodFloatingSwitch() {
      return `
        <div class="period-floating">
          <div class="period-floating-title">周期</div>
          <div class="period-switch">
            ${DETAIL_PERIOD_OPTIONS.map((item) => periodButton(item)).join("")}
          </div>
        </div>
      `;
    }

    function timeframeStatusText(meta, data) {
      if (meta && meta.loading) return "正在拉取交易所原生 K 线...";
      if (meta && meta.error) return meta.error;
      if (!data) return "等待加载周期分析";
      const source = String(data.exchange || "").startsWith("okx") ? "OKX" : "Binance";
      const stamp = data.generated_at ? new Date(Number(data.generated_at) * 1000).toLocaleTimeString() : "";
      const forming = data.candle_confirmed ? "已收线" : "进行中";
      return `${source} ${data.period_label || detailPeriod()} K 线 · ${forming}${stamp ? ` · ${stamp}` : ""}`;
    }

    function timeframeNarrative(data) {
      if (!data) return "选择周期后会基于交易所原生 K 线生成当前区间分析。";
      const state = structureState(data);
      const move = Number(data.price_move_pct || 0);
      const volume = Number(data.volume_multiplier || 0);
      const support = fmtPriceLevel(data.support_price);
      const resistance = fmtPriceLevel(data.resistance_price);
      const poc = fmtPriceLevel(data.profile_poc_price);
      const valueLow = fmtPriceLevel(data.value_area_low);
      const valueHigh = fmtPriceLevel(data.value_area_high);
      if (state === "支撑失守") {
        return `${data.period_label} 支撑 ${support} 已被跌破，若无法快速收回价值区 ${valueLow} - ${valueHigh}，先按破位后的反抽压力处理。`;
      }
      if (state === "压力突破") {
        return `${data.period_label} 压力 ${resistance} 已被突破，重点看价格能否在 POC ${poc} 上方继续被市场接受。`;
      }
      if (state === "测试支撑") {
        return `当前正在测试 ${data.period_label} 支撑 ${support}，更有价值的确认是缩量不破，或快速收回 POC ${poc}。`;
      }
      if (state === "测试压力") {
        return `当前正在测试 ${data.period_label} 压力 ${resistance}，若没有放量站上，容易先回到价值区 ${valueLow} - ${valueHigh} 内轮动。`;
      }
      if (state === "价值区轮动") {
        return `价格仍在价值区 ${valueLow} - ${valueHigh} 内轮动，优先按均值回归处理，等待离开价值区后的接受/拒绝信号。`;
      }
      if (move >= 1 && volume >= 1.5) {
        return `${data.period_label} 周期里价格正向推动明显，且量能高于近邻均值；短线重点看 ${resistance} 一带能否被继续放量站上。`;
      }
      if (move <= -1 && volume >= 1.5) {
        return `${data.period_label} 周期里下行动能偏强，且放量配合明显；除非快速收回 VWAP，否则更像压力主导。`;
      }
      if (state === "贴近支撑") {
        return `当前更贴近 ${data.period_label} 支撑 ${support}，若后续继续缩量不破，反抽观察价值会更高。`;
      }
      if (state === "贴近压力") {
        return `当前更贴近 ${data.period_label} 压力 ${resistance}，若无进一步放量，先防冲高回落。`;
      }
      return `${data.period_label} 周期暂时更像区间内波动，先观察价格是回归 VWAP，还是继续向 ${resistance} / ${support} 其中一侧扩张。`;
    }

    function renderTimeframeSection(symbol) {
      const period = detailPeriod();
      const header = `
        <div class="period-head">
          <div>
            <div class="detail-title">周期视图</div>
            <div class="period-meta">交易所原生 K 线、成交额与标记价走势。</div>
          </div>
        </div>
      `;
      if (period === "realtime") {
        const updatedAt = Number(symbol.updated_at || 0);
        const stamp = updatedAt > 0
          ? new Date(updatedAt > 1e12 ? updatedAt : updatedAt * 1000).toLocaleTimeString()
          : "";
        const realtimeStatus = hasCoreTradeData(symbol)
          ? `实时成交流 / OI / 盘口补充${stamp ? ` · ${stamp}` : ""}`
          : "等待实时成交与盘口稳定";
        return `
          <section class="period-section">
            ${header}
            <div class="period-status">${esc(realtimeStatus)}</div>
            ${renderRealtimeSupplement(symbol)}
          </section>
        `;
      }
      const key = timeframeKey(symbol.symbol, period);
      const meta = timeframeMeta[key] || null;
      const data = cachedTimeframeAnalysis(symbol.symbol, period);
      const confluence = cachedTimeframeConfluence(symbol.symbol);
      maybeLoadTimeframeAnalysis(symbol.symbol, false);
      maybeLoadTimeframeConfluence(symbol.symbol, false);
      const statusText = timeframeStatusText(meta, data);
      const statusClass = meta && meta.error ? "period-status error" : "period-status";
      if (!data) {
        return `
          <section class="period-section">
            ${header}
            <div class="${statusClass}">${esc(statusText)}</div>
            <div class="detail-placeholder">当前还没有拿到 ${esc(period)} 周期分析，稍等片刻或点右上时间按钮重试。</div>
          </section>
        `;
      }
      const priceTone = valueClass(data.price_move_pct);
      const markTone = valueClass(data.mark_move_pct);
      const priceChart = lineChartSvg(data.price_series || [], priceTone);
      const volumeChart = barChartSvg(data.volume_series || [], Number(data.volume_multiplier || 0) >= 1 ? "blue" : "mixed");
      const markChart = (data.mark_price_series || []).length >= 2
        ? lineChartSvg(data.mark_price_series, markTone)
        : chartEmptyHtml("标记价序列不足");
      return `
        <section class="period-section">
          ${header}
          <div class="${statusClass}">
            ${esc(statusText)}
            <button class="inline-link" id="period-refresh-btn" type="button" style="margin-left:8px">刷新</button>
          </div>
          <div class="period-summary">
            <div class="period-pill">
              <div class="period-pill-label">${esc(data.period_label)} 涨跌</div>
              <div class="period-pill-value ${priceTone}">${fmtPctMaybe(data.price_move_pct, true)}</div>
            </div>
            <div class="period-pill">
              <div class="period-pill-label">结构状态</div>
              <div class="period-pill-value ${structureStateClass(structureState(data))}">${esc(structureState(data))}</div>
            </div>
            <div class="period-pill">
              <div class="period-pill-label">量能 / 均值</div>
              <div class="period-pill-value ${Number(data.volume_multiplier || 0) >= 1.5 ? "up" : "muted"}">${fmtNumber(data.volume_multiplier, 2)}x</div>
            </div>
            <div class="period-pill">
              <div class="period-pill-label">标记价偏离</div>
              <div class="period-pill-value ${valueClass(data.mark_premium_bps)}">${data.mark_premium_bps === null || data.mark_premium_bps === undefined ? "--" : `${fmtSignedNumber(data.mark_premium_bps, 1)} bps`}</div>
            </div>
            <div class="period-pill">
              <div class="period-pill-label">多周期共振</div>
              <div class="period-pill-value ${confluenceTone(confluence)}">${confluence ? fmtNumber(confluence.score, 1) : "--"}</div>
            </div>
          </div>
          <div class="visual-grid">
            ${chartCard(`${data.period_label} 价格轨迹`, fmtPctMaybe(data.price_move_pct, true), priceChart, "span-2")}
            ${chartCard(`${data.period_label} 成交额`, `${fmtNumber(data.quote_volume, 0)} USDT`, volumeChart)}
            ${chartCard(`${data.period_label} 标记价`, data.mark_move_pct === null || data.mark_move_pct === undefined ? mutedValue() : fmtPctMaybe(data.mark_move_pct, true), markChart)}
          </div>
        </section>
      `;
    }

    detailEl.addEventListener("click", (event) => {
      const tabButton = event.target.closest("[data-detail-tab]");
      if (tabButton) {
        captureDetailScroll(selectedSymbol);
        setDetailTab(tabButton.dataset.detailTab);
        renderDetail(lastSymbols);
        return;
      }
      const periodButton = event.target.closest("[data-detail-period]");
      if (periodButton && selectedSymbol) {
        setDetailPeriod(periodButton.dataset.detailPeriod);
        maybeLoadTimeframeAnalysis(selectedSymbol, false);
        renderDetail(lastSymbols);
        return;
      }
      if (event.target.id === "ai-refresh-btn" && selectedSymbol) {
        fetchAIAnalysis(selectedSymbol, true);
        return;
      }
      if (event.target.id === "ai-copy-btn" && selectedSymbol) {
        copyAIOpinions(selectedSymbol);
        return;
      }
      if (event.target.id === "period-refresh-btn" && selectedSymbol) {
        fetchTimeframeAnalysis(selectedSymbol, detailPeriod(), true);
        fetchTimeframeConfluence(selectedSymbol, true);
      }
    });
    detailEl.addEventListener("scroll", (event) => {
      if (event.target && event.target.id === "ai-block" && selectedSymbol) {
        aiScrollBySymbol[aiAnalysisKey(selectedSymbol)] = event.target.scrollTop;
        noteAIInteraction();
      }
    }, true);
    detailEl.addEventListener("wheel", (event) => {
      if (event.target && event.target.closest && event.target.closest("#ai-block") && selectedSymbol) {
        noteAIInteraction();
      }
    }, { passive: true });
    detailEl.addEventListener("touchmove", (event) => {
      if (event.target && event.target.closest && event.target.closest("#ai-block") && selectedSymbol) {
        noteAIInteraction();
      }
    }, { passive: true });
    if (sideScrollEl) {
      sideScrollEl.addEventListener("scroll", () => {
        if (selectedSymbol) drawerScrollBySymbol[selectedSymbol] = sideScrollEl.scrollTop;
      });
    }
    detailCloseBtn.addEventListener("click", () => closeDetailDrawer(true));
    detailDrawerBackdropEl.addEventListener("click", () => closeDetailDrawer(true));

    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-collapse]");
      if (!button) return;
      const section = button.dataset.collapse;
      if (!section) return;
      const collapsed = !isCollapsed(section);
      setCollapsed(section, collapsed);
      updateCollapseButton(button, collapsed);
      const block = button.closest(".collapsible");
      if (block) block.classList.toggle("collapsed", collapsed);
      if (section === "events") applyEventsCollapseState();
    });

    document.getElementById("profile-open-telegram").addEventListener("click", async () => {
      closeModal(profileModal);
      await loadTelegramConfig();
      openModal(telegramModal);
    });
    document.getElementById("profile-open-ai").addEventListener("click", async () => {
      closeModal(profileModal);
      await loadAIConfig();
      openModal(aiModal);
    });
    document.getElementById("profile-logout").addEventListener("click", () => {
      closeModal(profileModal);
      logout();
    });

    [
      ["close-profile-modal", profileModal],
      ["close-profile-btn", profileModal],
      ["close-telegram-modal", telegramModal],
      ["close-telegram-btn", telegramModal],
      ["close-ai-modal", aiModal],
      ["close-ai-btn", aiModal],
      ["close-signals-modal", signalsModal],
      ["close-signals-btn", signalsModal],
      ["close-threshold-modal", thresholdModal],
      ["close-threshold-btn", thresholdModal]
    ].forEach(([id, modal]) => {
      document.getElementById(id).addEventListener("click", () => closeModal(modal));
    });
    [profileModal, telegramModal, aiModal, signalsModal, thresholdModal].forEach((modal) => {
      modal.addEventListener("click", (event) => {
        if (event.target === modal) closeModal(modal);
      });
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeModal(profileModal);
        closeModal(telegramModal);
        closeModal(aiModal);
        closeModal(signalsModal);
        closeModal(thresholdModal);
        closeDetailDrawer(true);
      }
    });

    bootstrap();
