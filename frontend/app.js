const state = {
  configs: [],
  configId: null,
  config: null,
  configHash: null,
  library: null,
  yaml: "",
  scan: null,
  assetCategory: "pre_roll",
  preview: null,
  jobs: [],
  sliceJobs: [],
  activeJob: null,
  eventSource: null,
  sliceEventSource: null,
  configMode: "simple",
  configDraft: null,
  configRefreshPromise: null,
  previewAudioCleanup: null,
  feishuSettings: { app_id: "", app_secret_configured: false },
  feishuSettingsDraft: { app_id: "", app_secret_configured: false },
  feishuSecretDraft: "",
  feishuConnection: null,
  feishuConnectionError: null,
  feishuSync: null,
  feishuSyncTimer: null,
  user: null,
  device: null,
  configLease: null,
  timeline: {
    sourceDirectory: "",
    sourceVideos: [],
    sourceIndex: -1,
    sourceLoading: false,
    sourcePickerOpen: false,
    sourceFilter: "",
    analysis: null,
    breakpoints: [],
    machineBreakpoints: [],
    audioLocked: true,
    selectedFrame: null,
    selectedFrames: [],
    playheadFrame: 0,
    dragging: false,
    drag: null,
    marquee: null,
    snapTargetFrame: null,
    suppressClickUntil: 0,
    videoFrameCallbackId: null,
    pixelsPerSecond: 30,
    shiftPressed: false,
    dragAnimationFrameId: null,
    reviewSaved: false,
    reviewRevision: null,
    selectedSegmentId: null,
    sliceUnits: [],
    sliceRequestId: null,
    sliceRequestFingerprint: null,
    mergeSelection: [],
    undoStack: [],
    redoStack: [],
    sliceTargets: [],
    waveform: {
      controller: null,
      requestKey: null,
      dataKey: null,
      data: null,
      animationFrameId: null,
    },
  },
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const categoryNames = {
  pre_roll: "前贴",
  hook: "引子",
  ending: "结尾",
  end_card: "尾帧",
  benefit_overlay: "风险提示语图片",
  visual_border: "视觉去重边框",
};
const benefitCategoryPattern = /^benefit_([1-9][0-9]*)$/;
function isBenefitCategory(category) { return benefitCategoryPattern.test(category); }
function categoryLabel(category) {
  const match = category.match(benefitCategoryPattern);
  const draftGroup = state.configDraft?.id === state.configId
    ? state.configDraft.sources?.[category]
    : null;
  const group = draftGroup || state.config?.sources?.[category];
  return group?.label?.trim() || (match ? `利益点 ${match[1]}` : (categoryNames[category] || category));
}
const terminalStates = new Set(["completed", "partial_failed", "failed", "cancelled", "interrupted"]);
const configUiStoragePrefix = "smartstitch.config-ui.";
const browserSessionStorageKey = "smartstitch.browser_session_id";
const pathQuotePairs = { "'": "'", '"': '"', "‘": "’", "“": "”" };

function normalizePathInput(value) {
  const normalized = String(value ?? "").trim();
  if (normalized.length >= 2 && pathQuotePairs[normalized[0]] === normalized.at(-1)) {
    return normalized.slice(1, -1);
  }
  return normalized;
}

function normalizePathField(input) {
  const normalized = normalizePathInput(input.value);
  if (input.value !== normalized) input.value = normalized;
  return normalized;
}

function libraryTargetPath(parentDirectory, folderName) {
  const parent = normalizePathInput(parentDirectory).replace(/[\\/]+$/, "");
  const folder = String(folderName ?? "").trim();
  if (!parent || !folder) return "";
  const parentName = parent.split(/[\\/]/).pop() || "";
  if (parentName.toLocaleLowerCase() === folder.toLocaleLowerCase()) return parent;
  const separator = parent.includes("\\") && !parent.includes("/") ? "\\" : "/";
  return `${parent}${separator}${folder}`;
}

function clientRequestId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}-${Math.random().toString(16).slice(2)}`;
}

function browserSessionId() {
  let value = "";
  try { value = sessionStorage.getItem(browserSessionStorageKey) || ""; } catch (_) {}
  if (value) return value;
  value = clientRequestId();
  try { sessionStorage.setItem(browserSessionStorageKey, value); } catch (_) {}
  return value;
}

async function api(path, options = {}) {
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    let payloadDetail = null;
    try {
      const payload = await response.json();
      if (Array.isArray(payload.detail)) {
        detail = payload.detail.map(item => `${item.loc?.slice(1).join(".") || "配置"}: ${item.msg}`).join("；");
      } else if (payload.detail && typeof payload.detail === "object") {
        payloadDetail = payload.detail;
        detail = payload.detail.message || detail;
      } else {
        detail = payload.detail || detail;
      }
    } catch (_) {}
    const error = new Error(detail);
    error.status = response.status;
    error.detail = payloadDetail;
    throw error;
  }
  return response.json();
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove("show"), 3000);
}

function renderCurrentUser() {
  const name = state.user?.display_name || "设置用户名";
  const device = state.device?.device_name || "用于配置协作锁";
  $("#userProfileName").textContent = name;
  $("#userProfileBtn .user-avatar").textContent = state.user?.display_name?.trim()?.[0] || "?";
  $("#userDeviceName").textContent = device;
}

async function loadCurrentUser() {
  try {
    const result = await api("/users/me");
    state.user = result.user;
    state.device = result.device;
    renderCurrentUser();
    if (!result.configured) openUserProfile(true);
  } catch (error) {
    toast(error.message, true);
  }
}

function openUserProfile(required = false) {
  $("#userProfileModal").dataset.required = required ? "true" : "false";
  $("#userDisplayNameInput").value = state.user?.display_name || "";
  $("#switchUserProfileBtn").classList.toggle("hidden", !state.user);
  $$('[data-close-user-profile]').forEach(element => element.classList.toggle("hidden", required));
  $("#userProfileModal").classList.add("open");
  $("#userProfileModal").setAttribute("aria-hidden", "false");
  requestAnimationFrame(() => $("#userDisplayNameInput").focus());
}

function closeUserProfile() {
  if ($("#userProfileModal").dataset.required === "true" && !state.user) {
    toast("请先输入用户名", true);
    return;
  }
  $("#userProfileModal").classList.remove("open");
  $("#userProfileModal").setAttribute("aria-hidden", "true");
}

async function saveUserProfile(switchUser) {
  if (state.configLease) {
    toast("请先保存或关闭正在编辑的配置", true);
    return;
  }
  const name = $("#userDisplayNameInput").value.trim();
  if (!name) {
    toast("请输入用户名", true);
    $("#userDisplayNameInput").focus();
    return;
  }
  if (switchUser && state.user && !window.confirm(`切换为新用户“${name}”？`)) return;
  const button = switchUser ? $("#switchUserProfileBtn") : $("#saveUserProfileBtn");
  button.disabled = true;
  try {
    const result = await api("/users/me", {
      method: "PUT",
      body: JSON.stringify({ display_name: name, switch_user: switchUser }),
    });
    state.user = result.user;
    state.device = result.device;
    renderCurrentUser();
    $("#userProfileModal").dataset.required = "false";
    closeUserProfile();
    toast(switchUser ? `已切换为 ${name}` : `用户名已保存为 ${name}`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function ensureCurrentUser() {
  if (state.user) return true;
  openUserProfile(true);
  toast("请先输入用户名", true);
  return false;
}

function configLeaseHeaders(lease = state.configLease, configHash = state.configHash) {
  if (!lease?.leaseToken) throw new Error("没有配置编辑权，请重新打开配置");
  if (lease.lost) throw new Error("配置编辑权已失效，请关闭后重新打开配置");
  return {
    "X-SmartStitch-Lease": lease.leaseToken,
    "X-SmartStitch-Config-Hash": configHash || "",
  };
}

function renderConfigLeaseBanner(lost = false, message = "") {
  const banner = $("#configLeaseBanner");
  if (!banner) return;
  const owner = state.configLease?.owner;
  if (state.configLease) state.configLease.lost = lost;
  banner.classList.toggle("lost", lost);
  banner.querySelector("strong").textContent = message || (lost ? "配置编辑权已失效" : "你正在独占编辑此配置");
  banner.querySelector("small").textContent = owner ? `${owner.display_name} · ${owner.device_name}` : "";
  $("#saveConfigBtn").disabled = lost;
}

function clearConfigLeaseTimers(lease) {
  if (!lease) return;
  if (lease.heartbeatTimer) clearInterval(lease.heartbeatTimer);
  if (lease.expiryTimer) clearTimeout(lease.expiryTimer);
  lease.heartbeatTimer = null;
  lease.expiryTimer = null;
}

function scheduleConfigLeaseExpiry(lease) {
  if (!lease || state.configLease !== lease) return;
  if (lease.expiryTimer) clearTimeout(lease.expiryTimer);
  const configuredSeconds = Number(lease.owner?.lease_seconds);
  const leaseSeconds = Number.isFinite(configuredSeconds) && configuredSeconds > 0
    ? configuredSeconds
    : 120;
  lease.expiresAt = Date.now() + leaseSeconds * 1000;
  lease.expiryTimer = setTimeout(() => expireConfigLease(lease), leaseSeconds * 1000);
}

function isTerminalConfigLeaseError(error) {
  if (error?.status !== 423 || error.detail?.code !== "lease_invalid") return false;
  return /已过期|已属于其他会话|不存在或无法读取|不是配置锁持有者/.test(error.message || "");
}

async function expireConfigLease(lease = state.configLease) {
  if (!lease || state.configLease !== lease || lease.expirationHandled) return false;
  lease.expirationHandled = true;
  lease.lost = true;
  clearConfigLeaseTimers(lease);
  state.configLease = null;
  await closeConfig({ skipConfirm: true, releaseLease: false });
  state.configDraft = state.config ? structuredClone(state.config) : null;
  if ($("#yamlEditor")) $("#yamlEditor").value = state.yaml;
  window.alert("配置编辑已超时过期，配置未保存");
  return true;
}

async function acquireConfigLease(configId) {
  if (!ensureCurrentUser()) return null;
  const payload = { browser_session_id: browserSessionId() };
  try {
    return await api(`/configs/${configId}/lock/acquire`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  } catch (error) {
    const lock = error.detail;
    if (error.status === 423 && lock?.code === "config_locked" && lock.stale) {
      const owner = lock.owner;
      const label = owner?.display_name ? `${owner.display_name}（${owner.device_name || "未知电脑"}）` : "上一位用户";
      if (window.confirm(`${label}的编辑锁已经过期，是否接管此配置？`)) {
        return api(`/configs/${configId}/lock/takeover`, {
          method: "POST",
          body: JSON.stringify(payload),
        });
      }
      return null;
    }
    if (error.status === 423 && lock?.code === "config_locked") {
      window.alert(lock.message || error.message);
      return null;
    }
    throw error;
  }
}

function installConfigLease(configId, acquired) {
  clearConfigLeaseTimers(state.configLease);
  state.configLease = {
    configId,
    leaseToken: acquired.lease_token,
    browserSessionId: browserSessionId(),
    owner: acquired.owner,
    heartbeatFailures: 0,
    lost: false,
    heartbeatTimer: null,
    expiryTimer: null,
    expiresAt: null,
    expirationHandled: false,
  };
  state.configLease.heartbeatTimer = setInterval(renewConfigLease, 15000);
  scheduleConfigLeaseExpiry(state.configLease);
  renderConfigLeaseBanner(false);
}

async function renewConfigLease() {
  const lease = state.configLease;
  if (!lease) return;
  try {
    const renewed = await api(`/configs/${lease.configId}/lock/renew`, {
      method: "POST",
      body: JSON.stringify({
        lease_token: lease.leaseToken,
        browser_session_id: lease.browserSessionId,
      }),
    });
    if (state.configLease !== lease) return;
    lease.owner = renewed.owner;
    lease.heartbeatFailures = 0;
    scheduleConfigLeaseExpiry(lease);
    renderConfigLeaseBanner(false);
  } catch (error) {
    if (state.configLease !== lease) return;
    if (isTerminalConfigLeaseError(error)) {
      await expireConfigLease(lease);
      return;
    }
    lease.heartbeatFailures += 1;
    const lost = error.status === 423 || lease.heartbeatFailures >= 3;
    renderConfigLeaseBanner(lost, lost ? "无法续租，已停止保存" : "NAS 连接不稳定，正在重试续租");
    if (lost) toast(error.message, true);
  }
}

async function releaseConfigLease(lease = state.configLease, { silent = false } = {}) {
  if (!lease) return;
  clearConfigLeaseTimers(lease);
  if (state.configLease === lease) state.configLease = null;
  try {
    await api(`/configs/${lease.configId}/lock/release`, {
      method: "POST",
      body: JSON.stringify({
        lease_token: lease.leaseToken,
        browser_session_id: lease.browserSessionId,
      }),
    });
  } catch (error) {
    if (!silent) toast(`${error.message}；锁将在约 2 分钟后自动过期`, true);
  }
}

function releaseConfigLeaseBeacon() {
  const lease = state.configLease;
  if (!lease || !navigator.sendBeacon) return;
  const body = new Blob([JSON.stringify({
    lease_token: lease.leaseToken,
    browser_session_id: lease.browserSessionId,
  })], { type: "application/json" });
  navigator.sendBeacon(`/api/v1/configs/${lease.configId}/lock/release`, body);
}

async function withTemporaryConfigLease(callback, { requireCurrentHash = true } = {}) {
  if (!state.configId) return null;
  const existing = state.configLease?.configId === state.configId ? state.configLease : null;
  if (existing) return callback(existing, state.configHash);
  const acquired = await acquireConfigLease(state.configId);
  if (!acquired) return null;
  const lease = {
    configId: state.configId,
    leaseToken: acquired.lease_token,
    browserSessionId: browserSessionId(),
    owner: acquired.owner,
  };
  try {
    if (requireCurrentHash && acquired.content_hash !== state.configHash) {
      await selectConfig(state.configId);
      throw new Error("配置已被同事修改，页面已刷新，请重新操作");
    }
    return await callback(lease, acquired.content_hash);
  } finally {
    await releaseConfigLease(lease, { silent: true });
  }
}

function statusInfo(status) {
  const map = {
    draft: ["草稿", "neutral"], queued: ["排队中", "running"], running: ["生成中", "running"],
    completed: ["已完成", "success"], partial_failed: ["部分失败", "danger"], failed: ["失败", "danger"],
    cancelling: ["取消中", "running"], cancelled: ["已取消", "neutral"], interrupted: ["已中断", "danger"],
    pending: ["等待", "neutral"], succeeded: ["成功", "success"],
  };
  return map[status] || [status, "neutral"];
}

async function init() {
  bindEvents();
  try {
    const health = await api("/system/health");
    $("#healthDot").classList.add("ok");
    $("#healthText").textContent = health.ffmpeg ? `FFmpeg 已就绪 · v${health.version}` : "FFmpeg 未找到";
  } catch (error) { $("#healthText").textContent = "后端连接失败"; }
  await loadCurrentUser();
  await loadConfigs();
  await Promise.all([loadJobs(), loadSliceJobs()]);
  connectSliceJobEvents();
}

function bindEvents() {
  document.addEventListener("paste", event => {
    const input = event.target.closest?.("[data-path-input]");
    if (!input) return;
    const pasted = event.clipboardData?.getData("text");
    if (typeof pasted !== "string") return;
    const normalized = normalizePathInput(pasted);
    if (normalized === pasted) return;
    event.preventDefault();
    input.setRangeText(normalized, input.selectionStart ?? 0, input.selectionEnd ?? input.value.length, "end");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  document.addEventListener("change", event => {
    const input = event.target.closest?.("[data-path-input]");
    if (input) normalizePathField(input);
  });
  $$(".section-tab").forEach(button => button.addEventListener("click", () => switchView(button.dataset.view)));
  $("#configSelect").addEventListener("change", () => selectConfig($("#configSelect").value));
  $("#scanBtn").addEventListener("click", scanAssets);
  $("#saveWeightsBtn").addEventListener("click", saveWeights);
  $("#previewBtn").addEventListener("click", previewPlan);
  $("#startBtn").addEventListener("click", startJob);
  $("#refreshJobsBtn").addEventListener("click", loadJobs);
  $("#deleteAllJobsBtn").addEventListener("click", showDeleteAllJobsConfirm);
  $("#cancelDeleteAllJobsBtn").addEventListener("click", hideDeleteAllJobsConfirm);
  $("#confirmDeleteAllJobsBtn").addEventListener("click", deleteAllJobRecords);
  $("#newConfigBtn").addEventListener("click", openNewConfig);
  $("#createConfigBtn").addEventListener("click", createConfig);
  $("#newWorkflowType").addEventListener("change", updateLibraryCreatePreview);
  $("#chooseLibraryParentBtn").addEventListener("click", chooseLibraryParent);
  $("#newConfigIdInput").addEventListener("input", updateLibraryCreatePreview);
  $("#newConfigNameInput").addEventListener("input", () => {
    const folder = $("#newLibraryFolderInput");
    if (folder.dataset.automatic !== "false") folder.value = $("#newConfigNameInput").value;
    updateLibraryCreatePreview();
  });
  $("#newLibraryFolderInput").addEventListener("input", event => {
    event.target.dataset.automatic = "false";
    updateLibraryCreatePreview();
  });
  $("#newLibraryParentInput").addEventListener("input", updateLibraryCreatePreview);
  $("#deleteConfigBtn").addEventListener("click", showDeleteConfigConfirm);
  $("#cancelDeleteConfigBtn").addEventListener("click", hideDeleteConfigConfirm);
  $("#confirmDeleteConfigBtn").addEventListener("click", deleteConfig);
  $("#cloneConfigBtn").addEventListener("click", cloneConfig);
  $("#editConfigBtn").addEventListener("click", openConfig);
  $("#saveConfigBtn").addEventListener("click", saveConfig);
  $("#userProfileBtn").addEventListener("click", () => openUserProfile(false));
  $("#saveUserProfileBtn").addEventListener("click", () => saveUserProfile(false));
  $("#switchUserProfileBtn").addEventListener("click", () => saveUserProfile(true));
  $("#userDisplayNameInput").addEventListener("keydown", event => {
    if (event.key === "Enter") saveUserProfile(false);
  });
  $$('[data-close-user-profile]').forEach(element => element.addEventListener("click", closeUserProfile));
  $$(".config-mode-tab").forEach(button => button.addEventListener("click", () => setConfigMode(button.dataset.configMode)));
  $$('[data-close-modal]').forEach(element => element.addEventListener("click", () => closeConfig()));
  $$('[data-close-new-config]').forEach(element => element.addEventListener("click", closeNewConfig));
  $$('[data-close-drawer]').forEach(element => element.addEventListener("click", closeDrawer));
  bindTimelineEvents();
  window.addEventListener("beforeunload", releaseConfigLeaseBeacon);
}

function switchView(view) {
  $$(".section-tab").forEach(button => button.classList.toggle("active", button.dataset.view === view));
  $$(".view").forEach(element => element.classList.toggle("active", element.id === `${view}View`));
  $("#timelineSliceQueue").classList.toggle("hidden", view !== "timeline");
  if (view === "jobs") loadJobs();
  if (view === "timeline") refreshTimelineConfig();
}

function bindTimelineEvents() {
  $("#analyzeTimelineBtn").addEventListener("click", analyzeTimeline);
  $("#chooseTimelineDirectoryBtn").addEventListener("click", chooseTimelineSourceDirectory);
  $("#previousVideoBtn").addEventListener("click", () => navigateTimelineSource(-1));
  $("#nextVideoBtn").addEventListener("click", () => navigateTimelineSource(1));
  $("#timelineCurrentSourceButton").addEventListener("click", toggleTimelineSourcePicker);
  $("#timelineSourceSearchInput").addEventListener("input", event => {
    state.timeline.sourceFilter = event.target.value;
    renderTimelineSourcePickerList();
  });
  $("#timelineSourceSearchInput").addEventListener("keydown", event => {
    if (event.key !== "Enter") return;
    const [firstIndex] = filteredTimelineSourceIndexes();
    if (firstIndex === undefined) return;
    event.preventDefault();
    selectTimelineSource(firstIndex);
  });
  document.addEventListener("click", event => {
    if (!state.timeline.sourcePickerOpen || event.target.closest?.("#timelineSourceSelector")) return;
    setTimelineSourcePickerOpen(false);
  });
  $("#timelinePathInput").addEventListener("input", event => {
    const directory = normalizePathInput(event.target.value);
    if (directory === state.timeline.sourceDirectory) return;
    state.timeline.sourceDirectory = "";
    state.timeline.sourceVideos = [];
    state.timeline.sourceIndex = -1;
    setTimelineSourcePickerOpen(false);
    clearTimelineAnalysisView();
    renderTimelineSourceNavigation();
  });
  $("#previousFrameBtn").addEventListener("click", () => stepTimelineFrame(-1));
  $("#nextFrameBtn").addEventListener("click", () => stepTimelineFrame(1));
  $("#addBreakpointBtn").addEventListener("click", addBreakpointAtPlayhead);
  $("#deleteBreakpointBtn").addEventListener("click", deleteSelectedBreakpoints);
  $("#sliceTimelineBtn").addEventListener("click", exportTimelineSlices);
  $("#mergeSegmentsBtn").addEventListener("click", mergeSelectedSliceUnits);
  $("#clearMergeSelectionBtn").addEventListener("click", clearMergeSelection);
  $("#timelineAudioLockBtn").addEventListener("click", toggleTimelineAudioLock);
  $("#selectedFrameInput").addEventListener("change", event => moveSelectedBreakpoint(Number(event.target.value)));
  const video = $("#timelineVideo");
  video.addEventListener("play", startTimelineVideoSync);
  video.addEventListener("pause", syncTimelineFromVideo);
  video.addEventListener("timeupdate", syncTimelineFromVideo);
  video.addEventListener("seeking", syncTimelineFromVideo);
  video.addEventListener("seeked", syncTimelineFromVideo);
  video.addEventListener("loadedmetadata", () => {
    updateTimelineMediaLayout(video.videoWidth, video.videoHeight);
  });
  $("#timelineRulerBar").addEventListener("pointerdown", event => {
    const frame = frameFromPointer(event, 0, event.shiftKey);
    seekTimelineFrame(frame, "timeline_scrub");
    beginTimelineDrag(event, { kind: "playhead", anchorFrame: frame });
  });
  $("#timelineMarqueeLane").addEventListener("pointerdown", beginTimelineMarquee);
  const timelineViewport = $("#timelineViewport");
  timelineViewport.addEventListener("scroll", () => {
    renderTimelineRuler();
    scheduleTimelineWaveformRender();
  });
  timelineViewport.addEventListener("wheel", handleTimelineWheel, { passive: false });
  $("#timelineZoomInput").addEventListener("input", event => {
    setTimelineZoom(Number(event.target.value));
  });
  $("#fitTimelineBtn").addEventListener("click", fitTimelineToViewport);
  window.addEventListener("resize", () => {
    if (state.timeline.analysis) renderTimeline();
  });
  window.addEventListener("keydown", event => {
    if (event.key === "Shift") {
      state.timeline.shiftPressed = true;
      scheduleTimelineDragFrame();
    }
    if (!$("#timelineView").classList.contains("active")) return;
    if (event.key === "Escape" && state.timeline.sourcePickerOpen) {
      event.preventDefault();
      setTimelineSourcePickerOpen(false);
      $("#timelineCurrentSourceButton").focus();
      return;
    }
    const activeElement = document.activeElement;
    const isEditing = ["INPUT", "TEXTAREA", "SELECT"].includes(activeElement?.tagName)
      || Boolean(activeElement?.isContentEditable);
    const isTextEditing = activeElement?.tagName === "TEXTAREA"
      || Boolean(activeElement?.isContentEditable)
      || (activeElement?.tagName === "INPUT" && ![
        "button", "checkbox", "radio", "range", "color", "file", "submit", "reset",
      ].includes(activeElement.type));
    const isTimelineHistoryShortcut = (event.metaKey || event.ctrlKey)
      && !event.altKey
      && event.key.toLowerCase() === "z";
    if (isTimelineHistoryShortcut && !isTextEditing) {
      event.preventDefault();
      event.stopPropagation();
      if (!event.repeat) {
        if (event.shiftKey) redoTimelineEdit();
        else undoTimelineEdit();
      }
      return;
    }
    if (event.code === "Space" && !isEditing) {
      event.preventDefault();
      event.stopPropagation();
    }
    if (TimelineMath.shouldTogglePlaybackFromSpace({
      code: event.code,
      hasAnalysis: Boolean(state.timeline.analysis),
      isEditing,
    })) {
      if (!event.repeat) toggleTimelinePlayback();
      return;
    }
    if (isEditing) return;
    if (["Delete", "Backspace"].includes(event.key) && state.timeline.selectedFrames.length) {
      event.preventDefault();
      deleteSelectedBreakpoints();
      return;
    }
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      stepTimelineFrame(event.key === "ArrowLeft" ? -1 : 1);
    }
  }, { capture: true });
  document.addEventListener("keyup", event => {
    if (event.key !== "Shift") return;
    state.timeline.shiftPressed = false;
    scheduleTimelineDragFrame();
  });
  window.addEventListener("focus", () => {
    if ($("#timelineView").classList.contains("active")) refreshTimelineConfig();
  });
}

async function refreshTimelineConfig() {
  if (!state.configId) return false;
  if (state.configRefreshPromise) return state.configRefreshPromise;
  const configId = state.configId;
  state.configRefreshPromise = (async () => {
    try {
      const refreshed = await api(`/configs/${configId}`);
      if (state.configId !== configId || refreshed.content_hash === state.configHash) return false;
      state.config = refreshed.config;
      state.configHash = refreshed.content_hash;
      state.yaml = refreshed.yaml_text;
      try {
        state.library = await api(`/libraries/by-config/${configId}`);
        const targets = await api(`/libraries/by-config/${configId}/slice-targets`);
        state.timeline.sliceTargets = targets.targets;
      } catch (_) {
        state.library = null;
        state.timeline.sliceTargets = [];
      }
      const validCategories = new Set(timelineCategoryOptions().map(option => option.category));
      state.timeline.sliceUnits.forEach(unit => {
        if (unit.category && !validCategories.has(unit.category)) unit.category = null;
      });
      if (state.timeline.analysis) renderTimeline();
      toast("视频库配置已更新，切片类型已刷新");
      return true;
    } catch (error) {
      toast(`刷新视频库配置失败：${error.message}`, true);
      return false;
    } finally {
      state.configRefreshPromise = null;
    }
  })();
  return state.configRefreshPromise;
}

async function analyzeTimeline() {
  const sourceDirectory = normalizePathField($("#timelinePathInput"));
  if (!sourceDirectory) return toast("请先填写或选择源视频文件夹", true);
  const silenceDuration = Number($("#timelineSilenceInput").value);
  if (!Number.isFinite(silenceDuration) || silenceDuration < 0.1 || silenceDuration > 3) {
    return toast("最短语音停顿必须在 0.1–3 秒之间", true);
  }
  state.timeline.sourceLoading = true;
  renderTimelineSourceNavigation("正在读取文件夹…");
  try {
    const currentPath = state.timeline.sourceVideos[state.timeline.sourceIndex]?.path;
    await loadTimelineSourceDirectory(sourceDirectory, currentPath);
    await analyzeTimelineSource(state.timeline.sourceIndex);
  } catch (error) {
    setTimelineStatus("分析失败", "danger");
    toast(error.message, true);
  } finally {
    state.timeline.sourceLoading = false;
    renderTimelineSourceNavigation();
  }
}

async function chooseTimelineSourceDirectory() {
  const button = $("#chooseTimelineDirectoryBtn");
  button.disabled = true;
  try {
    const result = await api("/system/directory-picker", { method: "POST" });
    if (result.cancelled) return;
    $("#timelinePathInput").value = result.path;
    state.timeline.sourceLoading = true;
    renderTimelineSourceNavigation("正在读取文件夹…");
    await loadTimelineSourceDirectory(result.path);
    toast(`已找到 ${state.timeline.sourceVideos.length} 个源视频`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.timeline.sourceLoading = false;
    button.disabled = false;
    renderTimelineSourceNavigation();
  }
}

async function loadTimelineSourceDirectory(sourceDirectory, preferredPath = null) {
  const result = await api("/timeline/sources", {
    method: "POST",
    body: JSON.stringify({ source_directory: sourceDirectory }),
  });
  if (!result.videos.length) {
    state.timeline.sourceDirectory = result.source_directory;
    state.timeline.sourceVideos = [];
    state.timeline.sourceIndex = -1;
    renderTimelineSourceNavigation();
    throw new Error("这个文件夹中没有支持的视频文件");
  }
  state.timeline.sourceDirectory = result.source_directory;
  state.timeline.sourceVideos = result.videos;
  setTimelineSourcePickerOpen(false);
  const preferredIndex = preferredPath
    ? result.videos.findIndex(video => video.path === preferredPath)
    : -1;
  const analyzedIndex = state.timeline.analysis?.source_path
    ? result.videos.findIndex(video => video.path === state.timeline.analysis.source_path)
    : -1;
  if (analyzedIndex < 0) clearTimelineAnalysisView();
  state.timeline.sourceIndex = preferredIndex >= 0
    ? preferredIndex
    : analyzedIndex >= 0 ? analyzedIndex : 0;
  $("#timelinePathInput").value = result.source_directory;
  renderTimelineSourceNavigation();
}

function clearTimelineAnalysisView() {
  if (!state.timeline.analysis) return;
  stopTimelineVideoSync();
  resetTimelineWaveform();
  state.timeline.analysis = null;
  state.timeline.breakpoints = [];
  state.timeline.machineBreakpoints = [];
  state.timeline.selectedFrame = null;
  state.timeline.selectedFrames = [];
  state.timeline.playheadFrame = 0;
  state.timeline.reviewSaved = false;
  state.timeline.reviewRevision = null;
  state.timeline.selectedSegmentId = null;
  state.timeline.sliceUnits = [];
  state.timeline.mergeSelection = [];
  resetTimelineHistory();
  const video = $("#timelineVideo");
  video.pause();
  video.removeAttribute("src");
  video.load();
  $("#timelineWorkspace").classList.add("hidden");
  $("#timelineEmpty").classList.remove("hidden");
  setTimelineStatus("等待视频", "neutral");
}

async function navigateTimelineSource(delta) {
  if (state.timeline.sourceLoading) return;
  const targetIndex = state.timeline.sourceIndex + delta;
  await selectTimelineSource(targetIndex);
}

async function selectTimelineSource(targetIndex) {
  setTimelineSourcePickerOpen(false);
  if (state.timeline.sourceLoading) return;
  if (targetIndex < 0 || targetIndex >= state.timeline.sourceVideos.length) return;
  const target = state.timeline.sourceVideos[targetIndex];
  if (targetIndex === state.timeline.sourceIndex && state.timeline.analysis?.source_path === target.path) {
    return;
  }
  const previousIndex = state.timeline.sourceIndex;
  state.timeline.sourceIndex = targetIndex;
  state.timeline.sourceLoading = true;
  renderTimelineSourceNavigation("正在分析画面与语音…");
  try {
    await analyzeTimelineSource(targetIndex);
  } catch (error) {
    state.timeline.sourceIndex = previousIndex;
    setTimelineStatus("分析失败", "danger");
    toast(error.message, true);
  } finally {
    state.timeline.sourceLoading = false;
    renderTimelineSourceNavigation();
  }
}

function toggleTimelineSourcePicker() {
  if (state.timeline.sourceLoading || !state.timeline.sourceVideos.length) return;
  setTimelineSourcePickerOpen(!state.timeline.sourcePickerOpen);
}

function setTimelineSourcePickerOpen(open) {
  state.timeline.sourcePickerOpen = Boolean(
    open && !state.timeline.sourceLoading && state.timeline.sourceVideos.length,
  );
  const picker = $("#timelineSourcePicker");
  const button = $("#timelineCurrentSourceButton");
  if (!picker || !button) return;
  picker.classList.toggle("hidden", !state.timeline.sourcePickerOpen);
  button.setAttribute("aria-expanded", String(state.timeline.sourcePickerOpen));
  if (!state.timeline.sourcePickerOpen) return;
  state.timeline.sourceFilter = "";
  $("#timelineSourceSearchInput").value = "";
  renderTimelineSourcePickerList();
  requestAnimationFrame(() => {
    $("#timelineSourceSearchInput").focus();
    $(".timeline-source-option[aria-selected=\"true\"]")?.scrollIntoView({ block: "center" });
  });
}

function filteredTimelineSourceIndexes() {
  const query = state.timeline.sourceFilter.trim().toLocaleLowerCase("zh-CN");
  return state.timeline.sourceVideos
    .map((video, index) => ({ video, index }))
    .filter(({ video }) => !query || video.name.toLocaleLowerCase("zh-CN").includes(query))
    .map(({ index }) => index);
}

function renderTimelineSourcePickerList() {
  const indexes = filteredTimelineSourceIndexes();
  $("#timelineSourceMatchCount").textContent = state.timeline.sourceFilter
    ? `找到 ${indexes.length} / ${state.timeline.sourceVideos.length} 个视频`
    : `共 ${state.timeline.sourceVideos.length} 个视频`;
  $("#timelineSourceList").innerHTML = indexes.length
    ? indexes.map(index => {
      const video = state.timeline.sourceVideos[index];
      const selected = index === state.timeline.sourceIndex;
      return `<button class="timeline-source-option" type="button" role="option" aria-selected="${selected}" data-source-index="${index}" title="${escapeHtml(video.name)}"><small>${index + 1} / ${state.timeline.sourceVideos.length}</small><span>${escapeHtml(video.name)}</span></button>`;
    }).join("")
    : '<div class="timeline-source-no-results">没有匹配的视频，换个文件名试试</div>';
  $$('[data-source-index]').forEach(option => {
    option.addEventListener("click", () => selectTimelineSource(Number(option.dataset.sourceIndex)));
  });
}

function renderTimelineSourceNavigation(loadingText = "") {
  const { sourceVideos, sourceIndex, sourceLoading } = state.timeline;
  const current = sourceVideos[sourceIndex] || null;
  $("#timelineCurrentSource").textContent = current?.name
    || (sourceLoading ? "正在载入源视频…" : "尚未载入源视频文件夹");
  $("#timelineSourcePosition").textContent = current
    ? (loadingText || `第 ${sourceIndex + 1} / ${sourceVideos.length} 个视频`)
    : (loadingText || "选择文件夹后可依次审核其中的视频");
  $("#previousVideoBtn").disabled = sourceLoading || sourceIndex <= 0;
  $("#nextVideoBtn").disabled = sourceLoading || sourceIndex < 0 || sourceIndex >= sourceVideos.length - 1;
  const sourceButton = $("#timelineCurrentSourceButton");
  sourceButton.disabled = sourceLoading || !current;
  sourceButton.title = current ? "点击搜索或选择其他视频" : "请先载入源视频文件夹";
  if (sourceLoading || !current) setTimelineSourcePickerOpen(false);
  const analyzeButton = $("#analyzeTimelineBtn");
  analyzeButton.disabled = sourceLoading;
  analyzeButton.textContent = sourceLoading
    ? "正在处理…"
    : current ? "分析当前视频" : "分析首个视频";
}

async function analyzeTimelineSource(sourceIndex) {
  const source = state.timeline.sourceVideos[sourceIndex];
  if (!source) throw new Error("请先载入包含视频的源文件夹");
  const silenceDuration = Number($("#timelineSilenceInput").value);
  setTimelineStatus("分析中", "running");
  resetTimelineWaveform();
  const analysis = await api("/timeline/analyze", {
    method: "POST",
    body: JSON.stringify({
      source_path: source.path,
      scene_threshold: Number($("#timelineThresholdInput").value),
      silence_duration_seconds: silenceDuration,
    }),
  });
  state.timeline.analysis = analysis;
  state.timeline.audioLocked = true;
  state.timeline.breakpoints = analysis.breakpoints.map(point => ({
    ...point,
    machine_origin_frame: point.frame_index,
  }));
  state.timeline.machineBreakpoints = analysis.breakpoints.map(point => ({ ...point }));
  state.timeline.selectedFrame = null;
  state.timeline.selectedFrames = [];
  state.timeline.playheadFrame = 0;
  state.timeline.snapTargetFrame = null;
  state.timeline.reviewSaved = false;
  state.timeline.reviewRevision = null;
  state.timeline.selectedSegmentId = null;
  state.timeline.sliceUnits = [];
  state.timeline.mergeSelection = [];
  resetTimelineHistory();
  resetTimelineWaveform();
  stopTimelineVideoSync();
  const video = $("#timelineVideo");
  updateTimelineMediaLayout(analysis.width, analysis.height);
  video.src = analysis.media_url;
  video.load();
  $("#timelineEmpty").classList.add("hidden");
  $("#timelineWorkspace").classList.remove("hidden");
  fitTimelineToViewport(false);
  renderTimeline();
  if (!analysis.audio || typeof analysis.audio.has_audio !== "boolean") {
    setTimelineStatus("后端需重启", "danger");
    toast("当前后端仍是旧版本，请重启 SmartStitch 后重新分析", true);
  } else if (analysis.audio.speech_pause_status === "failed") {
    setTimelineStatus("待人工审核", "running");
    toast("语音停顿分析失败，本次仅使用画面转场候选", true);
  } else {
    setTimelineStatus("待人工审核", "running");
    const videoCount = analysis.breakpoints.filter(point => point.reasons?.includes("scene_change")).length;
    const audioCount = analysis.breakpoints.filter(point => point.reasons?.includes("speech_pause")).length;
    toast(`${source.name}：V1 ${videoCount} 个画面候选、A1 ${audioCount} 个语音候选`);
  }
}

function setTimelineStatus(text, type) {
  const element = $("#timelineStatus");
  element.textContent = text;
  element.className = `status ${type}`;
}

function timelineActiveBreakpoints() {
  return TimelineMath.activeTimelineBreakpoints(
    state.timeline.breakpoints,
    state.timeline.audioLocked,
  );
}

function toggleTimelineAudioLock() {
  const analysis = state.timeline.analysis;
  if (!analysis?.audio?.has_audio) return;
  recordTimelineEdit();
  state.timeline.audioLocked = !state.timeline.audioLocked;
  const activeFrames = new Set(timelineActiveBreakpoints().map(point => point.frame_index));
  setSelectedBreakpointFrames(
    state.timeline.selectedFrames.filter(frame => activeFrames.has(frame)),
  );
  state.timeline.selectedSegmentId = null;
  timelineReviewChanged();
  renderTimeline();
  toast(state.timeline.audioLocked
    ? "A1 已锁定：片段只按 V1 与人工断点划分，导出仍保留音频"
    : "A1 已解锁：语音停顿候选重新参与片段划分");
}

function updateTimelineMediaLayout(width, height) {
  const mediaGrid = $("#timelineMediaGrid");
  const orientation = TimelineMath.mediaLayoutOrientation(width, height);
  mediaGrid.classList.toggle("portrait", orientation === "portrait");
  mediaGrid.dataset.orientation = orientation;
}

function newSliceUnit(segmentIds, category = "") {
  return { id: `unit-${clientRequestId()}`, segmentIds: [...segmentIds], category };
}

function timelineEditSnapshot() {
  return structuredClone({
    audioLocked: state.timeline.audioLocked,
    breakpoints: state.timeline.breakpoints,
    selectedFrame: state.timeline.selectedFrame,
    selectedFrames: state.timeline.selectedFrames,
    selectedSegmentId: state.timeline.selectedSegmentId,
    sliceUnits: state.timeline.sliceUnits,
    mergeSelection: state.timeline.mergeSelection,
    reviewSaved: state.timeline.reviewSaved,
    reviewRevision: state.timeline.reviewRevision,
  });
}

function resetTimelineHistory() {
  state.timeline.undoStack = [];
  state.timeline.redoStack = [];
}

function recordTimelineEdit() {
  state.timeline.undoStack.push(timelineEditSnapshot());
  if (state.timeline.undoStack.length > 100) state.timeline.undoStack.shift();
  state.timeline.redoStack = [];
}

function restoreTimelineEditSnapshot(snapshot, message) {
  state.timeline.audioLocked = snapshot.audioLocked;
  state.timeline.breakpoints = structuredClone(snapshot.breakpoints);
  state.timeline.selectedFrame = snapshot.selectedFrame;
  state.timeline.selectedFrames = [...snapshot.selectedFrames];
  state.timeline.selectedSegmentId = snapshot.selectedSegmentId;
  state.timeline.sliceUnits = structuredClone(snapshot.sliceUnits);
  state.timeline.mergeSelection = [...snapshot.mergeSelection];
  state.timeline.reviewSaved = snapshot.reviewSaved;
  state.timeline.reviewRevision = snapshot.reviewRevision;
  $("#timelineVideo").pause();
  $("#timelineSliceResult").textContent = message;
  $("#timelineSliceResult").className = "timeline-slice-result";
  renderTimeline();
  toast(message);
}

function undoTimelineEdit() {
  const snapshot = state.timeline.undoStack.pop();
  if (!snapshot) return;
  state.timeline.redoStack.push(timelineEditSnapshot());
  restoreTimelineEditSnapshot(snapshot, "已撤销上一步操作");
}

function redoTimelineEdit() {
  const snapshot = state.timeline.redoStack.pop();
  if (!snapshot) return;
  state.timeline.undoStack.push(timelineEditSnapshot());
  restoreTimelineEditSnapshot(snapshot, "已重做上一步操作");
}

function sliceUnitForSegment(segmentId) {
  return state.timeline.sliceUnits.find(unit => unit.segmentIds.includes(segmentId)) || null;
}

function sliceUnitSegments(unit, allSegments = timelineSegments()) {
  const byId = new Map(allSegments.map(segment => [segment.id, segment]));
  return unit.segmentIds.map(segmentId => byId.get(segmentId)).filter(Boolean)
    .sort((left, right) => left.startFrame - right.startFrame);
}

function renderTimeline() {
  const analysis = state.timeline.analysis;
  if (!analysis) return;
  layoutTimelineCanvas();
  const segments = timelineSegments();
  const unitBySegment = new Map();
  const groupLabelBySegment = new Map();
  let groupNumber = 0;
  state.timeline.sliceUnits.forEach(unit => {
    if (unit.segmentIds.length > 1) groupNumber += 1;
    unit.segmentIds.forEach(segmentId => {
      unitBySegment.set(segmentId, unit);
      if (unit.segmentIds.length > 1) groupLabelBySegment.set(segmentId, `G${groupNumber}`);
    });
  });
  $("#timelineSegments").innerHTML = segments.map(segment => {
    const left = timelineXForFrame(segment.startFrame);
    const width = timelineXForFrame(segment.endFrame) - left;
    const unit = unitBySegment.get(segment.id);
    const category = unit?.category || "pending";
    const selected = state.timeline.selectedSegmentId === segment.id ? "selected" : "";
    const queued = unit ? "queued" : "";
    const groupLabel = groupLabelBySegment.get(segment.id) || "";
    const composite = groupLabel ? "composite-member" : "";
    const titlePrefix = groupLabel ? `${groupLabel} 组合成员` : "加入片段";
    const selectionLabel = selected ? "移除片段" : "选中片段";
    return `<button class="timeline-segment ${segmentCategoryClass(category)} ${selected} ${queued} ${composite}" type="button" data-timeline-segment="${segment.id}" data-group-label="${groupLabel}" style="left:${left}px;width:${width}px" title="${titlePrefix} ${segment.index} · ${escapeHtml(categoryLabelForTimeline(category))} · ${segment.durationFrames} 帧" aria-label="${selectionLabel} ${segment.index}" aria-pressed="${selected ? "true" : "false"}"></button>`;
  }).join("");
  const audioLocked = state.timeline.audioLocked;
  const activeBreakpoints = timelineActiveBreakpoints();
  const selectedFrames = new Set(state.timeline.selectedFrames);
  const markerHtml = (point, lane = "") => `
    <button class="timeline-marker ${selectedFrames.has(point.frame_index) ? "selected" : ""} ${point.frame_index === state.timeline.snapTargetFrame ? "snap-target" : ""} ${point.review_status === "machine_suggested" ? "machine" : "human"}"
      style="left:${timelineXForFrame(point.frame_index)}px" data-frame="${point.frame_index}" data-lane="${lane}"
      ${lane === "A" && audioLocked ? "disabled aria-disabled=\"true\"" : ""}
      aria-label="${lane === "V" ? "画面转场" : lane === "A" ? "语音停顿" : "人工"}断点，第 ${point.frame_index} 帧"
      title="${lane === "V" ? "V1 画面转场" : lane === "A" ? "A1 语音停顿" : "人工确认断点"} · 第 ${point.frame_index} 帧 · ${timelineReasonLabel(point)}"><i></i></button>`;
  const machinePoints = state.timeline.breakpoints.filter(point => point.review_status === "machine_suggested");
  $("#timelineVideoMarkers").innerHTML = machinePoints
    .filter(point => point.reasons?.includes("scene_change"))
    .map(point => markerHtml(point, "V")).join("");
  $("#timelineAudioMarkers").innerHTML = machinePoints
    .filter(point => point.reasons?.some(reason => reason === "speech_pause" || reason === "silence_end"))
    .map(point => markerHtml(point, "A")).join("");
  $("#timelineMarkers").innerHTML = state.timeline.breakpoints
    .filter(point => point.review_status !== "machine_suggested" && activeBreakpoints.includes(point))
    .map(point => markerHtml(point)).join("");
  $$('[data-timeline-segment]').forEach(segment => {
    segment.addEventListener("click", () => selectTimelineSegment(
      segment.dataset.timelineSegment,
      { toggleMembership: true },
    ));
  });
  $$(".timeline-marker:not(:disabled)").forEach(marker => bindTimelineMarker(marker));
  const audioTrack = $("#timelineAudioTrack");
  const audioLockButton = $("#timelineAudioLockBtn");
  const hasAudio = Boolean(analysis.audio?.has_audio);
  audioTrack.classList.toggle("locked", audioLocked);
  audioLockButton.disabled = !hasAudio;
  audioLockButton.setAttribute("aria-pressed", String(audioLocked));
  audioLockButton.setAttribute("aria-label", audioLocked ? "解锁 A1 音频轨道" : "锁定 A1 音频轨道");
  audioLockButton.title = hasAudio
    ? (audioLocked ? "A1 已锁定：点击解锁语音候选" : "A1 已解锁：点击锁定语音候选")
    : "此视频没有可锁定的音轨";
  const hasAudioMetadata = analysis.audio && typeof analysis.audio.has_audio === "boolean";
  const audioLabel = !hasAudioMetadata
    ? "音频待重新分析"
    : analysis.audio.has_audio
      ? `${analysis.audio.channels || 1} 声道音频`
      : "无音轨";
  $("#timelineMeta").innerHTML = `<span>${escapeHtml(analysis.source_name)}</span><span>${analysis.width}×${analysis.height}</span><span>${analysis.fps.toFixed(3)} fps</span><span>${analysis.frame_count} 帧</span><span>${formatPreciseTime(analysis.duration)}</span><span>${audioLabel}</span>`;
  const outputCount = state.timeline.sliceUnits.length;
  const sourceCount = state.timeline.sliceUnits.reduce((sum, unit) => sum + unit.segmentIds.length, 0);
  const classifiedCount = state.timeline.sliceUnits.filter(unit => unit.category).length;
  const videoCandidateCount = machinePoints.filter(point => point.reasons?.includes("scene_change")).length;
  const audioCandidateCount = machinePoints.filter(point => point.reasons?.some(reason => reason === "speech_pause" || reason === "silence_end")).length;
  const lockSummary = !hasAudio
    ? " · A1 不可用"
    : audioLocked ? " · A1 已锁定" : " · A1 已解锁";
  $("#timelineBreakpointSummary").textContent = `${segments.length} 个可选片段 · ${activeBreakpoints.length} 个当前断点（V1 ${videoCandidateCount} / A1 ${audioCandidateCount}${lockSummary}） · ${outputCount} 个输出片段 · ${sourceCount} 个源区间 · ${classifiedCount} 个已分类`;
  renderTimelineRuler();
  scheduleTimelineWaveformRender();
  renderSegmentList();
  renderSliceControls();
  syncSelectedBreakpointControls();
  renderTimelinePlayhead();
  updateTimelineSnapVisual();
}

function layoutTimelineCanvas() {
  const analysis = state.timeline.analysis;
  const viewport = $("#timelineViewport");
  const timelineDuration = Math.max(analysis.duration, analysis.frame_count / analysis.fps);
  const width = Math.max(viewport.clientWidth, timelineDuration * state.timeline.pixelsPerSecond);
  $("#timelineCanvas").style.width = `${Math.ceil(width)}px`;
  $("#timelineZoomInput").value = String(Math.round(state.timeline.pixelsPerSecond));
  $("#timelineZoomValue").textContent = `${Math.round(state.timeline.pixelsPerSecond)} px/s`;
}

function timelineXForFrame(frame) {
  return frame / state.timeline.analysis.fps * state.timeline.pixelsPerSecond;
}

function renderTimelineRuler() {
  const analysis = state.timeline.analysis;
  if (!analysis) return;
  const viewport = $("#timelineViewport");
  const pixelsPerSecond = state.timeline.pixelsPerSecond;
  const { majorSeconds, minorSeconds } = TimelineMath.chooseRulerStep(pixelsPerSecond);
  const startSeconds = Math.max(0, (viewport.scrollLeft - 100) / pixelsPerSecond);
  const endSeconds = Math.min(
    analysis.duration,
    (viewport.scrollLeft + viewport.clientWidth + 100) / pixelsPerSecond,
  );
  const firstIndex = Math.max(0, Math.floor(startSeconds / minorSeconds));
  const lastIndex = Math.ceil(endSeconds / minorSeconds);
  const majorEvery = Math.max(1, Math.round(majorSeconds / minorSeconds));
  const ticks = [];
  for (let index = firstIndex; index <= lastIndex; index += 1) {
    const seconds = index * minorSeconds;
    if (seconds > analysis.duration + 1e-6) break;
    const major = index % majorEvery === 0;
    const frame = Math.min(analysis.frame_count - 1, Math.round(seconds * analysis.fps));
    ticks.push(`<i class="${major ? "major" : "minor"}" style="left:${seconds * pixelsPerSecond}px">${major ? `<span>${frameTimecode(frame, analysis.fps)}</span>` : ""}</i>`);
  }
  $("#timelineTicks").innerHTML = ticks.join("");
}

function resetTimelineWaveform() {
  const waveform = state.timeline.waveform;
  waveform.controller?.abort();
  if (waveform.animationFrameId !== null) cancelAnimationFrame(waveform.animationFrameId);
  waveform.controller = null;
  waveform.requestKey = null;
  waveform.dataKey = null;
  waveform.data = null;
  waveform.animationFrameId = null;
  const canvas = $("#timelineWaveform");
  if (canvas) {
    canvas.width = 1;
    canvas.height = 1;
  }
  const status = $("#timelineAudioState");
  if (status) status.textContent = "";
}

function scheduleTimelineWaveformRender() {
  const waveform = state.timeline.waveform;
  if (!state.timeline.analysis || waveform.animationFrameId !== null) return;
  waveform.animationFrameId = requestAnimationFrame(() => {
    waveform.animationFrameId = null;
    renderTimelineWaveform();
  });
}

function prepareTimelineWaveformCanvas() {
  const viewport = $("#timelineViewport");
  const canvas = $("#timelineWaveform");
  const { left: timelineLeft, width: cssWidth } = TimelineMath.waveformViewportGeometry({
    scrollLeft: viewport.scrollLeft,
    viewportWidth: viewport.clientWidth,
    pixelsPerSecond: state.timeline.pixelsPerSecond,
    duration: state.timeline.analysis.duration,
  });
  const cssHeight = 64;
  const pixelRatio = Math.max(1, window.devicePixelRatio || 1);
  canvas.style.left = `${timelineLeft}px`;
  canvas.style.width = `${cssWidth}px`;
  canvas.style.height = `${cssHeight}px`;
  canvas.width = Math.max(1, Math.round(cssWidth * pixelRatio));
  canvas.height = Math.max(1, Math.round(cssHeight * pixelRatio));
  const context = canvas.getContext("2d");
  context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  context.clearRect(0, 0, cssWidth, cssHeight);
  return { canvas, context, cssWidth, cssHeight, timelineLeft };
}

function drawTimelineWaveform(view, data) {
  const { context, cssWidth, cssHeight, timelineLeft } = view;
  const peaks = data?.peaks || [];
  const middle = cssHeight / 2;
  context.save();
  context.strokeStyle = getComputedStyle(document.documentElement)
    .getPropertyValue("--green").trim() || "#1d5c43";
  context.lineWidth = 1;
  context.globalAlpha = 0.2;
  context.beginPath();
  context.moveTo(0, middle + 0.5);
  context.lineTo(cssWidth, middle + 0.5);
  context.stroke();
  if (peaks.length) {
    const startFrame = Number(data.start_frame) || 0;
    const endFrame = Number(data.end_frame) || startFrame + 1;
    const frameSpan = Math.max(1, endFrame - startFrame);
    const amplitude = cssHeight / 2 - 5;
    context.globalAlpha = 0.58;
    context.beginPath();
    peaks.forEach((peak, index) => {
      const frame = startFrame + (index + 0.5) / peaks.length * frameSpan;
      const x = timelineXForFrame(frame) - timelineLeft;
      if (x < -1 || x > cssWidth + 1) return;
      context.moveTo(x, middle - peak[1] * amplitude);
      context.lineTo(x, middle - peak[0] * amplitude);
    });
    context.stroke();
  }
  context.restore();
}

async function renderTimelineWaveform() {
  const analysis = state.timeline.analysis;
  if (!analysis) return;
  const view = prepareTimelineWaveformCanvas();
  const status = $("#timelineAudioState");
  if (!analysis.audio || typeof analysis.audio.has_audio !== "boolean") {
    status.textContent = "后端版本过旧，请重启后重新分析";
    return;
  }
  const audio = analysis.audio;
  if (!audio.has_audio || audio.waveform_status === "unavailable") {
    status.textContent = "此视频没有音轨";
    return;
  }
  if (audio.waveform_status !== "ready") {
    status.textContent = audio.waveform_status === "failed"
      ? "波形暂不可用"
      : "正在生成音频波形…";
    return;
  }

  const waveform = state.timeline.waveform;
  // Reproject cached absolute-frame data immediately so zooming feels continuous.
  if (waveform.data) drawTimelineWaveform(view, waveform.data);

  const viewport = $("#timelineViewport");
  const { startFrame, endFrame } = TimelineMath.waveformFrameRange({
    scrollLeft: viewport.scrollLeft,
    viewportWidth: viewport.clientWidth,
    pixelsPerSecond: state.timeline.pixelsPerSecond,
    fps: analysis.fps,
    frameCount: analysis.frame_count,
  });
  const widthPx = Math.max(1, Math.min(4096, Math.ceil(view.cssWidth)));
  const key = `${analysis.analysis_id}:${startFrame}:${endFrame}:${widthPx}`;
  if (waveform.dataKey === key && waveform.data) {
    status.textContent = "";
    drawTimelineWaveform(view, waveform.data);
    return;
  }
  if (waveform.requestKey === key) {
    status.textContent = "正在读取音频波形…";
    return;
  }

  waveform.controller?.abort();
  const controller = new AbortController();
  waveform.controller = controller;
  waveform.requestKey = key;
  status.textContent = "正在读取音频波形…";
  try {
    const data = await api(
      `/timeline/waveforms/${analysis.analysis_id}?start_frame=${startFrame}&end_frame=${endFrame}&width_px=${widthPx}`,
      { signal: controller.signal },
    );
    if (
      controller.signal.aborted
      || state.timeline.analysis?.analysis_id !== analysis.analysis_id
      || waveform.requestKey !== key
    ) return;
    waveform.data = data;
    waveform.dataKey = key;
    waveform.requestKey = null;
    waveform.controller = null;
    status.textContent = "";
    drawTimelineWaveform(prepareTimelineWaveformCanvas(), data);
  } catch (error) {
    if (error.name === "AbortError") return;
    if (waveform.requestKey === key) {
      waveform.requestKey = null;
      waveform.controller = null;
      status.textContent = "波形暂不可用";
    }
  }
}

function fitTimelineToViewport(render = true) {
  const analysis = state.timeline.analysis;
  if (!analysis) return;
  const viewport = $("#timelineViewport");
  state.timeline.pixelsPerSecond = Math.max(1, Math.min(240, viewport.clientWidth / analysis.duration));
  viewport.scrollLeft = 0;
  if (render) renderTimeline();
}

function setTimelineZoom(pixelsPerSecond, anchorClientX = null) {
  const analysis = state.timeline.analysis;
  if (!analysis) return;
  const viewport = $("#timelineViewport");
  const oldPixelsPerSecond = state.timeline.pixelsPerSecond;
  let anchorSeconds;
  let anchorViewportX;
  if (Number.isFinite(anchorClientX)) {
    const viewportRect = viewport.getBoundingClientRect();
    anchorViewportX = Math.max(0, Math.min(viewport.clientWidth, anchorClientX - viewportRect.left));
    anchorSeconds = (viewport.scrollLeft + anchorViewportX) / oldPixelsPerSecond;
  } else {
    anchorSeconds = state.timeline.playheadFrame / analysis.fps;
    anchorViewportX = anchorSeconds * oldPixelsPerSecond - viewport.scrollLeft;
  }
  state.timeline.pixelsPerSecond = Math.max(1, Math.min(240, pixelsPerSecond));
  layoutTimelineCanvas();
  viewport.scrollLeft = Math.max(
    0,
    anchorSeconds * state.timeline.pixelsPerSecond - anchorViewportX,
  );
  renderTimeline();
}

function handleTimelineWheel(event) {
  if (!event.altKey || !state.timeline.analysis) return;
  event.preventDefault();
  const delta = event.deltaY || event.deltaX;
  if (!delta) return;
  const zoomFactor = Math.exp(-delta * 0.003);
  setTimelineZoom(state.timeline.pixelsPerSecond * zoomFactor, event.clientX);
}

function toggleTimelinePlayback() {
  const video = $("#timelineVideo");
  if (video.paused || video.ended) {
    if (video.ended) video.currentTime = 0;
    video.play().catch(() => toast("视频暂时无法播放", true));
  } else {
    video.pause();
  }
}

function setSelectedBreakpointFrames(frames) {
  const available = new Set(state.timeline.breakpoints.map(point => point.frame_index));
  const selected = [...new Set(frames)]
    .filter(frame => available.has(frame))
    .sort((first, second) => first - second);
  state.timeline.selectedFrames = selected;
  state.timeline.selectedFrame = selected.length === 1 ? selected[0] : null;
  state.timeline.selectedSegmentId = null;
}

function clampTimelineX(clientX) {
  const canvas = $("#timelineCanvas");
  const x = timelineXFromClientX(clientX);
  return Math.max(0, Math.min(canvas.getBoundingClientRect().width, x));
}

function beginTimelineMarquee(event) {
  if (event.button !== 0 || !state.timeline.analysis || state.timeline.drag || state.timeline.marquee) return;
  event.preventDefault();
  const startX = clampTimelineX(event.clientX);
  const captureTarget = event.currentTarget;
  try { captureTarget.setPointerCapture(event.pointerId); } catch (_) {}
  state.timeline.marquee = {
    pointerId: event.pointerId,
    captureTarget,
    startX,
    currentX: startX,
    moved: false,
  };
  setSelectedBreakpointFrames([]);
  syncSelectedBreakpointControls();
  $$(".timeline-marker.selected").forEach(marker => marker.classList.remove("selected"));
  window.addEventListener("pointermove", handleTimelineMarqueeMove);
  window.addEventListener("pointerup", finishTimelineMarquee);
  window.addEventListener("pointercancel", finishTimelineMarquee);
  window.addEventListener("blur", finishTimelineMarquee);
}

function handleTimelineMarqueeMove(event) {
  const marquee = state.timeline.marquee;
  if (!marquee || (event.pointerId !== undefined && event.pointerId !== marquee.pointerId)) return;
  event.preventDefault();
  marquee.currentX = clampTimelineX(event.clientX);
  if (Math.abs(marquee.currentX - marquee.startX) >= 3) marquee.moved = true;
  if (!marquee.moved) return;

  const left = Math.min(marquee.startX, marquee.currentX);
  const right = Math.max(marquee.startX, marquee.currentX);
  const box = $("#timelineMarqueeBox");
  box.style.left = `${left}px`;
  box.style.width = `${Math.max(1, right - left)}px`;
  box.classList.remove("hidden");

  setSelectedBreakpointFrames(TimelineMath.breakpointFramesInPixelRange({
    breakpoints: timelineActiveBreakpoints(),
    startX: marquee.startX,
    endX: marquee.currentX,
    pixelsPerSecond: state.timeline.pixelsPerSecond,
    fps: state.timeline.analysis.fps,
  }));
  const selected = new Set(state.timeline.selectedFrames);
  $$(".timeline-marker").forEach(marker => {
    marker.classList.toggle("selected", selected.has(Number(marker.dataset.frame)));
  });
  syncSelectedBreakpointControls();
}

function finishTimelineMarquee(event) {
  const marquee = state.timeline.marquee;
  if (!marquee || (event?.pointerId !== undefined && event.pointerId !== marquee.pointerId)) return;
  window.removeEventListener("pointermove", handleTimelineMarqueeMove);
  window.removeEventListener("pointerup", finishTimelineMarquee);
  window.removeEventListener("pointercancel", finishTimelineMarquee);
  window.removeEventListener("blur", finishTimelineMarquee);
  try { marquee.captureTarget.releasePointerCapture(marquee.pointerId); } catch (_) {}
  state.timeline.marquee = null;
  $("#timelineMarqueeBox").classList.add("hidden");
  renderTimeline();
}

function bindTimelineMarker(marker) {
  marker.addEventListener("click", event => {
    event.stopPropagation();
    if (Date.now() < state.timeline.suppressClickUntil) return;
    const frame = Number(marker.dataset.frame);
    selectTimelineBreakpoint(frame);
    seekTimelineFrame(frame, "breakpoint_select");
  });
  marker.addEventListener("pointerdown", event => {
    event.preventDefault();
    event.stopPropagation();
    const originalFrame = Number(marker.dataset.frame);
    const point = state.timeline.breakpoints.find(item => item.frame_index === originalFrame);
    if (!point) return;
    setSelectedBreakpointFrames([originalFrame]);
    marker.classList.add("selected");
    syncSelectedBreakpointControls();
    beginTimelineDrag(event, { kind: "breakpoint", anchorFrame: originalFrame, point });
  });
}

function beginTimelineDrag(event, { kind, anchorFrame, point = null }) {
  if (!state.timeline.analysis || state.timeline.drag) return;
  event.preventDefault();
  const video = $("#timelineVideo");
  const captureTarget = event.currentTarget;
  try { captureTarget.setPointerCapture(event.pointerId); } catch (_) {}
  state.timeline.dragging = true;
  state.timeline.snapTargetFrame = null;
  state.timeline.shiftPressed = event.shiftKey;
  const pointerTimelineX = timelineXFromClientX(event.clientX);
  state.timeline.drag = {
    kind,
    point,
    pointerId: event.pointerId,
    captureTarget,
    anchorFrame,
    originalFrame: point?.frame_index ?? anchorFrame,
    targetFrame: anchorFrame,
    startClientX: event.clientX,
    latestClientX: event.clientX,
    grabOffsetPx: kind === "breakpoint" || captureTarget.id === "timelinePlayhead"
      ? pointerTimelineX - timelineXForFrame(anchorFrame)
      : 0,
    moved: false,
    wasPlaying: !video.paused,
  };
  $("#timelineTrack").classList.add("is-dragging");
  video.pause();
  window.addEventListener("pointermove", handleTimelineDragMove);
  window.addEventListener("pointerup", finishTimelineDrag);
  window.addEventListener("pointercancel", finishTimelineDrag);
  window.addEventListener("blur", finishTimelineDrag);
}

function handleTimelineDragMove(event) {
  const drag = state.timeline.drag;
  if (!drag || (event.pointerId !== undefined && event.pointerId !== drag.pointerId)) return;
  event.preventDefault();
  if (Math.abs(event.clientX - drag.startClientX) >= 2) drag.moved = true;
  drag.latestClientX = event.clientX;
  state.timeline.shiftPressed = event.shiftKey;
  scheduleTimelineDragFrame();
}

function scheduleTimelineDragFrame() {
  if (!state.timeline.drag || state.timeline.dragAnimationFrameId !== null) return;
  state.timeline.dragAnimationFrameId = requestAnimationFrame(() => {
    state.timeline.dragAnimationFrameId = null;
    applyTimelineDragFrame();
  });
}

function applyTimelineDragFrame() {
  const drag = state.timeline.drag;
  if (!drag) return;
  const minimum = drag.kind === "breakpoint" ? 1 : 0;
  const maximum = state.timeline.analysis.frame_count - 1;
  const pointerTimelineX = Math.max(
    0,
    timelineXFromClientX(drag.latestClientX) - drag.grabOffsetPx,
  );
  const rawFrame = TimelineMath.frameFromTimelineX(
    pointerTimelineX,
    state.timeline.pixelsPerSecond,
    state.timeline.analysis.fps,
    minimum,
    maximum,
  );
  drag.targetFrame = snappedTimelineFrame(rawFrame, pointerTimelineX, drag);
  if (drag.kind === "breakpoint") {
    $$(`.timeline-marker[data-frame="${drag.originalFrame}"]`).forEach(marker => {
      marker.style.left = `${timelineXForFrame(drag.targetFrame)}px`;
    });
    $("#selectedFrameInput").value = String(drag.targetFrame);
  } else {
    state.timeline.playheadFrame = drag.targetFrame;
    seekTimelineFrame(drag.targetFrame, "timeline_scrub");
  }
  updateTimelineSnapVisual();
}

function finishTimelineDrag(event) {
  const drag = state.timeline.drag;
  if (!drag || (event?.pointerId !== undefined && event.pointerId !== drag.pointerId)) return;
  if (Number.isFinite(event?.clientX)) drag.latestClientX = event.clientX;
  if (state.timeline.dragAnimationFrameId !== null) {
    cancelAnimationFrame(state.timeline.dragAnimationFrameId);
    state.timeline.dragAnimationFrameId = null;
  }
  applyTimelineDragFrame();
  window.removeEventListener("pointermove", handleTimelineDragMove);
  window.removeEventListener("pointerup", finishTimelineDrag);
  window.removeEventListener("pointercancel", finishTimelineDrag);
  window.removeEventListener("blur", finishTimelineDrag);
  try { drag.captureTarget.releasePointerCapture(drag.pointerId); } catch (_) {}

  state.timeline.drag = null;
  state.timeline.dragging = false;
  state.timeline.shiftPressed = false;
  state.timeline.snapTargetFrame = null;
  $("#timelineTrack").classList.remove("is-dragging");
  if (drag.moved) state.timeline.suppressClickUntil = Date.now() + 250;

  if (drag.kind === "breakpoint") {
    const occupied = state.timeline.breakpoints.find(
      item => item !== drag.point && item.frame_index === drag.targetFrame,
    );
    if (occupied) {
      setSelectedBreakpointFrames([occupied.frame_index]);
    } else if (drag.targetFrame !== drag.originalFrame) {
      recordTimelineEdit();
      drag.point.frame_index = drag.targetFrame;
      drag.point.time_seconds = drag.targetFrame / state.timeline.analysis.fps;
      drag.point.review_status = "human_adjusted";
      drag.point.reasons = ["human_adjusted"];
      setSelectedBreakpointFrames([drag.targetFrame]);
      sortTimelineBreakpoints();
      timelineReviewChanged();
    } else {
      setSelectedBreakpointFrames([drag.originalFrame]);
    }
  }

  if (drag.kind === "playhead") seekTimelineFrame(drag.targetFrame, "timeline_drag_end");
  renderTimeline();
  if (drag.wasPlaying && drag.kind === "breakpoint") $("#timelineVideo").play().catch(() => {});
}

function snappedTimelineFrame(rawFrame, pointerTimelineX, drag) {
  const candidates = timelineSnapCandidates(drag);
  const result = TimelineMath.choosePointerSnap({
    rawFrame,
    pointerTimelineX,
    pixelsPerSecond: state.timeline.pixelsPerSecond,
    fps: state.timeline.analysis.fps,
    candidates,
    lockedFrame: state.timeline.snapTargetFrame,
    enabled: state.timeline.shiftPressed,
  });
  state.timeline.snapTargetFrame = result.snapTargetFrame;
  return result.frame;
}

function timelineSnapCandidates(drag = null) {
  const byFrame = new Map();
  const add = (frame, priority, label) => {
    const current = byFrame.get(frame);
    if (!current || priority > current.priority) byFrame.set(frame, { frame, priority, label });
  };
  if (
    drag?.kind === "breakpoint"
    && state.timeline.playheadFrame > 0
    && state.timeline.playheadFrame < state.timeline.analysis.frame_count
  ) {
    add(state.timeline.playheadFrame, 4, "播放头");
  }
  timelineActiveBreakpoints().forEach(point => {
    if (point === drag?.point) return;
    const human = point.review_status !== "machine_suggested";
    add(point.frame_index, human ? 3 : 2, human ? "人工断点" : "机器断点");
  });
  TimelineMath.activeTimelineBreakpoints(
    state.timeline.machineBreakpoints,
    state.timeline.audioLocked,
  ).forEach(point => {
    if (
      drag?.point?.machine_origin_frame === point.frame_index
      && drag.originalFrame === point.frame_index
    ) return;
    add(point.frame_index, 2, "机器断点");
  });
  return [...byFrame.values()];
}

function timelineXFromClientX(clientX) {
  return clientX - $("#timelineCanvas").getBoundingClientRect().left;
}

function frameFromPointer(event, minimum = 0, snapEnabled = false) {
  const analysis = state.timeline.analysis;
  const timelineX = Math.max(0, timelineXFromClientX(event.clientX));
  const rawFrame = TimelineMath.frameFromTimelineX(
    timelineX,
    state.timeline.pixelsPerSecond,
    analysis.fps,
    minimum,
    analysis.frame_count - 1,
  );
  return TimelineMath.choosePointerSnap({
    rawFrame,
    pointerTimelineX: timelineX,
    pixelsPerSecond: state.timeline.pixelsPerSecond,
    fps: analysis.fps,
    candidates: timelineSnapCandidates(),
    enabled: snapEnabled,
  }).frame;
}

function currentTimelineFrame() {
  const analysis = state.timeline.analysis;
  return analysis ? TimelineMath.frameFromPlaybackTime(
    $("#timelineVideo").currentTime,
    analysis.fps,
    0,
    analysis.frame_count - 1,
  ) : 0;
}

function seekTimelineFrame(frame, source = "programmatic") {
  const analysis = state.timeline.analysis;
  if (!analysis) return;
  const normalized = Math.max(0, Math.min(analysis.frame_count - 1, Math.round(frame)));
  state.timeline.playheadFrame = normalized;
  state.timeline.syncSource = source;
  const video = $("#timelineVideo");
  const targetTime = TimelineMath.previewTimeForFrame(
    normalized,
    analysis.fps,
    analysis.duration,
  );
  if (Math.abs(video.currentTime - targetTime) > 1e-9) video.currentTime = targetTime;
  renderTimelinePlayhead(normalized);
}

function syncTimelineFromVideo(event = null) {
  if (!state.timeline.analysis || state.timeline.dragging) return;
  const mediaTime = event?.mediaTime;
  const frame = Number.isFinite(mediaTime)
    ? TimelineMath.frameFromPresentedTime(
      mediaTime,
      state.timeline.analysis.fps,
      0,
      state.timeline.analysis.frame_count - 1,
    )
    : currentTimelineFrame();
  state.timeline.playheadFrame = Math.max(
    0,
    Math.min(state.timeline.analysis.frame_count - 1, frame),
  );
  state.timeline.syncSource = "video_playback";
  renderTimelinePlayhead();
}

function startTimelineVideoSync() {
  stopTimelineVideoSync();
  const video = $("#timelineVideo");
  if (typeof video.requestVideoFrameCallback !== "function") return;
  const onFrame = (_now, metadata) => {
    state.timeline.videoFrameCallbackId = null;
    syncTimelineFromVideo(metadata);
    if (!video.paused && !video.ended) {
      state.timeline.videoFrameCallbackId = video.requestVideoFrameCallback(onFrame);
    }
  };
  state.timeline.videoFrameCallbackId = video.requestVideoFrameCallback(onFrame);
}

function stopTimelineVideoSync() {
  const video = $("#timelineVideo");
  if (
    state.timeline.videoFrameCallbackId !== null
    && typeof video.cancelVideoFrameCallback === "function"
  ) {
    video.cancelVideoFrameCallback(state.timeline.videoFrameCallbackId);
  }
  state.timeline.videoFrameCallbackId = null;
}

function stepTimelineFrame(delta) {
  const analysis = state.timeline.analysis;
  if (!analysis) return;
  const next = TimelineMath.stepFrameFromPlayhead(
    state.timeline.playheadFrame,
    delta,
    analysis.frame_count,
  );
  seekTimelineFrame(next, "frame_step");
}

function addBreakpointAtPlayhead() {
  addTimelineBreakpoint(currentTimelineFrame(), "human_added");
}

function addTimelineBreakpoint(frame, reviewStatus) {
  const analysis = state.timeline.analysis;
  frame = Math.max(1, Math.min(analysis.frame_count - 1, Math.round(frame)));
  if (state.timeline.breakpoints.some(point => point.frame_index === frame)) {
    selectTimelineBreakpoint(frame);
    return;
  }
  recordTimelineEdit();
  state.timeline.breakpoints.push({
    frame_index: frame,
    time_seconds: frame / analysis.fps,
    reasons: ["human_added"],
    confidence: 1,
    review_status: reviewStatus,
  });
  sortTimelineBreakpoints();
  setSelectedBreakpointFrames([frame]);
  timelineReviewChanged();
  renderTimeline();
}

function moveSelectedBreakpoint(frame) {
  const analysis = state.timeline.analysis;
  if (!analysis || state.timeline.selectedFrame === null || !Number.isFinite(frame)) return;
  frame = Math.max(1, Math.min(analysis.frame_count - 1, Math.round(frame)));
  const point = state.timeline.breakpoints.find(item => item.frame_index === state.timeline.selectedFrame);
  if (!point) return;
  if (state.timeline.breakpoints.some(item => item !== point && item.frame_index === frame)) return;
  if (frame === point.frame_index) return;
  recordTimelineEdit();
  point.frame_index = frame;
  point.time_seconds = frame / analysis.fps;
  point.review_status = "human_adjusted";
  point.reasons = ["human_adjusted"];
  setSelectedBreakpointFrames([frame]);
  sortTimelineBreakpoints();
  timelineReviewChanged();
  seekTimelineFrame(frame);
  renderTimeline();
}

function deleteSelectedBreakpoints() {
  const selected = new Set(state.timeline.selectedFrames);
  if (!selected.size) return;
  recordTimelineEdit();
  state.timeline.breakpoints = state.timeline.breakpoints.filter(
    point => !selected.has(point.frame_index),
  );
  setSelectedBreakpointFrames([]);
  timelineReviewChanged();
  renderTimeline();
}

function timelineReviewChanged() {
  state.timeline.reviewSaved = false;
  state.timeline.reviewRevision = null;
  const validSegmentIds = new Set(timelineSegments().map(segment => segment.id));
  const nextUnits = [];
  state.timeline.sliceUnits.forEach(unit => {
    const surviving = unit.segmentIds.filter(segmentId => validSegmentIds.has(segmentId));
    if (surviving.length === unit.segmentIds.length) {
      nextUnits.push(unit);
    } else {
      surviving.forEach(segmentId => nextUnits.push(newSliceUnit([segmentId], unit.category)));
    }
  });
  state.timeline.sliceUnits = nextUnits;
  const validUnitIds = new Set(nextUnits.map(unit => unit.id));
  state.timeline.mergeSelection = state.timeline.mergeSelection.filter(unitId => validUnitIds.has(unitId));
  if (!validSegmentIds.has(state.timeline.selectedSegmentId)) {
    state.timeline.selectedSegmentId = null;
  }
  $("#timelineSliceResult").textContent = "断点已变更；受影响的组合已拆分，切片时将保存最新审核版本";
  $("#timelineSliceResult").className = "timeline-slice-result";
}

function selectTimelineBreakpoint(frame) {
  setSelectedBreakpointFrames([frame]);
  renderTimeline();
}

function sortTimelineBreakpoints() {
  state.timeline.breakpoints.sort((a, b) => a.frame_index - b.frame_index);
}

function syncSelectedBreakpointControls() {
  const selected = state.timeline.selectedFrames;
  const singleFrame = selected.length === 1 ? selected[0] : null;
  const input = $("#selectedFrameInput");
  input.disabled = singleFrame === null;
  input.value = singleFrame ?? "";
  input.max = String((state.timeline.analysis?.frame_count || 1) - 1);
  const deleteButton = $("#deleteBreakpointBtn");
  deleteButton.disabled = selected.length === 0;
  deleteButton.textContent = selected.length > 1 ? `删除 ${selected.length} 个断点` : "删除断点";
}

function renderTimelinePlayhead(forcedFrame = null) {
  const analysis = state.timeline.analysis;
  if (!analysis) return;
  const frame = forcedFrame ?? state.timeline.playheadFrame;
  $("#timelinePlayhead").style.left = `${timelineXForFrame(frame)}px`;
  $("#timelineTimecode").textContent = frameTimecode(frame, analysis.fps);
  $("#timelineFrameMeta").textContent = `第 ${frame} 帧 · ${formatPreciseTime(frame / analysis.fps)}`;
}

function updateTimelineSnapVisual() {
  const guide = $("#timelineSnapGuide");
  const analysis = state.timeline.analysis;
  const frame = state.timeline.snapTargetFrame;
  if (!analysis || frame === null) {
    guide.classList.add("hidden");
    return;
  }
  const candidate = timelineSnapCandidates(state.timeline.drag).find(item => item.frame === frame);
  guide.style.left = `${timelineXForFrame(frame)}px`;
  guide.querySelector("span").textContent = `${candidate?.label || "断点"}吸附 · 第 ${frame} 帧 · ${frameTimecode(frame, analysis.fps)}`;
  guide.classList.remove("hidden");
  $$(".timeline-marker").forEach(marker => {
    marker.classList.toggle("snap-target", Number(marker.dataset.frame) === frame);
  });
}

function timelineSegmentId(startFrame, endFrame) {
  return `f${String(startFrame).padStart(9, "0")}-f${String(endFrame).padStart(9, "0")}`;
}

function timelineSegments() {
  const analysis = state.timeline.analysis;
  if (!analysis) return [];
  const breakpoints = timelineActiveBreakpoints();
  const boundaries = [0, ...breakpoints.map(point => point.frame_index), analysis.frame_count];
  return boundaries.slice(0, -1).map((startFrame, offset) => {
    const endFrame = boundaries[offset + 1];
    const endpoint = breakpoints[offset];
    return {
      index: offset + 1,
      id: timelineSegmentId(startFrame, endFrame),
      startFrame,
      endFrame,
      durationFrames: endFrame - startFrame,
      endReason: endpoint ? timelineReasonLabel(endpoint) : "视频结束",
    };
  });
}

function timelineCategoryOptions() {
  const options = [
    { category: "unclassified", label: "未归类" },
  ];
  if (!state.config) return options;
  state.config.timeline.forEach(category => {
    if (state.config.sources[category]) {
      options.push({ category, label: categoryLabel(category) });
    }
  });
  return options;
}

function categoryLabelForTimeline(category) {
  if (category === "pending") return "待分类";
  if (category === "skip") return "不入库";
  if (category === "unclassified") return "未归类";
  return categoryLabel(category);
}

function segmentCategoryClass(category) {
  if (isBenefitCategory(category)) return "cat-benefit";
  return {
    pending: "cat-pending",
    skip: "cat-skip",
    unclassified: "cat-unclassified",
    pre_roll: "cat-pre-roll",
    hook: "cat-hook",
    ending: "cat-ending",
    end_card: "cat-end-card",
  }[category] || "cat-unclassified";
}

function selectTimelineSegment(segmentId, { scrollIntoView = false, toggleMembership = false } = {}) {
  const segment = timelineSegments().find(item => item.id === segmentId);
  if (!segment) return;
  if (TimelineMath.shouldRemoveSelectedSegment(
    state.timeline.selectedSegmentId,
    segment.id,
    toggleMembership,
  )) {
    removeSliceSegment(segment.id);
    return;
  }
  if (!sliceUnitForSegment(segment.id)) {
    recordTimelineEdit();
    state.timeline.sliceUnits.push(newSliceUnit([segment.id]));
  }
  setSelectedBreakpointFrames([]);
  state.timeline.selectedSegmentId = segment.id;
  renderTimeline();
  if (scrollIntoView) {
    requestAnimationFrame(() => {
      const unit = sliceUnitForSegment(segmentId);
      const row = unit ? $(`[data-unit-id="${unit.id}"]`) : null;
      row?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  }
}

function renderMergeToolbar() {
  const selectedCount = state.timeline.mergeSelection.length;
  const mergeButton = $("#mergeSegmentsBtn");
  const clearButton = $("#clearMergeSelectionBtn");
  mergeButton.disabled = selectedCount < 2;
  mergeButton.textContent = `合并所选（${selectedCount}）`;
  clearButton.disabled = selectedCount === 0;
}

function mergeSelectedSliceUnits() {
  const selectedIds = new Set(state.timeline.mergeSelection);
  const selectedUnits = state.timeline.sliceUnits.filter(unit => selectedIds.has(unit.id));
  if (selectedUnits.length < 2) return toast("请至少勾选两个片段", true);

  const allSegments = timelineSegments();
  const segmentStartFrames = Object.fromEntries(
    allSegments.map(segment => [segment.id, segment.startFrame]),
  );
  const segmentIds = [...new Set(selectedUnits.flatMap(unit => unit.segmentIds))];
  if (segmentIds.length > 20) return toast("一个组合最多包含 20 个源片段", true);

  recordTimelineEdit();
  const result = TimelineMath.mergeSliceUnits({
    units: state.timeline.sliceUnits,
    selectedUnitIds: state.timeline.mergeSelection,
    segmentStartFrames,
    mergedUnitId: `unit-${clientRequestId()}`,
  });
  state.timeline.sliceUnits = result.units;
  state.timeline.mergeSelection = [];
  state.timeline.selectedSegmentId = result.mergedUnit.segmentIds[0] || null;
  toast(`已合并 ${result.mergedUnit.segmentIds.length} 个源片段，中间未选择内容不会入库`);
  renderTimeline();
}

function clearMergeSelection() {
  state.timeline.mergeSelection = [];
  renderSegmentList();
}

function splitSliceUnit(unitId) {
  const unit = state.timeline.sliceUnits.find(item => item.id === unitId);
  if (!unit || unit.segmentIds.length < 2) return;
  recordTimelineEdit();
  state.timeline.sliceUnits = state.timeline.sliceUnits.flatMap(item => (
    item.id === unitId
      ? item.segmentIds.map(segmentId => newSliceUnit([segmentId], item.category))
      : [item]
  ));
  state.timeline.mergeSelection = state.timeline.mergeSelection.filter(id => id !== unitId);
  toast("组合片段已拆分，分类已保留");
  renderTimeline();
}

function removeSliceUnit(unitId, { recordHistory = true } = {}) {
  const unit = state.timeline.sliceUnits.find(item => item.id === unitId);
  if (!unit) return;
  if (recordHistory) recordTimelineEdit();
  state.timeline.sliceUnits = state.timeline.sliceUnits.filter(item => item.id !== unitId);
  state.timeline.mergeSelection = state.timeline.mergeSelection.filter(id => id !== unitId);
  if (unit.segmentIds.includes(state.timeline.selectedSegmentId)) state.timeline.selectedSegmentId = null;
  renderTimeline();
}

function removeSliceSegment(segmentId) {
  const unit = sliceUnitForSegment(segmentId);
  if (!unit) return;
  recordTimelineEdit();
  if (unit.segmentIds.length === 1) {
    removeSliceUnit(unit.id, { recordHistory: false });
    return;
  }
  unit.segmentIds = unit.segmentIds.filter(id => id !== segmentId);
  if (state.timeline.selectedSegmentId === segmentId) state.timeline.selectedSegmentId = null;
  renderTimeline();
}

function renderSegmentList() {
  const analysis = state.timeline.analysis;
  const allSegments = timelineSegments();
  renderMergeToolbar();
  if (!state.timeline.sliceUnits.length) {
    $("#timelineSegmentList").innerHTML = '<div class="breakpoint-empty">点击上方时间轴中的片段，将需要切割的条目加入这里。</div>';
    return;
  }
  const categoryOptions = timelineCategoryOptions();
  let compositeNumber = 0;
  $("#timelineSegmentList").innerHTML = state.timeline.sliceUnits.map((unit, unitOffset) => {
    const memberSegments = sliceUnitSegments(unit, allSegments);
    if (!memberSegments.length) return "";
    const composite = memberSegments.length > 1;
    if (composite) compositeNumber += 1;
    const selectedCategory = unit.category || "";
    const options = categoryOptions.map(option => (
      `<option value="${escapeHtml(option.category)}" ${selectedCategory === option.category ? "selected" : ""}>${escapeHtml(option.label)}</option>`
    )).join("");
    const totalFrames = memberSegments.reduce((sum, segment) => sum + segment.durationFrames, 0);
    const excludedFrames = memberSegments.slice(1).reduce((sum, segment, index) => (
      sum + Math.max(0, segment.startFrame - memberSegments[index].endFrame)
    ), 0);
    const selected = memberSegments.some(segment => state.timeline.selectedSegmentId === segment.id);
    const checked = state.timeline.mergeSelection.includes(unit.id);
    const memberMarkup = composite
      ? `<div class="segment-unit-parts">${memberSegments.map(segment => (
        `<button type="button" data-select-member="${segment.id}"><b>${String(segment.index).padStart(2, "0")}</b><span>${frameTimecode(segment.startFrame, analysis.fps)} → ${frameTimecode(segment.endFrame, analysis.fps)}</span></button>`
      )).join("")}</div>`
      : `<div class="segment-row-time"><strong>${frameTimecode(memberSegments[0].startFrame, analysis.fps)}</strong><b>→</b><strong>${frameTimecode(memberSegments[0].endFrame, analysis.fps)}</strong></div>`;
    const numberLabel = composite ? `G${compositeNumber}` : String(memberSegments[0].index).padStart(2, "0");
    const gapLabel = composite ? ` · 排除 ${excludedFrames} 帧间隙` : "";
    return `<div class="segment-row segment-unit ${composite ? "composite" : ""} ${selected ? "selected" : ""} ${checked ? "merge-selected" : ""}" data-unit-id="${unit.id}" tabindex="0">
      <input class="segment-merge-checkbox" type="checkbox" data-merge-unit="${unit.id}" ${checked ? "checked" : ""} aria-label="选择输出片段 ${unitOffset + 1} 用于合并">
      <span>${numberLabel}</span>
      ${memberMarkup}
      <small>${memberSegments.length} 段 · ${totalFrames} 帧 · ${formatPreciseTime(totalFrames / analysis.fps)}${gapLabel}</small>
      <select class="segment-type-select ${selectedCategory ? "" : "pending"}" data-unit-category="${unit.id}" ${state.configId ? "" : "disabled"} aria-label="输出片段 ${unitOffset + 1} 类型">
        <option value="" disabled ${selectedCategory ? "" : "selected"}>选择类型</option>${options}
      </select>
      <i>${composite ? `组合 ${memberSegments.length} 段` : `结束：${escapeHtml(memberSegments[0].endReason)}`}</i>
      <div class="segment-row-actions">
        ${composite ? `<button class="segment-split-button" type="button" data-split-unit="${unit.id}">拆分</button>` : ""}
        <button class="segment-remove-button" type="button" data-remove-unit="${unit.id}">移除</button>
      </div>
    </div>`;
  }).join("");
  $$(".segment-row").forEach(row => {
    row.addEventListener("click", event => {
      if (event.target.closest("select, button, input")) return;
      const unit = state.timeline.sliceUnits.find(item => item.id === row.dataset.unitId);
      if (unit?.segmentIds[0]) selectTimelineSegment(unit.segmentIds[0]);
    });
    row.addEventListener("keydown", event => {
      if ((event.key === "Enter" || event.key === " ") && !event.target.closest("select, button, input")) {
        event.preventDefault();
        const unit = state.timeline.sliceUnits.find(item => item.id === row.dataset.unitId);
        if (unit?.segmentIds[0]) selectTimelineSegment(unit.segmentIds[0]);
      }
    });
  });
  $$('[data-select-member]').forEach(button => {
    button.addEventListener("click", event => {
      event.stopPropagation();
      selectTimelineSegment(event.currentTarget.dataset.selectMember);
    });
  });
  $$('[data-merge-unit]').forEach(checkbox => {
    checkbox.addEventListener("change", event => {
      const unitId = event.currentTarget.dataset.mergeUnit;
      if (event.currentTarget.checked) {
        if (!state.timeline.mergeSelection.includes(unitId)) state.timeline.mergeSelection.push(unitId);
      } else {
        state.timeline.mergeSelection = state.timeline.mergeSelection.filter(id => id !== unitId);
      }
      renderSegmentList();
    });
  });
  $$('[data-unit-category]').forEach(select => {
    select.addEventListener("click", event => event.stopPropagation());
    select.addEventListener("change", event => {
      const unit = state.timeline.sliceUnits.find(item => item.id === event.target.dataset.unitCategory);
      if (!unit) return;
      if (unit.category === event.target.value) return;
      recordTimelineEdit();
      unit.category = event.target.value;
      state.timeline.selectedSegmentId = unit.segmentIds[0] || null;
      renderTimeline();
    });
  });
  $$('[data-split-unit]').forEach(button => button.addEventListener("click", event => {
    event.stopPropagation();
    splitSliceUnit(event.currentTarget.dataset.splitUnit);
  }));
  $$('[data-remove-unit]').forEach(button => button.addEventListener("click", event => {
    event.stopPropagation();
    removeSliceUnit(event.currentTarget.dataset.removeUnit);
  }));
}

function renderSliceControls() {
  const analysis = state.timeline.analysis;
  const button = $("#sliceTimelineBtn");
  if (!analysis) {
    button.disabled = true;
    return;
  }

  const healthyLibrary = Boolean(state.library?.managed && state.library.health === "healthy");
  if (!state.configId) {
    $("#timelineSliceLibrary").textContent = "请先选择配置";
  } else if (healthyLibrary) {
    $("#timelineSliceLibrary").textContent = `入库到：${state.config.name} · ${state.library.root_path}`;
  } else {
    $("#timelineSliceLibrary").textContent = "切片前需选择或修复 SmartStitch 标准视频库";
  }
  const outputCount = state.timeline.sliceUnits.length;
  const allClassified = outputCount > 0 && state.timeline.sliceUnits.every(unit => Boolean(unit.category));
  button.disabled = !healthyLibrary || !allClassified;
}

async function exportTimelineSlices() {
  const analysis = state.timeline.analysis;
  if (!analysis || !state.configId) return;
  if (!state.library?.managed || state.library.health !== "healthy") {
    return toast("请先选择或修复 SmartStitch 标准视频库", true);
  }
  const segments = timelineSegments();
  if (!state.timeline.sliceUnits.length) return toast("请先在时间轴标记至少一个片段", true);
  const unclassifiedUnit = state.timeline.sliceUnits.find(unit => !unit.category);
  if (unclassifiedUnit) {
    const firstSegment = segments.find(segment => segment.id === unclassifiedUnit.segmentIds[0]);
    if (firstSegment) selectTimelineSegment(firstSegment.id, { scrollIntoView: true });
    return toast("请先为所有输出片段选择类型", true);
  }
  const segmentById = new Map(segments.map(segment => [segment.id, segment]));
  const assignments = state.timeline.sliceUnits.map(unit => ({
    client_unit_id: unit.id,
    segment_indexes: unit.segmentIds.map(segmentId => segmentById.get(segmentId)?.index)
      .filter(Number.isInteger),
    category: unit.category,
  }));
  if (assignments.some(item => item.segment_indexes.length === 0)) {
    return toast("片段边界已变化，请重新选择", true);
  }
  const cuttableCount = assignments.length;
  const sourceSegmentCount = assignments.reduce((sum, item) => sum + item.segment_indexes.length, 0);
  const button = $("#sliceTimelineBtn");
  const resultElement = $("#timelineSliceResult");
  const activeBreakpoints = timelineActiveBreakpoints();
  const requestFingerprint = JSON.stringify({
    analysisId: analysis.analysis_id,
    configHash: state.configHash,
    frames: activeBreakpoints.map(point => point.frame_index),
    assignments,
  });
  const requestId = state.timeline.sliceRequestFingerprint === requestFingerprint
    ? (state.timeline.sliceRequestId || clientRequestId())
    : clientRequestId();
  state.timeline.sliceRequestId = requestId;
  state.timeline.sliceRequestFingerprint = requestFingerprint;
  button.disabled = true;
  button.textContent = "正在保存并入队…";
  resultElement.textContent = `正在确认 ${cuttableCount} 个输出片段（${sourceSegmentCount} 个源区间）并保存断点…`;
  resultElement.className = "timeline-slice-result";
  try {
    const review = await api("/timeline/decisions", {
      method: "PUT",
      body: JSON.stringify({
        analysis_id: analysis.analysis_id,
        frame_indexes: activeBreakpoints.map(point => point.frame_index),
      }),
    });
    activeBreakpoints.forEach(point => { point.review_status = "human_confirmed"; });
    state.timeline.reviewSaved = true;
    state.timeline.reviewRevision = review.review_revision;
    button.textContent = "正在加入队列…";
    resultElement.textContent = `正在固化 ${cuttableCount} 个输出片段的任务快照…`;
    const result = await api("/timeline/slice-jobs", {
      method: "POST",
      body: JSON.stringify({
        analysis_id: analysis.analysis_id,
        config_id: state.configId,
        review_revision: state.timeline.reviewRevision,
        current_config_hash: state.configHash,
        assignments,
        client_request_id: requestId,
      }),
    });
    state.timeline.sliceRequestId = null;
    state.timeline.sliceRequestFingerprint = null;
    state.timeline.sliceUnits = [];
    state.timeline.mergeSelection = [];
    resetTimelineHistory();
    resultElement.textContent = `已加入切片队列 #${result.short_id}，可继续审核下一条视频`;
    resultElement.className = "timeline-slice-result success";
    setTimelineStatus("已加入队列", "success");
    toast(`已加入切片队列 #${result.short_id}`);
    renderTimeline();
    await loadSliceJobs();
  } catch (error) {
    resultElement.textContent = error.message;
    resultElement.className = "timeline-slice-result error";
    setTimelineStatus("入队失败", "danger");
    toast(error.message, true);
  } finally {
    button.textContent = "加入切片队列";
    renderSliceControls();
  }
}

async function loadSliceJobs() {
  try {
    state.sliceJobs = await api("/timeline/slice-jobs?limit=50");
    renderSliceQueue();
    if ($("#jobsView")?.classList.contains("active")) renderJobs();
  } catch (error) {
    toast(error.message, true);
  }
}

function connectSliceJobEvents() {
  if (state.sliceEventSource) state.sliceEventSource.close();
  state.sliceEventSource = new EventSource("/api/v1/timeline/slice-jobs/events");
  state.sliceEventSource.addEventListener("slice_jobs_update", async event => {
    state.sliceJobs = JSON.parse(event.data);
    renderSliceQueue();
    renderJobs();
    if (state.activeJob?.job_type === "timeline_slice") {
      const summary = state.sliceJobs.find(job => job.id === state.activeJob.id);
      if (summary) {
        try { renderSliceJobDetail(await api(`/timeline/slice-jobs/${summary.id}`)); } catch (_) {}
      }
    }
  });
}

function sliceJobStatusInfo(job) {
  if (job.status === "running") return ["切片中", "running"];
  return statusInfo(job.status);
}

function renderSliceQueue() {
  const container = $("#timelineSliceQueueList");
  if (!container) return;
  const activeCount = state.sliceJobs.filter(job => !terminalStates.has(job.status)).length;
  $("#timelineSliceQueueCount").textContent = activeCount ? `${activeCount} 个处理中` : "暂无处理中任务";
  const activeJobs = state.sliceJobs.filter(job => !terminalStates.has(job.status));
  const terminalJobs = state.sliceJobs.filter(job => terminalStates.has(job.status));
  const jobs = [...activeJobs, ...terminalJobs].slice(0, 20);
  if (!jobs.length) {
    container.innerHTML = '<div class="slice-queue-empty">提交后可在这里查看后台切片进度</div>';
    return;
  }
  container.innerHTML = jobs.map(job => {
    const [label, cls] = sliceJobStatusInfo(job);
    const progress = Math.max(0, Math.min(100, Number(job.progress || 0) * 100));
    const sourceName = job.source?.name || job.source?.path?.split(/[\\/]/).pop() || "未知视频";
    const finished = Number(job.success_count || 0) + Number(job.failure_count || 0) + Number(job.cancelled_count || 0);
    const terminalClass = terminalStates.has(job.status) ? " is-terminal" : "";
    return `<button class="slice-queue-row${terminalClass}" type="button" data-slice-job-id="${job.id}">
      <span class="slice-queue-source"><strong title="${escapeHtml(sourceName)}">${escapeHtml(sourceName)}</strong><small>#${escapeHtml(job.short_id)} · ${formatDate(job.created_at)}</small></span>
      <span class="slice-queue-progress"><span class="mini-progress"><i style="width:${progress}%"></i></span><small>${finished}/${job.output_unit_count} 已处理 · ${progress.toFixed(0)}%</small></span>
      <span class="status ${cls}">${label}</span><b>›</b>
    </button>`;
  }).join("");
  $$('[data-slice-job-id]').forEach(row => row.addEventListener("click", () => openSliceJob(row.dataset.sliceJobId)));
}

function timelineReasonLabel(point) {
  if (point.review_status === "human_added") return "人工添加";
  if (point.review_status === "human_adjusted") return "人工调整";
  const labels = {
    scene_change: "画面转场",
    silence_end: "静音结束",
    speech_pause: "语音停顿",
    human_added: "人工添加",
    human_adjusted: "人工调整",
  };
  return (point.reasons || []).map(reason => labels[reason] || reason).join(" + ") || "机器建议";
}

function frameTimecode(frame, fps) {
  const nominalFps = Math.max(1, Math.round(fps));
  const totalSeconds = Math.floor(frame / nominalFps);
  const frames = frame % nominalFps;
  const seconds = totalSeconds % 60;
  const minutes = Math.floor(totalSeconds / 60) % 60;
  const hours = Math.floor(totalSeconds / 3600);
  return [hours, minutes, seconds, frames].map(value => String(value).padStart(2, "0")).join(":");
}

function formatClock(seconds) {
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes).padStart(2, "0")}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}

function formatPreciseTime(seconds) {
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes).padStart(2, "0")}:${(seconds % 60).toFixed(3).padStart(6, "0")}`;
}

async function loadConfigs(preferredId = null) {
  state.configs = await api("/configs");
  const select = $("#configSelect");
  select.innerHTML = state.configs.map(config => `<option value="${escapeHtml(config.id)}" ${!config.valid ? "disabled" : ""}>${escapeHtml(config.name)}${config.valid ? "" : "（配置错误）"}</option>`).join("");
  const valid = state.configs.filter(config => config.valid);
  const hasConfigs = valid.length > 0;
  [$("#cloneConfigBtn"), $("#deleteConfigBtn"), $("#editConfigBtn"), $("#scanBtn"), $("#saveWeightsBtn"), $("#previewBtn"), $("#startBtn")]
    .forEach(button => { button.disabled = !hasConfigs; });
  if (!hasConfigs) {
    state.configId = null; state.config = null; state.configHash = null; state.library = null; state.scan = null;
    select.innerHTML = '<option value="">暂无可用配置</option>';
    $("#heroConfigName").textContent = "尚未创建配置";
    $("#heroAssetCount").textContent = "新建配置后开始扫描";
    $("#scanSummary").textContent = "请先新建一个配置";
    $("#assetTabs").innerHTML = "";
    $("#assetTable").innerHTML = '<tr><td colspan="7" style="text-align:center;padding:50px;color:var(--muted)">暂无配置</td></tr>';
    hideDeleteConfigConfirm();
    return;
  }
  const targetId = valid.some(config => config.id === preferredId) ? preferredId
    : valid.some(config => config.id === state.configId) ? state.configId : valid[0].id;
  await selectConfig(targetId);
}

async function selectConfig(id) {
  state.configId = id;
  $("#configSelect").value = id;
  try {
    const result = await api(`/configs/${id}`);
    state.config = result.config;
    state.configHash = result.content_hash;
    state.yaml = result.yaml_text;
    try {
      state.library = await api(`/libraries/by-config/${id}`);
    } catch (_) {
      state.library = null;
    }
    if (state.library?.config_updated) {
      const migrated = await api(`/configs/${id}`);
      state.config = migrated.config;
      state.configHash = migrated.content_hash;
      state.yaml = migrated.yaml_text;
    }
    if (state.library?.managed && state.library.health === "healthy") {
      try {
        const targets = await api(`/libraries/by-config/${id}/slice-targets`);
        state.timeline.sliceTargets = targets.targets;
      } catch (_) {
        state.timeline.sliceTargets = [];
      }
      if (!$("#timelinePathInput").value && !state.timeline.sourceVideos.length) {
        const rootPath = state.library.root_path.replace(/\/+$/, "");
        $("#timelinePathInput").value = `${rootPath}/原始视频`;
      }
    } else {
      state.timeline.sliceTargets = [];
    }
    state.timeline.sliceUnits = [];
    state.timeline.mergeSelection = [];
    state.timeline.selectedSegmentId = null;
    resetTimelineHistory();
    $("#heroConfigName").textContent = state.config.name;
    $("#countInput").value = state.config.batch.default_count;
    $("#concurrencyInput").value = state.config.batch.concurrency;
    state.preview = null;
    resetPreview();
    if (state.timeline.analysis) renderTimeline();
    await scanAssets(false);
  } catch (error) { toast(error.message, true); }
}

async function scanAssets(showToast = true) {
  if (!state.configId) return;
  const button = $("#scanBtn");
  button.disabled = true; button.textContent = "扫描中…";
  $("#scanSummary").textContent = "正在用 ffprobe 检查素材，请稍候…";
  try {
    state.scan = await api(`/configs/${state.configId}/scan`, { method: "POST" });
    const all = Object.values(state.scan.assets).flat();
    const valid = all.filter(asset => asset.valid && asset.enabled && asset.weight > 0).length;
    $("#heroAssetCount").textContent = `${valid} 个可用素材 · ${state.scan.ok ? "预检通过" : `${state.scan.errors.length} 个阻塞问题`}`;
    $("#scanSummary").innerHTML = state.scan.ok
      ? `<span class="valid">● 预检通过</span>　扫描到 ${all.length} 个素材，其中 ${valid} 个参与抽取`
      : `<span class="invalid">● 预检未通过</span>　${escapeHtml(state.scan.errors.join("；"))}`;
    renderAssetTabs(); renderAssets();
    if (showToast) toast(state.scan.ok ? "素材扫描完成" : "扫描完成，但存在阻塞问题", !state.scan.ok);
  } catch (error) { $("#scanSummary").textContent = error.message; toast(error.message, true); }
  finally {
    button.disabled = false;
    button.textContent = "重新扫描";
    updateGenerateAvailability();
  }
}

function updateGenerateAvailability() {
  const emptyGeneric = state.config?.workflow_type === "generic"
    && !state.config.timeline.some(category => state.config.sources[category]?.mode !== "disabled");
  $("#previewBtn").disabled = !state.config || emptyGeneric;
  $("#startBtn").disabled = !state.config || emptyGeneric;
  if (emptyGeneric) {
    $("#heroAssetCount").textContent = "请先在高级配置中添加并启用至少一个视频库";
  }
}

function renderAssetTabs() {
  if (!state.scan) return;
  const categories = Object.keys(state.scan.assets);
  if (!categories.includes(state.assetCategory)) state.assetCategory = categories[0];
  $("#assetTabs").innerHTML = categories.map(category => {
    const count = state.scan.assets[category].length;
    return `<button class="asset-tab ${state.assetCategory === category ? "active" : ""}" data-category="${escapeHtml(category)}">${escapeHtml(categoryLabel(category))} · ${count}</button>`;
  }).join("");
  $$(".asset-tab").forEach(button => button.addEventListener("click", () => {
    syncVisibleAssetValues(false);
    state.assetCategory = button.dataset.category; renderAssetTabs(); renderAssets();
  }));
}

function renderAssets() {
  const assets = state.scan?.assets[state.assetCategory] || [];
  const fixedAsset = ["benefit_overlay", "visual_border"].includes(state.assetCategory);
  const total = assets.filter(asset => asset.enabled && asset.valid).reduce((sum, asset) => sum + Number(asset.weight), 0);
  $("#assetTable").innerHTML = assets.length ? assets.map(asset => {
    const probe = asset.probe;
    const meta = probe ? (asset.media_type === "image" ? `${probe.width}×${probe.height} · 图片` : `${probe.width}×${probe.height} · ${formatDuration(probe.duration)} · ${probe.fps ? probe.fps.toFixed(2) + "fps" : "—"}`) : "无法读取";
    const percent = asset.enabled && asset.valid && total ? (asset.weight / total * 100).toFixed(1) : "0.0";
    return `<tr data-id="${asset.id}">
      <td>${fixedAsset ? '<span class="valid">固定</span>' : `<input class="check asset-enabled" type="checkbox" ${asset.enabled ? "checked" : ""}>`}</td>
      <td><div class="file-name" title="${escapeHtml(asset.name)}">${escapeHtml(asset.name)}</div><div class="file-path" title="${escapeHtml(asset.path)}">${escapeHtml(asset.path)}</div></td>
      <td><span class="media-meta">${escapeHtml(meta)}</span></td>
      <td>${fixedAsset ? "—" : `<input class="tags-input asset-tags" value="${escapeHtml(asset.tags.join(","))}" placeholder="通用">`}</td>
      <td>${fixedAsset ? "不参与随机" : `<input class="weight-input asset-weight" type="number" min="0" step="0.1" value="${asset.weight}">`}</td>
      <td>${fixedAsset ? "100%" : `${percent}%`}</td>
      <td><span class="${asset.valid ? "valid" : "invalid"}" title="${escapeHtml(asset.error || "")}">${asset.valid ? "可用" : "异常"}</span></td>
    </tr>`;
  }).join("") : `<tr><td colspan="7" style="text-align:center;padding:50px;color:var(--muted)">此类别当前没有素材</td></tr>`;
  $$(".asset-weight,.asset-enabled,.asset-tags").forEach(input => input.addEventListener("change", () => syncVisibleAssetValues(true)));
}

function syncVisibleAssetValues(rerender = true) {
  if (!state.scan?.assets[state.assetCategory]) return;
  if (["benefit_overlay", "visual_border"].includes(state.assetCategory)) return;
  const assets = state.scan.assets[state.assetCategory];
  $$("#assetTable tr[data-id]").forEach(row => {
    const asset = assets.find(item => item.id === row.dataset.id);
    asset.enabled = row.querySelector(".asset-enabled").checked;
    asset.weight = Number(row.querySelector(".asset-weight").value || 0);
    asset.tags = row.querySelector(".asset-tags").value.split(",").map(value => value.trim()).filter(Boolean);
  });
  if (rerender) renderAssets();
}

async function saveWeights() {
  if (!state.scan) return;
  syncVisibleAssetValues(false);
  const items = Object.entries(state.scan.assets)
    .filter(([category]) => !["benefit_overlay", "visual_border"].includes(category))
    .flatMap(([category, assets]) => assets.map(asset => ({ category, path: asset.path, enabled: asset.enabled, weight: Number(asset.weight), tags: asset.tags })));
  try {
    const saved = await withTemporaryConfigLease((lease, configHash) => api(`/configs/${state.configId}/weights`, {
      method: "POST",
      headers: configLeaseHeaders(lease, configHash),
      body: JSON.stringify({ items }),
    }));
    if (!saved) return;
    state.configHash = saved.content_hash;
    toast("权重已保存，原配置已备份");
    await selectConfig(state.configId);
  } catch (error) { toast(error.message, true); }
}

function requestValues() {
  const count = Number($("#countInput").value);
  const seedValue = $("#seedInput").value.trim();
  return {
    config_id: state.configId,
    count,
    seed: seedValue ? Number(seedValue) : null,
    output_directory: normalizePathField($("#outputInput")) || null,
  };
}

async function previewPlan() {
  const button = $("#previewBtn"); button.disabled = true; button.textContent = "计算中…";
  try {
    state.preview = await api("/plans/preview", { method: "POST", body: JSON.stringify(requestValues()) });
    $("#emptyPreview").classList.add("hidden"); $("#previewContent").classList.remove("hidden");
    $("#previewStatus").className = "status success"; $("#previewStatus").textContent = `${state.preview.count} 条 · 预检通过`;
    $("#previewSeed").textContent = state.preview.seed;
    $("#seedInput").value = state.preview.seed;
    $("#distributionList").innerHTML = Object.entries(state.preview.distribution).filter(([, entries]) => Object.keys(entries).length).map(([category, entries]) => {
      const rows = Object.entries(entries).sort((a,b) => b[1]-a[1]).map(([name, count]) => `<div class="dist-row"><span class="dist-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span><span class="bar"><i style="width:${count/state.preview.count*100}%"></i></span><b>${count}</b></div>`).join("");
      return `<div class="dist-group"><h4>${escapeHtml(categoryLabel(category))}</h4>${rows}</div>`;
    }).join("");
    $("#previewWarnings").innerHTML = state.preview.warnings.map(warning => `<div class="warning-box">${escapeHtml(warning)}</div>`).join("");
    toast("组合计划已生成");
  } catch (error) { resetPreview(); toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "预览组合"; }
}

function resetPreview() {
  $("#emptyPreview").classList.remove("hidden"); $("#previewContent").classList.add("hidden");
  $("#previewStatus").className = "status neutral"; $("#previewStatus").textContent = "尚未预览";
}

async function startJob() {
  const button = $("#startBtn"); button.disabled = true; button.querySelector("span").textContent = "正在创建…";
  try {
    const payload = { ...requestValues(), concurrency: Number($("#concurrencyInput").value), auto_start: true };
    const job = await api("/jobs", { method: "POST", body: JSON.stringify(payload) });
    toast("任务已开始生成");
    await loadJobs(); switchView("jobs"); openJob(job.id);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.querySelector("span").textContent = "开始生成"; }
}

async function loadJobs() {
  try { state.jobs = await api("/jobs"); renderJobs(); } catch (error) { toast(error.message, true); }
}

function renderJobs() {
  const list = $("#jobsList");
  const combinedJobs = [
    ...state.jobs.map(job => ({ ...job, job_type: job.job_type || "render" })),
    ...state.sliceJobs,
  ].sort((left, right) => String(right.created_at).localeCompare(String(left.created_at)));
  $("#deleteAllJobsBtn").disabled = state.jobs.length === 0;
  $("#deleteAllJobsCount").textContent = String(state.jobs.length);
  if (!state.jobs.length) hideDeleteAllJobsConfirm();
  if (!combinedJobs.length) { list.innerHTML = `<div class="empty-state"><h3>还没有任务</h3><p>成片生成和时间线切片任务都会在这里显示。</p></div>`; return; }
  list.innerHTML = combinedJobs.map(job => {
    const isSlice = job.job_type === "timeline_slice";
    const [label, cls] = isSlice ? sliceJobStatusInfo(job) : statusInfo(job.status);
    const done = Number(job.success_count || 0) + Number(job.failure_count || 0) + Number(job.cancelled_count || 0);
    const total = isSlice ? job.output_unit_count : job.count;
    const pct = isSlice ? Number(job.progress || 0) * 100 : (total ? done / total * 100 : 0);
    const sourceName = job.source?.name || job.source?.path?.split(/[\\/]/).pop();
    const title = isSlice ? sourceName : job.config_name;
    const typeLabel = isSlice ? "切片入库" : "成片渲染";
    return `<div class="job-row" data-job-id="${job.id}" data-job-type="${isSlice ? "slice" : "render"}"><div><strong>${escapeHtml(title || "未命名任务")}</strong><small>${typeLabel} · ${formatDate(job.created_at)} · #${job.short_id}</small></div><div><div class="mini-progress"><i style="width:${pct}%"></i></div><small>${done}/${total} 已处理</small></div><span class="status ${cls}">${label}</span><span class="job-count">成功 ${job.success_count || 0} · 失败 ${job.failure_count || 0}</span><b>›</b></div>`;
  }).join("");
  $$(".job-row").forEach(row => row.addEventListener("click", () => {
    if (row.dataset.jobType === "slice") openSliceJob(row.dataset.jobId);
    else openJob(row.dataset.jobId);
  }));
}

async function openJob(jobId) {
  $("#jobDrawer").classList.add("open"); $("#jobDrawer").setAttribute("aria-hidden", "false");
  if (state.eventSource) state.eventSource.close();
  if (state.feishuSyncTimer) clearTimeout(state.feishuSyncTimer);
  state.feishuSyncTimer = null;
  state.feishuSync = null;
  const update = job => { state.activeJob = job; renderJobDetail(job); };
  try { update(await api(`/jobs/${jobId}`)); } catch (error) { toast(error.message, true); return; }
  if (terminalStates.has(state.activeJob.status) && state.activeJob.feishu_base_sync?.enabled) {
    loadJobFeishuSync(state.activeJob);
  }
  if (!terminalStates.has(state.activeJob.status)) {
    state.eventSource = new EventSource(`/api/v1/jobs/${jobId}/events`);
    state.eventSource.addEventListener("job_update", event => {
      const job = JSON.parse(event.data); update(job); loadJobs();
      if (terminalStates.has(job.status)) {
        state.eventSource.close();
        if (job.feishu_base_sync?.enabled) loadJobFeishuSync(job);
      }
    });
  }
}

function renderFeishuSyncPanel(job) {
  if (!job.feishu_base_sync?.enabled) return "";
  const sync = state.feishuSync;
  const status = sync?.status || "not_started";
  const statusMap = {
    not_started: ["等待同步", "neutral"],
    pending: ["等待同步", "running"],
    running: ["同步中", "running"],
    succeeded: ["同步成功", "success"],
    failed: ["同步失败", "danger"],
    interrupted: ["同步中断", "danger"],
  };
  const [label, cls] = statusMap[status] || [status, "neutral"];
  const inProgress = status === "pending" || status === "running";
  const canRetry = terminalStates.has(job.status) && !inProgress && status !== "succeeded";
  const counts = status === "succeeded"
    ? `<p>已校验 ${Number(sync.verified_count || 0)} 条 · 新增 ${Number(sync.inserted_count || 0)} 条 · 更新 ${Number(sync.updated_count || 0)} 条</p>`
    : "";
  const error = sync?.error ? `<div class="error-text">${escapeHtml(sync.error)}</div>` : "";
  return `<section class="feishu-sync-panel">
    <div><span class="feishu-sync-icon">飞</span><div><strong>飞书多维表格同步</strong><small>${escapeHtml(sync?.table_name || "任务结束后自动写入目标数据表")}</small></div><span class="status ${cls}">${label}</span></div>
    ${counts}${error}
    <div class="feishu-sync-actions">
      <a class="button secondary small" href="${escapeHtml(job.feishu_base_sync.base_url)}" target="_blank" rel="noopener noreferrer">打开多维表格</a>
      ${canRetry ? '<button id="retryFeishuSyncBtn" class="button secondary small" type="button">立即同步 / 重试</button>' : ""}
    </div>
  </section>`;
}

async function loadJobFeishuSync(job) {
  if (!job?.feishu_base_sync?.enabled || state.activeJob?.id !== job.id) return;
  if (state.feishuSyncTimer) clearTimeout(state.feishuSyncTimer);
  state.feishuSyncTimer = null;
  try {
    state.feishuSync = await api(`/jobs/${job.id}/sync/feishu`);
    if (state.activeJob?.id !== job.id) return;
    renderJobDetail(state.activeJob);
    if (["pending", "running"].includes(state.feishuSync.status)) {
      state.feishuSyncTimer = setTimeout(() => loadJobFeishuSync(job), 1200);
    }
  } catch (error) {
    toast(error.message, true);
  }
}

async function retryJobFeishuSync(job) {
  const button = $("#retryFeishuSyncBtn");
  if (button) { button.disabled = true; button.textContent = "正在提交…"; }
  try {
    state.feishuSync = await api(`/jobs/${job.id}/sync/feishu`, { method: "POST" });
    renderJobDetail(job);
    state.feishuSyncTimer = setTimeout(() => loadJobFeishuSync(job), 300);
    toast("飞书表格同步已提交");
  } catch (error) {
    toast(error.message, true);
    if (button) { button.disabled = false; button.textContent = "立即同步 / 重试"; }
  }
}

function renderJobDetail(job) {
  const [label, cls] = statusInfo(job.status); const done = job.success_count + job.failure_count; const totalProgress = job.items.reduce((sum, item) => sum + item.progress, 0) / job.count * 100;
  $("#jobDetail").innerHTML = `<div class="job-detail-header"><p class="eyebrow">BATCH #${job.short_id}</p><h2>${escapeHtml(job.config_name)}</h2><span class="status ${cls}">${label}</span><p>${escapeHtml(job.output_directory)}</p></div>
    <div class="big-progress"><div><span>总体进度</span><b>${totalProgress.toFixed(1)}%</b></div><div class="bar"><i style="width:${totalProgress}%"></i></div></div>
    <div class="seed-card"><span>随机种子</span><strong>${job.seed}</strong></div>
    ${renderFeishuSyncPanel(job)}
    ${!terminalStates.has(job.status) ? `<button id="cancelJobBtn" class="button secondary" style="width:100%">取消剩余任务</button>` : ""}
    ${terminalStates.has(job.status) ? `<div class="record-delete-zone">
      <button id="showDeleteJobBtn" class="text-btn danger-text" type="button">删除任务记录</button>
      <div id="deleteJobConfirm" class="record-delete-confirm hidden">
        <div><strong>删除这条任务记录？</strong><p>只会从任务记录中移除，已经生成的视频不会删除。</p></div>
        <div class="record-delete-actions">
          <button id="cancelDeleteJobBtn" class="button secondary small" type="button">取消</button>
          <button id="confirmDeleteJobBtn" class="button danger small" type="button">确认删除</button>
        </div>
      </div>
    </div>` : ""}
    <div class="item-list">${job.items.map(item => {
      const [itemLabel,itemCls] = statusInfo(item.status);
      const selections = Object.entries(item.selections || {})
        .filter(([, asset]) => asset)
        .map(([category, asset]) => `${categoryLabel(category)}：${asset.name}`)
        .join("　·　");
      return `<div class="item-row"><b>${String(item.index).padStart(2,"0")}</b><div><strong>${escapeHtml(item.output_name)}</strong><small class="item-selections">${escapeHtml(selections)}</small><div class="mini-progress" style="margin-top:7px"><i style="width:${item.progress*100}%"></i></div></div><span class="status ${itemCls}">${itemLabel}</span>${item.error ? `<div class="error-text">${escapeHtml(item.error)}</div>` : ""}</div>`;
    }).join("")}</div>`;
  $("#cancelJobBtn")?.addEventListener("click", () => cancelJob(job.id));
  $("#retryFeishuSyncBtn")?.addEventListener("click", () => retryJobFeishuSync(job));
  $("#showDeleteJobBtn")?.addEventListener("click", () => {
    $("#showDeleteJobBtn").classList.add("hidden");
    $("#deleteJobConfirm").classList.remove("hidden");
  });
  $("#cancelDeleteJobBtn")?.addEventListener("click", () => {
    $("#deleteJobConfirm").classList.add("hidden");
    $("#showDeleteJobBtn").classList.remove("hidden");
  });
  $("#confirmDeleteJobBtn")?.addEventListener("click", () => deleteJobRecord(job.id));
}

async function openSliceJob(jobId) {
  $("#jobDrawer").classList.add("open");
  $("#jobDrawer").setAttribute("aria-hidden", "false");
  if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
  try {
    const job = await api(`/timeline/slice-jobs/${jobId}`);
    state.activeJob = job;
    renderSliceJobDetail(job);
  } catch (error) {
    toast(error.message, true);
  }
}

function renderSliceJobDetail(job) {
  state.activeJob = job;
  const [label, cls] = sliceJobStatusInfo(job);
  const progress = Math.max(0, Math.min(100, Number(job.progress || 0) * 100));
  const sourceName = job.source?.name || job.source?.path?.split(/[\\/]/).pop() || "未知视频";
  const phaseNames = { waiting: "等待", probing_audio: "检查音轨", encoding: "编码", encoding_fallback: "软件编码回退", verifying: "校验", committing: "入库", done: "完成" };
  const encoderLabel = encoder => encoder === "h264_videotoolbox" ? "VideoToolbox" : (encoder === "libx264" ? "libx264" : encoder || "待确定");
  const actualEncoders = (job.encoding?.actual_video_encoders || []).map(encoderLabel);
  const plannedEncoder = encoderLabel(job.encoding?.planned_video_encoder);
  const encoderSummary = actualEncoders.length ? actualEncoders.join(" + ") : plannedEncoder;
  const fallbackCount = Number(job.encoding?.fallback_count || 0);
  $("#jobDetail").innerHTML = `<div class="job-detail-header"><p class="eyebrow">SLICE #${escapeHtml(job.short_id)}</p><h2>${escapeHtml(sourceName)}</h2><span class="status ${cls}">${label}</span><p>${escapeHtml(job.source?.path || "")}</p></div>
    <div class="big-progress"><div><span>总体进度</span><b>${progress.toFixed(1)}%</b></div><div class="bar"><i style="width:${progress}%"></i></div></div>
    <div class="slice-job-stats"><span>输出 <b>${job.output_unit_count}</b></span><span>成功 <b>${job.success_count}</b></span><span>失败 <b>${job.failure_count}</b></span><span>取消 <b>${job.cancelled_count || 0}</b></span></div>
    <div class="slice-encoder-summary"><span>视频编码</span><b>${escapeHtml(encoderSummary)}</b>${fallbackCount ? `<small>${fallbackCount} 个条目已自动回退到 libx264</small>` : ""}</div>
    ${job.manifest_sync_error ? `<div class="warning-box">切片清单同步失败：${escapeHtml(job.manifest_sync_error)}</div>` : ""}
    ${!terminalStates.has(job.status) ? `<button id="cancelSliceJobBtn" class="button secondary" style="width:100%">取消切片任务</button>` : ""}
    ${["partial_failed", "failed", "cancelled", "interrupted"].includes(job.status) ? `<button id="retrySliceJobBtn" class="button primary" style="width:100%;margin-top:8px">${job.status === "interrupted" ? "继续未完成项" : "重试未完成项"}</button>` : ""}
    ${terminalStates.has(job.status) ? `<div class="record-delete-zone"><button id="deleteSliceJobBtn" class="text-btn danger-text" type="button">删除切片任务记录</button></div>` : ""}
    <div class="item-list">${job.items.map(item => {
      const [itemLabel, itemCls] = statusInfo(item.status);
      const parts = (item.parts || []).map(part => `#${part.segment_index} ${Number(part.start_seconds).toFixed(2)}–${Number(part.end_seconds).toFixed(2)}s`).join(" · ");
      const itemProgress = Number(item.progress || 0) * 100;
      const itemEncoder = encoderLabel(item.actual_video_encoder || item.planned_video_encoder);
      return `<div class="item-row"><b>${String(item.unit_index).padStart(2, "0")}</b><div><strong>${escapeHtml(categoryLabelForTimeline(item.category))}</strong><small class="item-selections">${escapeHtml(parts)} · ${phaseNames[item.phase] || item.phase} · ${escapeHtml(itemEncoder)}</small><div class="mini-progress" style="margin-top:7px"><i style="width:${itemProgress}%"></i></div></div><span class="status ${itemCls}">${itemLabel}</span>${item.error ? `<div class="error-text">${escapeHtml(item.error)}</div>` : ""}</div>`;
    }).join("")}</div>`;
  $("#cancelSliceJobBtn")?.addEventListener("click", () => cancelSliceJob(job.id));
  $("#retrySliceJobBtn")?.addEventListener("click", () => retrySliceJob(job));
  $("#deleteSliceJobBtn")?.addEventListener("click", () => deleteSliceJobRecord(job.id));
}

async function cancelSliceJob(id) {
  try {
    const job = await api(`/timeline/slice-jobs/${id}/cancel`, { method: "POST" });
    renderSliceJobDetail(job);
    await loadSliceJobs();
    toast(job.status === "cancelled" ? "切片任务已取消" : "正在取消切片任务");
  } catch (error) { toast(error.message, true); }
}

async function deleteSliceJobRecord(id) {
  try {
    await api(`/timeline/slice-jobs/${id}`, { method: "DELETE" });
    state.activeJob = null;
    closeDrawer();
    await loadSliceJobs();
    toast("切片任务记录已删除，入库视频和清单已保留");
  } catch (error) { toast(error.message, true); }
}

async function retrySliceJob(job) {
  const endpoint = job.status === "interrupted" ? "resume" : "retry-failed";
  try {
    const updated = await api(`/timeline/slice-jobs/${job.id}/${endpoint}`, { method: "POST" });
    await loadSliceJobs();
    renderSliceJobDetail(await api(`/timeline/slice-jobs/${updated.id}`));
    toast("已重新加入切片队列");
  } catch (error) { toast(error.message, true); }
}

async function cancelJob(id) {
  try { await api(`/jobs/${id}/cancel`, { method: "POST" }); toast("正在取消任务"); } catch (error) { toast(error.message, true); }
}

async function deleteJobRecord(id) {
  const button = $("#confirmDeleteJobBtn");
  if (button) { button.disabled = true; button.textContent = "删除中…"; }
  try {
    if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
    await api(`/jobs/${id}`, { method: "DELETE" });
    state.activeJob = null;
    closeDrawer();
    await loadJobs();
    toast("任务记录已删除，生成的视频已保留");
  } catch (error) {
    toast(error.message, true);
    if (button) { button.disabled = false; button.textContent = "确认删除"; }
  }
}

function showDeleteAllJobsConfirm() {
  if (!state.jobs.length) return;
  $("#deleteAllJobsCount").textContent = String(state.jobs.length);
  $("#deleteAllJobsConfirm").classList.remove("hidden");
}

function hideDeleteAllJobsConfirm() {
  $("#deleteAllJobsConfirm").classList.add("hidden");
}

async function deleteAllJobRecords() {
  const button = $("#confirmDeleteAllJobsBtn");
  button.disabled = true;
  button.textContent = "删除中…";
  try {
    const result = await api("/jobs", { method: "DELETE" });
    hideDeleteAllJobsConfirm();
    await loadJobs();
    toast(`已删除 ${result.deleted_count} 条任务记录，生成的视频已保留`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "确认全部删除";
  }
}
function closeDrawer() {
  $("#jobDrawer").classList.remove("open");
  state.activeJob = null;
  if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
  if (state.feishuSyncTimer) clearTimeout(state.feishuSyncTimer);
  state.feishuSyncTimer = null;
  state.feishuSync = null;
}

function openNewConfig() {
  $("#newConfigIdInput").value = "";
  $("#newConfigNameInput").value = "";
  $("#newLibraryFolderInput").value = "";
  $("#newLibraryFolderInput").dataset.automatic = "true";
  $("#newLibraryParentInput").value = "";
  $("#newWorkflowType").value = "taobao_flash";
  updateLibraryCreatePreview();
  $("#newConfigModal").classList.add("open");
  $("#newConfigModal").setAttribute("aria-hidden", "false");
  requestAnimationFrame(() => $("#newConfigIdInput").focus());
}

function updateLibraryCreatePreview() {
  const newId = $("#newConfigIdInput").value.trim();
  const newName = $("#newConfigNameInput").value.trim();
  const folder = $("#newLibraryFolderInput").value.trim();
  const parent = normalizePathInput($("#newLibraryParentInput").value);
  const workflowType = $("#newWorkflowType").value;
  const complete = /^[a-z0-9][a-z0-9-]*$/.test(newId) && newName && folder && parent;
  const finalPath = libraryTargetPath(parent, folder);
  const usesSelectedDirectory = Boolean(finalPath) && finalPath === parent.replace(/[\\/]+$/, "");
  $("#createConfigBtn").disabled = !complete;
  $("#newLibraryFinalPath").textContent = finalPath || "请先选择保存位置";
  const directorySummary = workflowType === "generic"
    ? "将创建：原始视频 / 视频库 / 未归类 / 风险提示语图片 / 成片输出 / 工作记录；具体视频库由你随后添加"
    : "将创建：原始视频 / 切片素材 / 前贴 / 引子 / 利益点 / 结尾 / 尾帧 / 未归类 / 风险提示语图片 / 成片输出 / 工作记录";
  $("#newLibraryDirectorySummary").textContent = usesSelectedDirectory
    ? `将直接使用所选空文件夹，不再创建同名子目录；${directorySummary}`
    : directorySummary;
}

async function chooseLibraryParent() {
  const button = $("#chooseLibraryParentBtn");
  button.disabled = true;
  button.textContent = "等待选择…";
  try {
    const result = await api("/system/directory-picker", { method: "POST" });
    if (!result.cancelled && result.path) {
      $("#newLibraryParentInput").value = result.path;
      updateLibraryCreatePreview();
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "选择位置";
  }
}

function closeNewConfig() {
  $("#newConfigModal").classList.remove("open");
  $("#newConfigModal").setAttribute("aria-hidden", "true");
}

async function createConfig() {
  const newId = $("#newConfigIdInput").value.trim();
  const newName = $("#newConfigNameInput").value.trim();
  const folderName = $("#newLibraryFolderInput").value.trim();
  const parentDirectory = normalizePathField($("#newLibraryParentInput"));
  const workflowType = $("#newWorkflowType").value;
  if (!/^[a-z0-9][a-z0-9-]*$/.test(newId)) {
    toast("配置 ID 只能使用小写英文、数字和短横线", true);
    $("#newConfigIdInput").focus();
    return;
  }
  if (!newName) {
    toast("请填写配置名称", true);
    $("#newConfigNameInput").focus();
    return;
  }
  if (!folderName) {
    toast("请填写视频库文件夹名", true);
    $("#newLibraryFolderInput").focus();
    return;
  }
  if (!parentDirectory) {
    toast("请先选择视频库的保存位置", true);
    return;
  }
  const button = $("#createConfigBtn");
  button.disabled = true; button.textContent = "正在创建目录…";
  try {
    const payload = {
      new_id: newId,
      new_name: newName,
      parent_directory: parentDirectory,
      folder_name: folderName,
      workflow_type: workflowType,
      client_request_id: clientRequestId(),
    };
    await api("/libraries/preflight", {
      method: "POST",
      body: JSON.stringify({ parent_directory: parentDirectory, folder_name: folderName, workflow_type: workflowType }),
    });
    await api("/libraries", { method: "POST", body: JSON.stringify(payload) });
    closeNewConfig();
    await loadConfigs(newId);
    toast(workflowType === "generic" ? "通用项目库已创建，请添加第一个视频库" : "淘宝闪购视频库和全部文件夹已创建");
    openConfig();
  } catch (error) { toast(error.message, true); }
  finally { button.textContent = "创建视频库"; updateLibraryCreatePreview(); }
}

function showDeleteConfigConfirm() {
  if (!state.config) return;
  $("#deleteConfigName").textContent = state.config.name;
  $("#deleteConfigConfirm").classList.remove("hidden");
}

function hideDeleteConfigConfirm() {
  $("#deleteConfigConfirm").classList.add("hidden");
}

async function deleteConfig() {
  if (!state.configId) return;
  const deletedId = state.configId;
  const button = $("#confirmDeleteConfigBtn");
  button.disabled = true; button.textContent = "删除中…";
  try {
    const deleted = await withTemporaryConfigLease((lease, configHash) => api(`/configs/${deletedId}`, {
      method: "DELETE",
      headers: configLeaseHeaders(lease, configHash),
    }));
    if (!deleted) return;
    try { localStorage.removeItem(`${configUiStoragePrefix}${deletedId}`); } catch (_) {}
    state.configId = null;
    hideDeleteConfigConfirm();
    await loadConfigs();
    toast("配置已删除，并已保留可恢复备份");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "确认删除"; }
}

async function openConfig() {
  if (!state.config) return;
  const button = $("#editConfigBtn");
  button.disabled = true;
  try {
    const acquired = await acquireConfigLease(state.configId);
    if (!acquired) return;
    installConfigLease(state.configId, acquired);
    state.config = acquired.config;
    state.configDraft = structuredClone(acquired.config);
    state.configHash = acquired.content_hash;
    state.yaml = acquired.yaml_text;
    state.feishuSettings = await api("/integrations/feishu/settings");
    state.feishuSettingsDraft = structuredClone(state.feishuSettings);
    state.feishuSecretDraft = "";
    state.feishuConnection = null;
    state.feishuConnectionError = null;
    $("#yamlEditor").value = state.yaml;
    restoreConfigUiPreferences();
    document.body.classList.add("config-modal-open");
    $("#configModal").classList.add("open");
    $("#configModal").setAttribute("aria-hidden","false");
    requestAnimationFrame(restoreConfigEditorScroll);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}
function configEditorIsDirty() {
  if (!state.configDraft || !state.config) return false;
  if (feishuCredentialsDirty()) return true;
  if (state.configMode === "yaml") return $("#yamlEditor").value !== state.yaml;
  try { return JSON.stringify(currentStructuredDraft()) !== JSON.stringify(state.config); }
  catch (_) { return true; }
}
async function closeConfig({ skipConfirm = false, releaseLease = true } = {}) {
  if (!skipConfirm && configEditorIsDirty() && !window.confirm("放弃未保存的修改并释放配置吗？")) return false;
  saveConfigUiPreferences();
  const audio = $("#loudnessPreviewAudio");
  if (audio) { audio.pause(); audio.removeAttribute("src"); audio.load(); }
  state.previewAudioCleanup?.();
  state.previewAudioCleanup = null;
  state.feishuSecretDraft = "";
  state.feishuConnection = null;
  state.feishuConnectionError = null;
  $("#configModal").classList.remove("open");
  $("#configModal").setAttribute("aria-hidden","true");
  document.body.classList.remove("config-modal-open");
  if (releaseLease) await releaseConfigLease();
  return true;
}

function setConfigMode(mode) {
  if (
    state.configMode === "advanced"
    && mode !== "advanced"
    && state.configDraft
    && $("#visualConfigEditor").children.length
  ) {
    state.configDraft = collectVisualConfig();
  }
  state.configMode = mode;
  if (mode === "simple") renderSimpleConfig();
  if (mode === "advanced") {
    renderVisualConfig();
    applyAdvancedSectionPreferences(readConfigUiPreferences());
  }
  $$(".config-mode-tab").forEach(button => button.classList.toggle("active", button.dataset.configMode === mode));
  $("#simpleConfigEditor").classList.toggle("hidden", mode !== "simple");
  $("#visualConfigEditor").classList.toggle("hidden", mode !== "advanced");
  $("#yamlConfigEditor").classList.toggle("hidden", mode !== "yaml");
  if ($("#configModal").classList.contains("open")) saveConfigUiPreferences();
}

function configUiStorageKey() {
  return `${configUiStoragePrefix}${state.configId}`;
}

function readConfigUiPreferences() {
  try {
    return JSON.parse(localStorage.getItem(configUiStorageKey()) || "{}") || {};
  } catch (_) {
    return {};
  }
}

function saveConfigUiPreferences() {
  if (!state.configId) return;
  const sections = {};
  $$("#visualConfigEditor details[data-config-section]").forEach(section => {
    sections[section.dataset.configSection] = section.open;
  });
  const preferences = {
    sections,
    simpleScrollTop: $("#simpleConfigEditor")?.scrollTop || 0,
    visualScrollTop: $("#visualConfigEditor")?.scrollTop || 0,
    yamlScrollTop: $("#yamlEditor")?.scrollTop || 0,
  };
  try { localStorage.setItem(configUiStorageKey(), JSON.stringify(preferences)); } catch (_) {}
}

function restoreConfigUiPreferences() {
  setConfigMode("simple");
}

function applyAdvancedSectionPreferences(preferences) {
  $$("#visualConfigEditor details[data-config-section]").forEach(section => {
    const saved = preferences.sections?.[section.dataset.configSection];
    if (typeof saved === "boolean") section.open = saved;
    section.addEventListener("toggle", saveConfigUiPreferences);
  });
}

function restoreConfigEditorScroll() {
  const preferences = readConfigUiPreferences();
  if ($("#simpleConfigEditor")) $("#simpleConfigEditor").scrollTop = Number(preferences.simpleScrollTop || 0);
  if ($("#visualConfigEditor")) $("#visualConfigEditor").scrollTop = Number(preferences.visualScrollTop || 0);
  if ($("#yamlEditor")) $("#yamlEditor").scrollTop = Number(preferences.yamlScrollTop || 0);
}

function simpleOutputPreset(output) {
  if (output.width === 720 && output.height === 1280) return "portrait";
  if (output.width === 1280 && output.height === 720) return "landscape";
  if (output.width === 1080 && output.height === 1080) return "square";
  return "custom";
}

function simpleQualityPreset(output) {
  if (["ultrafast", "veryfast"].includes(output.video_preset) || output.crf >= 22) return "fast";
  if (["slow", "slower"].includes(output.video_preset) || output.crf <= 16) return "high";
  return "recommended";
}

function ensureVisualDedup(config) {
  if (!config.visual_dedup) config.visual_dedup = {};
  const visual = config.visual_dedup;
  if (typeof visual.enabled !== "boolean") visual.enabled = false;
  visual.foreground ||= { scale: 0.9 };
  visual.background ||= { mode: "gaussian_blur", sigma: 20, steps: 2, brightness: 0 };
  visual.border_overlay ||= {
    mode: "disabled",
    file: "",
    media_kind: "auto",
    scale_mode: "exact",
    playback: "loop",
    opacity: 1,
    alpha_mode: "straight",
  };
  return visual;
}

function simpleVisualDedupPreset(visual) {
  const presets = {
    light: [0.94, 12, 2, 0],
    standard: [0.9, 20, 2, 0],
    strong: [0.86, 28, 2, 0],
  };
  const current = [
    Number(visual.foreground.scale),
    Number(visual.background.sigma),
    Number(visual.background.steps),
    Number(visual.background.brightness),
  ];
  return Object.entries(presets).find(([, values]) =>
    values.every((value, index) => Math.abs(value - current[index]) < 1e-9)
  )?.[0] || "custom";
}

function simpleOverlayDirectory() {
  const root = state.library?.root_path || state.configDraft?.source_root || "";
  return `${String(root).replace(/\/+$/, "")}/风险提示语图片`;
}

function simpleChoiceButtons(name, choices, selected) {
  return `<div class="simple-choice-row" role="group">${choices.map(([value, title, note, disabled = false]) => `
    <button type="button" class="simple-choice ${selected === value ? "active" : ""}" data-simple-choice="${name}" data-simple-value="${value}" ${disabled ? "disabled" : ""}>
      <strong>${title}</strong>${note ? `<small>${note}</small>` : ""}
    </button>`).join("")}</div>`;
}

function ensureOutputNaming(config) {
  if (!config.output.naming) {
    config.output.naming = {
      enabled: false,
      product: "",
      benefit: "",
      sequence_start: 1,
      template: "{product}-{benefit}-{talents}-{restriction_date}-{sequence}.mp4",
      source_metadata: {
        categories: ["pool_*"],
        strip_smartstitch_suffix: true,
        pattern: "^(?P<source_index>\\d+)_(?P<talent>[^-]+)-(?P<source_title>.+)-(?P<restriction_date>\\d{4}-\\d{2}-\\d{2})$",
        restriction_date_formats: ["%Y-%m-%d"],
        on_unmatched: "error",
      },
      talent: { merge: "ordered_unique", separator: "+" },
      restriction_date: { merge: "earliest", output_format: "%Y%m%d" },
      duplicate_suffix: "-{serial:02d}",
    };
  }
  return config.output.naming;
}

function ensureFeishuBaseSync(config) {
  if (!config.output.feishu_base_sync) {
    config.output.feishu_base_sync = {
      enabled: false,
      base_url: "",
      table_id: "",
      trigger: "job_terminal",
      row_scope: "all_items",
      write_mode: "upsert",
    };
  }
  return config.output.feishu_base_sync;
}

function feishuCredentialsDirty() {
  return Boolean(state.feishuSecretDraft)
    || state.feishuSettingsDraft.app_id !== state.feishuSettings.app_id;
}

function feishuTableOptions(selectedId) {
  const tables = state.feishuConnection?.tables || [];
  if (!tables.length) {
    return selectedId
      ? `<option value="${escapeHtml(selectedId)}" selected>已选择 ${escapeHtml(selectedId)}</option>`
      : '<option value="">请先测试连接</option>';
  }
  return ['<option value="">请选择数据表</option>', ...tables.map(table =>
    `<option value="${escapeHtml(table.table_id)}" ${table.table_id === selectedId ? "selected" : ""}>${escapeHtml(table.name)} · ${escapeHtml(table.table_id)}</option>`
  )].join("");
}

function feishuConnectionStatus() {
  if (!state.feishuConnection) return "填写后点击测试连接";
  return `已连接，识别到 ${state.feishuConnection.tables.length} 个数据表`;
}

function feishuConnectionErrorMarkup() {
  const error = state.feishuConnectionError;
  if (!error) return "";
  const link = error.console_url
    ? `<a href="${escapeHtml(error.console_url)}" target="_blank" rel="noopener noreferrer">打开飞书权限配置</a>`
    : "";
  return `<div class="feishu-connection-error"><span>${escapeHtml(error.message || "飞书连接失败")}</span>${link}</div>`;
}

async function testFeishuConnection(prefix) {
  if (state.configMode === "advanced") state.configDraft = collectVisualConfig();
  const appId = $(`#${prefix}FeishuAppId`)?.value.trim() || state.feishuSettingsDraft.app_id;
  const secret = $(`#${prefix}FeishuAppSecret`)?.value.trim() || state.feishuSecretDraft;
  const url = $(`#${prefix}FeishuUrl`)?.value.trim() || ensureFeishuBaseSync(state.configDraft).base_url;
  if (!appId || (!secret && !state.feishuSettings.app_secret_configured)) {
    toast("请先填写 App ID 和 App Secret", true);
    return;
  }
  if (!url) {
    toast("请先填写飞书多维表格链接", true);
    return;
  }
  const button = $(`#${prefix}TestFeishuBtn`);
  if (button) { button.disabled = true; button.textContent = "正在连接…"; }
  try {
    state.feishuSettingsDraft.app_id = appId;
    state.feishuSecretDraft = secret;
    const sync = ensureFeishuBaseSync(state.configDraft);
    sync.base_url = url;
    const result = await api("/integrations/feishu/test", {
      method: "POST",
      body: JSON.stringify({
        base_url: url,
        app_id: appId,
        app_secret: secret || null,
      }),
    });
    state.feishuConnection = result;
    state.feishuConnectionError = null;
    if (!sync.table_id || !result.tables.some(table => table.table_id === sync.table_id)) {
      sync.table_id = result.selected_table_id || "";
    }
    const scrollTop = configEditorScrollTop();
    if (state.configMode === "advanced") renderVisualConfig();
    else renderSimpleConfig();
    restoreActiveConfigScroll(scrollTop);
    toast("飞书多维表格连接成功");
  } catch (error) {
    state.feishuConnection = null;
    state.feishuConnectionError = error.detail || { message: error.message };
    const scrollTop = configEditorScrollTop();
    if (state.configMode === "advanced") renderVisualConfig();
    else renderSimpleConfig();
    restoreActiveConfigScroll(scrollTop);
    toast(error.message, true);
  } finally {
    const current = $(`#${prefix}TestFeishuBtn`);
    if (current) { current.disabled = false; current.textContent = "测试连接"; }
  }
}

function bindFeishuControls(prefix) {
  const sync = ensureFeishuBaseSync(state.configDraft);
  $(`#${prefix}FeishuEnabled`)?.addEventListener("change", event => {
    sync.enabled = event.target.checked;
    const scrollTop = configEditorScrollTop();
    if (state.configMode === "advanced") renderVisualConfig();
    else renderSimpleConfig();
    restoreActiveConfigScroll(scrollTop);
  });
  $(`#${prefix}FeishuAppId`)?.addEventListener("input", event => {
    state.feishuSettingsDraft.app_id = event.target.value;
  });
  $(`#${prefix}FeishuAppSecret`)?.addEventListener("input", event => {
    state.feishuSecretDraft = event.target.value;
  });
  $(`#${prefix}FeishuUrl`)?.addEventListener("input", event => {
    sync.base_url = event.target.value.trim();
    sync.table_id = "";
    state.feishuConnection = null;
    state.feishuConnectionError = null;
  });
  $(`#${prefix}FeishuTable`)?.addEventListener("change", event => {
    sync.table_id = event.target.value;
  });
  $(`#${prefix}TestFeishuBtn`)?.addEventListener("click", () => testFeishuConnection(prefix));
}

function namingCategoryMatches(naming, category) {
  return naming.source_metadata.categories.some(pattern => pattern === "pool_*" || pattern === category);
}

function parseNamingDate(value, formats) {
  for (const format of formats) {
    if (format === "%Y-%m-%d" && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
      const compact = value.replaceAll("-", "");
      const parsed = parseNamingDate(compact, ["%Y%m%d"]);
      if (parsed) return parsed;
    }
    if (format === "%Y%m%d" && /^\d{8}$/.test(value)) {
      const year = Number(value.slice(0, 4));
      const month = Number(value.slice(4, 6));
      const day = Number(value.slice(6, 8));
      const date = new Date(Date.UTC(year, month - 1, day));
      if (date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day) return value;
    }
    if (format === "%y%m%d" && /^\d{6}$/.test(value)) {
      const expanded = `20${value}`;
      const parsed = parseNamingDate(expanded, ["%Y%m%d"]);
      if (parsed) return parsed;
    }
  }
  return null;
}

function parseNamingAsset(config, asset) {
  const naming = ensureOutputNaming(config);
  let stem = asset.name.replace(/\.[^.]+$/, "");
  if (naming.source_metadata.strip_smartstitch_suffix) stem = stem.split("__", 1)[0];
  const browserPattern = naming.source_metadata.pattern.replaceAll("(?P<", "(?<");
  const match = stem.match(new RegExp(browserPattern));
  const talent = match?.groups?.talent?.trim();
  const restrictionDate = match?.groups?.restriction_date
    ? parseNamingDate(match.groups.restriction_date, naming.source_metadata.restriction_date_formats)
    : null;
  if (!talent || !restrictionDate) throw new Error("无法识别达人名和限制日期");
  return { talent, restrictionDate };
}

function namingExample(config) {
  const naming = ensureOutputNaming(config);
  const talents = [];
  const dates = [];
  for (const category of config.timeline) {
    if (!namingCategoryMatches(naming, category)) continue;
    const asset = (state.scan?.assets?.[category] || []).find(item => item.enabled && item.valid && Number(item.weight) > 0);
    if (!asset) continue;
    try {
      const parsed = parseNamingAsset(config, asset);
      if (!talents.includes(parsed.talent)) talents.push(parsed.talent);
      dates.push(parsed.restrictionDate);
    } catch (_) {}
  }
  const values = {
    product: naming.product || "产品",
    benefit: naming.benefit || "利益点",
    talents: talents.join(naming.talent.separator) || "达人名",
    restriction_date: dates.length ? dates.sort()[0] : "限制日期",
    sequence: String(naming.sequence_start || 1),
  };
  return naming.template.replace(/\{(product|benefit|talents|restriction_date|sequence)(?::[^}]*)?\}/g, (_, key) => values[key]);
}

function renderSimpleConfig() {
  const config = state.configDraft;
  if (!config) return;
  const generic = config.workflow_type === "generic";
  const categories = config.timeline.filter(category => config.sources[category]);
  const benefitCategories = categories.filter(isBenefitCategory);
  const sourceCards = categories.map((category, index) => {
    const group = config.sources[category];
    const count = state.scan?.assets?.[category]?.length ?? 0;
    const label = group.label?.trim() || categoryLabel(category);
    const folderName = String(group.directory || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || group.directory;
    const canDelete = generic || (isBenefitCategory(category) && benefitCategories.length > 1);
    const canMove = generic || isBenefitCategory(category);
    const draggable = generic || isBenefitCategory(category);
    const movableCategories = generic ? categories : benefitCategories;
    const movableIndex = movableCategories.indexOf(category);
    return `
      <article class="simple-source-card ${group.mode === "disabled" ? "is-disabled" : ""}" data-simple-source-card="${escapeHtml(category)}" ${draggable ? 'draggable="true"' : ""}>
        <div class="simple-source-step"><span>${String(index + 1).padStart(2, "0")}</span>${draggable ? '<i title="拖动改变顺序">⠿</i>' : ""}</div>
        <div class="simple-source-main">
          ${generic
            ? `<input class="simple-source-name" data-simple-source-name="${escapeHtml(category)}" value="${escapeHtml(label)}" aria-label="视频库名称">`
            : `<strong>${escapeHtml(label)}</strong>`}
          <div class="simple-source-meta"><span class="${count ? "has-assets" : ""}">${count ? `${count} 个素材可用` : "还没有素材"}</span></div>
        </div>
        <button type="button" class="text-btn simple-source-directory" data-open-source-directory="${escapeHtml(category)}" title="打开文件夹：${escapeHtml(group.directory)}">${escapeHtml(folderName)} ↗</button>
        <label class="simple-source-switch">
          <span>参与拼接</span>
          <input class="switch-input" type="checkbox" data-simple-source-enabled="${escapeHtml(category)}" data-active-mode="${group.mode === "disabled" ? "required" : group.mode}" ${group.mode !== "disabled" ? "checked" : ""}>
        </label>
        ${canMove ? `<div class="simple-source-actions">
          <button type="button" class="text-btn" data-simple-source-action="up" data-simple-source-id="${escapeHtml(category)}" ${movableIndex <= 0 ? "disabled" : ""}>上移</button>
          <button type="button" class="text-btn" data-simple-source-action="down" data-simple-source-id="${escapeHtml(category)}" ${movableIndex === movableCategories.length - 1 ? "disabled" : ""}>下移</button>
          ${canDelete ? `<button type="button" class="text-btn danger-text" data-simple-source-action="delete" data-simple-source-id="${escapeHtml(category)}">删除</button>` : ""}
        </div>` : ""}
      </article>`;
  }).join("");
  const overlay = config.benefit_overlays;
  const overlayEnabled = overlay.mode !== "disabled";
  const overlayAsset = (state.scan?.assets?.benefit_overlay || []).find(asset => asset.valid);
  const overlayName = overlayAsset?.name || (overlay.file ? overlay.file.split("/").pop() : "尚未识别到图片");
  const overlayStatus = overlayEnabled
    ? (overlayAsset ? "已识别并将在生成时叠加" : "已开启，请添加一张图片")
    : "当前关闭，不会叠加到成片";
  const timingChoices = generic
    ? [["full", "整条视频"], ["custom", "指定时间"]]
    : [["full", "整条视频"], ["main", "主要内容"], ["benefits", "利益点部分"], ["custom", "指定时间"]];
  const addButton = generic
    ? `<button id="simpleAddPoolBtn" class="button secondary" type="button" ${categories.length >= 50 ? "disabled" : ""}>＋ 添加视频库</button>`
    : `<button id="simpleAddBenefitBtn" class="button secondary" type="button" ${benefitCategories.length >= 20 ? "disabled" : ""}>＋ 添加利益点</button>`;
  const projectType = generic ? "通用视频拼接" : "淘宝闪购";
  const libraryHealth = !state.library?.managed
    ? "外部素材配置"
    : state.library.health === "healthy" ? "项目目录正常" : "请检查项目目录";
  const outputPreset = simpleOutputPreset(config.output);
  const qualityPreset = simpleQualityPreset(config.output);
  const visual = ensureVisualDedup(config);
  const visualPreset = simpleVisualDedupPreset(visual);
  const visualBorderEnabled = visual.border_overlay.mode !== "disabled";
  const visualBorderAsset = (state.scan?.assets?.visual_border || []).find(asset => asset.valid);
  const visualBorderInvalid = (state.scan?.assets?.visual_border || []).find(asset => !asset.valid);
  const visualBorderName = visualBorderAsset?.name
    || visualBorderInvalid?.name
    || (visual.border_overlay.file ? visual.border_overlay.file.split("/").pop() : "尚未选择边框");
  const visualBorderStatus = visualBorderInvalid
    ? visualBorderInvalid.error
    : visualBorderAsset
      ? (visualBorderAsset.media_type === "video" ? "动态边框可用" : "静态边框可用")
      : visualBorderEnabled ? "已开启，请选择透明边框" : "当前不使用边框";
  const naming = generic ? ensureOutputNaming(config) : null;
  const feishu = ensureFeishuBaseSync(config);
  const namingErrors = generic
    ? (state.scan?.errors || []).filter(error => error.includes("命名") || error.includes("识别达人名"))
    : [];
  const namingCard = generic ? `
    <section class="simple-config-card simple-wide-card simple-naming-card">
      <header><span class="simple-card-number">05</span><div><h3>命名设置</h3><p>填写产品和利益点，达人与限制日期由系统从剧情素材中提取。</p></div></header>
      <div class="simple-card-body simple-naming-card-body">
        <label class="simple-toggle-row compact"><span><b>按业务信息命名</b><small>使用产品-利益点-达人-限制日期生成文件名</small></span><input id="simpleNamingEnabled" class="switch-input" type="checkbox" ${naming.enabled ? "checked" : ""}></label>
        <div id="simpleNamingFields" class="simple-naming-fields ${naming.enabled ? "" : "hidden"}">
          <div class="simple-naming-inputs">
            <label class="simple-large-field"><span>产品</span><input id="simpleNamingProduct" value="${escapeHtml(naming.product)}" placeholder="例如 红果短剧"></label>
            <label class="simple-large-field"><span>利益点</span><input id="simpleNamingBenefit" value="${escapeHtml(naming.benefit)}" placeholder="例如 功能综述"></label>
            <label class="simple-large-field"><span>序号起点</span><input id="simpleNamingSequenceStart" type="number" min="1" max="999999" step="1" value="${escapeHtml(naming.sequence_start || 1)}"></label>
          </div>
          <small class="simple-naming-help">达人和限制日期从抽中的剧情素材自动提取；序号从设定值开始逐条递增。</small>
          <div class="simple-info-strip"><span class="${namingErrors.length ? "warning" : "ok"}">${namingErrors.length ? "命名预检异常" : "文件名预览"}</span><code id="simpleNamingPreview">${escapeHtml(naming.enabled ? namingExample(config) : "将继续使用原文件名模板")}</code>${namingErrors.length ? '<button class="text-btn" type="button" data-open-naming-advanced>查看高级规则 →</button>' : ""}</div>
        </div>
      </div>
    </section>` : "";
  const feishuCardNumber = generic ? "07" : "06";
  const feishuSecretHint = state.feishuSettings.app_secret_configured
    ? "已配置，留空则不修改"
    : "填写企业自建应用 App Secret";
  const feishuCard = `
    <section class="simple-config-card simple-wide-card simple-feishu-card ${feishu.enabled ? "is-accent" : ""}">
      <header><span class="simple-card-number">${feishuCardNumber}</span><div><h3>飞书多维表格同步</h3><p>成片任务结束后，将每条成片作为一条记录写入指定数据表。</p></div><label class="simple-header-switch"><span>${feishu.enabled ? "已启用" : "未启用"}</span><input id="simpleFeishuEnabled" class="switch-input" type="checkbox" ${feishu.enabled ? "checked" : ""}></label></header>
      <div class="simple-card-body simple-feishu-body ${feishu.enabled ? "" : "is-disabled"}">
        <div class="simple-feishu-grid">
          <label class="simple-large-field"><span>App ID <small>全局凭证，所有项目共用</small></span><input id="simpleFeishuAppId" value="${escapeHtml(state.feishuSettingsDraft.app_id)}" placeholder="cli_xxxxxxxxxxxxx" ${feishu.enabled ? "" : "disabled"}></label>
          <label class="simple-large-field"><span>App Secret <small>只允许更换，不会读回原值</small></span><input id="simpleFeishuAppSecret" type="password" value="${escapeHtml(state.feishuSecretDraft)}" placeholder="${escapeHtml(feishuSecretHint)}" autocomplete="new-password" ${feishu.enabled ? "" : "disabled"}></label>
          <label class="simple-large-field simple-feishu-url"><span>多维表格链接</span><input id="simpleFeishuUrl" value="${escapeHtml(feishu.base_url)}" placeholder="支持 /wiki/... 或 /base/... 链接" ${feishu.enabled ? "" : "disabled"}></label>
          <label class="simple-large-field"><span>数据表</span><select id="simpleFeishuTable" ${feishu.enabled ? "" : "disabled"}>${feishuTableOptions(feishu.table_id)}</select></label>
        </div>
        <div class="simple-feishu-actions"><button id="simpleTestFeishuBtn" class="button secondary small" type="button" ${feishu.enabled ? "" : "disabled"}>测试连接</button><span class="${state.feishuConnection ? "ok" : ""}">${escapeHtml(feishuConnectionStatus())}</span><small>Secret 仅保存在本机，不会进入成片任务快照。</small>${feishuConnectionErrorMarkup()}</div>
      </div>
    </section>`;
  const outputChoices = [
    ["portrait", "竖屏", "720 × 1280"],
    ["landscape", "横屏（未完成）", "1280 × 720", true],
    ["square", "方形（未完成）", "1080 × 1080", true],
  ];
  if (outputPreset === "custom") outputChoices.push(["custom", "保留当前", `${config.output.width} × ${config.output.height}`]);

  $("#simpleConfigEditor").innerHTML = `
    <div class="simple-mode-banner">
      <div><strong>简单模式</strong><p>按下面 ${generic ? "7" : "6"} 步完成设置；没有显示的高级参数会原样保留。</p></div>
      <span class="simple-safe-badge">高级参数已保护</span>
      <button class="text-btn" type="button" data-open-advanced>进入高级模式 →</button>
    </div>

    <section class="simple-config-card simple-half-card">
      <header><span class="simple-card-number">01</span><div><h3>项目设置</h3><p>给它一个容易找到的名字。</p></div></header>
      <div class="simple-card-body simple-basic-grid">
        <label class="simple-large-field"><span>项目名称</span><input id="simpleConfigName" value="${escapeHtml(config.name)}"></label>
        <div class="simple-info-strip"><span>${escapeHtml(projectType)}</span><span class="${state.library?.health === "healthy" ? "ok" : "warning"}">${escapeHtml(libraryHealth)}</span></div>
      </div>
    </section>

    <section class="simple-config-card simple-half-card ${overlayEnabled ? "is-accent" : ""}">
      <header><span class="simple-card-number">02</span><div><h3>风险提示语设置</h3><p>提示图会覆盖在成片最上层。</p></div><label class="simple-header-switch"><span>${overlayEnabled ? "显示" : "不显示"}</span><input id="simpleOverlayEnabled" class="switch-input" type="checkbox" ${overlayEnabled ? "checked" : ""}></label></header>
      <div class="simple-card-body">
        <div class="simple-overlay-row">
          <div class="simple-overlay-status ${overlayAsset ? "has-file" : ""}"><i></i><div><strong>${escapeHtml(overlayName)}</strong><span>${escapeHtml(overlayStatus)}</span></div></div>
          <div class="simple-overlay-actions">
            <label class="button secondary ${state.library?.managed ? "" : "disabled"}">${overlayAsset ? "更换图片" : "选择图片"}<input id="simpleOverlayFile" type="file" accept=".png,.jpg,.jpeg,.webp,.bmp" ${state.library?.managed ? "" : "disabled"}></label>
          </div>
        </div>
        <div class="simple-timing-settings ${overlayEnabled ? "" : "hidden"}">
          <div class="simple-setting-label">显示在哪一段？</div>
          ${simpleChoiceButtons("timing", timingChoices.map(([value, label]) => [value, label, ""]), overlay.timing.scope)}
          <div class="simple-inline-settings ${overlay.timing.scope === "custom" ? "" : "hidden"}">
            <label><span>开始时间（秒）</span><input id="simpleOverlayStart" type="number" min="0" step="0.1" value="${overlay.timing.start_seconds}"></label>
            <label><span>结束时间（秒）</span><input id="simpleOverlayEnd" type="number" min="0" step="0.1" value="${overlay.timing.end_seconds ?? ""}" placeholder="留空到片尾"></label>
          </div>
        </div>
      </div>
    </section>

    <section class="simple-config-card simple-wide-card">
      <header><span class="simple-card-number">03</span><div><h3>拼接顺序</h3><p>${generic ? "列表从上到下，就是成片从头到尾。" : "淘宝闪购主流程保持固定，利益点可以增删和排序。"}</p></div></header>
      <div class="simple-card-body">
        <div class="simple-logic-strip"><span>每个启用的视频库随机取 1 条</span><i>→</i><span>按下方顺序拼接</span><i>→</i><strong>输出成片</strong></div>
        <div class="simple-source-list">${sourceCards || '<div class="simple-empty-state">还没有视频库。添加第一个视频库后即可开始。</div>'}</div>
        <div class="simple-card-footer">${addButton}<small>拖动卡片或点击上下移动，编号会自动更新</small></div>
      </div>
    </section>

    <section class="simple-config-card simple-wide-card ${visual.enabled ? "is-accent" : ""}">
      <header><span class="simple-card-number">04</span><div><h3>视觉去重</h3><p>缩小清晰画面，以同一画面的模糊版本补满四周，并可叠加透明边框。</p></div><label class="simple-header-switch"><span>${visual.enabled ? "已启用" : "未启用"}</span><input id="simpleVisualDedupEnabled" class="switch-input" type="checkbox" ${visual.enabled ? "checked" : ""}></label></header>
      <div class="simple-card-body simple-visual-dedup-body ${visual.enabled ? "" : "is-disabled"}">
        <div class="simple-setting-group"><div class="simple-setting-label">效果强度</div>${simpleChoiceButtons("visualDedup", [
          ["light", "轻度", "保留更多主画面"],
          ["standard", "标准", "推荐"],
          ["strong", "强化", "边框区域更明显"],
          ...(visualPreset === "custom" ? [["custom", "自定义", "由高级模式设置", true]] : []),
        ], visualPreset)}</div>
        <label class="simple-toggle-row compact"><span><b>使用透明边框</b><small>边框位于主画面之上，风险提示语之下</small></span><input id="simpleVisualBorderEnabled" class="switch-input" type="checkbox" ${visualBorderEnabled ? "checked" : ""}></label>
        <div class="simple-overlay-row ${visualBorderEnabled ? "" : "hidden"}">
          <div class="simple-overlay-status ${visualBorderAsset ? "has-file" : ""}"><i></i><div><strong>${escapeHtml(visualBorderName)}</strong><span>${escapeHtml(visualBorderStatus)}</span></div></div>
          <div class="simple-overlay-actions"><label class="button secondary ${state.library?.managed ? "" : "disabled"}">${visualBorderAsset ? "更换边框" : "选择边框"}<input id="simpleVisualBorderFile" type="file" accept=".mov,.png,.webp" ${state.library?.managed ? "" : "disabled"}></label></div>
        </div>
      </div>
    </section>

    ${namingCard}

    <section class="simple-config-card simple-wide-card">
      <header><span class="simple-card-number">${generic ? "06" : "05"}</span><div><h3>输出设置</h3><p>选择常用方案即可，编码和码率由系统自动处理。</p></div></header>
      <div class="simple-card-body simple-finish-settings">
        <div class="simple-setting-group"><div class="simple-setting-label">画面方向</div>${simpleChoiceButtons("output", outputChoices, outputPreset)}</div>
        <div class="simple-setting-group"><div class="simple-setting-label">生成速度与画质</div>${simpleChoiceButtons("quality", [["fast", "快速生成", "速度优先"], ["recommended", "清晰画质", "推荐"], ["high", "高清优先", "耗时更长"]], qualityPreset)}</div>
        <div class="simple-setting-group"><div class="simple-setting-label">组合重复规则</div>${simpleChoiceButtons("duplicate", [["allow", "允许重复", "保持素材权重"], ["best_effort", "尽量不重复", "推荐"], ["strict", "完全不重复", "不足时停止"]], config.randomization.duplicate_policy)}</div>
        <label class="simple-toggle-row compact"><span><b>自动均衡音量</b><small>减少不同素材之间忽大忽小的音量差</small></span><input id="simpleLoudnessEnabled" class="switch-input" type="checkbox" ${config.output.loudness?.enabled ? "checked" : ""}></label>
      </div>
    </section>

    ${feishuCard}`;
  bindSimpleConfigControls();
}

function mutateSimpleConfig(mutator) {
  mutator(state.configDraft);
  renderSimpleConfig();
}

function moveSimpleSource(category, action) {
  mutateSimpleConfig(config => {
    const movable = config.workflow_type === "generic"
      ? config.timeline
      : config.timeline.filter(isBenefitCategory);
    const position = movable.indexOf(category);
    const other = action === "up" ? movable[position - 1] : movable[position + 1];
    if (!other) return;
    const currentIndex = config.timeline.indexOf(category);
    const otherIndex = config.timeline.indexOf(other);
    [config.timeline[currentIndex], config.timeline[otherIndex]] = [config.timeline[otherIndex], config.timeline[currentIndex]];
  });
}

function bindSimpleConfigControls() {
  bindFeishuControls("simple");
  $$('[data-open-advanced]').forEach(button => button.addEventListener("click", () => setConfigMode("advanced")));
  $$('[data-open-naming-advanced]').forEach(button => button.addEventListener("click", () => {
    setConfigMode("advanced");
    requestAnimationFrame(() => {
      const section = $('[data-config-section="output-naming"]');
      if (!section) return;
      section.open = true;
      section.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }));
  $("#simpleConfigName")?.addEventListener("input", event => { state.configDraft.name = event.target.value; });
  $("#simpleNamingEnabled")?.addEventListener("change", event => {
    ensureOutputNaming(state.configDraft).enabled = event.target.checked;
    renderSimpleConfig();
  });
  $("#simpleNamingProduct")?.addEventListener("input", event => {
    ensureOutputNaming(state.configDraft).product = event.target.value;
    const preview = $("#simpleNamingPreview");
    if (preview) preview.textContent = namingExample(state.configDraft);
  });
  $("#simpleNamingBenefit")?.addEventListener("input", event => {
    ensureOutputNaming(state.configDraft).benefit = event.target.value;
    const preview = $("#simpleNamingPreview");
    if (preview) preview.textContent = namingExample(state.configDraft);
  });
  $("#simpleNamingSequenceStart")?.addEventListener("input", event => {
    const value = Number(event.target.value);
    if (Number.isInteger(value) && value >= 1 && value <= 999999) {
      ensureOutputNaming(state.configDraft).sequence_start = value;
    }
    const preview = $("#simpleNamingPreview");
    if (preview) preview.textContent = namingExample(state.configDraft);
  });
  $("#simpleNamingSequenceStart")?.addEventListener("change", event => {
    const value = Number(event.target.value);
    const normalized = Number.isInteger(value) && value >= 1 && value <= 999999 ? value : 1;
    ensureOutputNaming(state.configDraft).sequence_start = normalized;
    event.target.value = String(normalized);
    const preview = $("#simpleNamingPreview");
    if (preview) preview.textContent = namingExample(state.configDraft);
  });
  $$('[data-simple-source-name]').forEach(input => input.addEventListener("input", () => {
    state.configDraft.sources[input.dataset.simpleSourceName].label = input.value;
  }));
  $$('[data-simple-source-enabled]').forEach(input => input.addEventListener("change", () => {
    const category = input.dataset.simpleSourceEnabled;
    state.configDraft.sources[category].mode = input.checked ? input.dataset.activeMode : "disabled";
    input.closest(".simple-source-card")?.classList.toggle("is-disabled", !input.checked);
  }));
  $$('[data-open-source-directory]').forEach(button => button.addEventListener("click", async event => {
    event.stopPropagation();
    button.disabled = true;
    try {
      const category = encodeURIComponent(button.dataset.openSourceDirectory);
      const result = await api(`/configs/${state.configId}/sources/${category}/open-directory`, { method: "POST" });
      toast(`已在${result.manager}中打开 ${result.folder_name}`);
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  }));
  $("#simpleAddPoolBtn")?.addEventListener("click", event => addPoolFromEditor(event.currentTarget));
  $("#simpleAddBenefitBtn")?.addEventListener("click", event => addBenefitFromEditor(event.currentTarget));
  $$('[data-simple-source-action]').forEach(button => button.addEventListener("click", async () => {
    const category = button.dataset.simpleSourceId;
    const action = button.dataset.simpleSourceAction;
    if (action !== "delete") {
      moveSimpleSource(category, action);
      return;
    }
    if (state.configDraft.workflow_type === "generic") {
      const group = state.configDraft.sources[category];
      const count = state.scan?.assets?.[category]?.length ?? 0;
      if (!window.confirm(`删除视频库“${group.label || category}”的编排配置？\n当前扫描到 ${count} 个素材，磁盘目录和素材会原样保留。`)) return;
      await deletePoolFromEditor(category, button);
      return;
    }
    const benefits = state.configDraft.timeline.filter(isBenefitCategory);
    if (benefits.length <= 1) { toast("至少需要保留一个利益点段", true); return; }
    if (!window.confirm(`删除${categoryLabel(category)}的配置？\n对应文件夹和本地素材会原样保留。`)) return;
    mutateSimpleConfig(config => {
      config.timeline = config.timeline.filter(item => item !== category);
      delete config.sources[category];
    });
  }));
  bindSimpleSourceDrag();

  $("#simpleOverlayEnabled")?.addEventListener("change", event => {
    state.configDraft.benefit_overlays.mode = event.target.checked ? "required" : "disabled";
    renderSimpleConfig();
  });
  $("#simpleVisualDedupEnabled")?.addEventListener("change", event => {
    ensureVisualDedup(state.configDraft).enabled = event.target.checked;
    renderSimpleConfig();
  });
  $("#simpleVisualBorderEnabled")?.addEventListener("change", event => {
    ensureVisualDedup(state.configDraft).border_overlay.mode = event.target.checked
      ? "required"
      : "disabled";
    renderSimpleConfig();
  });
  $$('[data-simple-choice]').forEach(button => button.addEventListener("click", () => {
    const choice = button.dataset.simpleChoice;
    const value = button.dataset.simpleValue;
    if (choice === "timing") {
      state.configDraft.benefit_overlays.timing.scope = value;
    } else if (choice === "output") {
      const sizes = { portrait: [720, 1280], landscape: [1280, 720], square: [1080, 1080] };
      if (sizes[value]) {
        [state.configDraft.output.width, state.configDraft.output.height] = sizes[value];
        state.configDraft.output.fps = 30;
      }
    } else if (choice === "quality") {
      const presets = {
        fast: { video_preset: "veryfast", crf: 23 },
        recommended: { video_preset: "medium", crf: 18 },
        high: { video_preset: "slow", crf: 16 },
      };
      Object.assign(state.configDraft.output, presets[value]);
      state.configDraft.output.rate_control = "crf";
    } else if (choice === "duplicate") {
      state.configDraft.randomization.duplicate_policy = value;
    } else if (choice === "visualDedup") {
      const presets = {
        light: { scale: 0.94, sigma: 12, steps: 2, brightness: 0 },
        standard: { scale: 0.9, sigma: 20, steps: 2, brightness: 0 },
        strong: { scale: 0.86, sigma: 28, steps: 2, brightness: 0 },
      };
      if (presets[value]) {
        const visual = ensureVisualDedup(state.configDraft);
        visual.foreground.scale = presets[value].scale;
        Object.assign(visual.background, {
          sigma: presets[value].sigma,
          steps: presets[value].steps,
          brightness: presets[value].brightness,
        });
      }
    }
    renderSimpleConfig();
  }));
  $("#simpleOverlayStart")?.addEventListener("input", event => {
    state.configDraft.benefit_overlays.timing.start_seconds = Number(event.target.value || 0);
  });
  $("#simpleOverlayEnd")?.addEventListener("input", event => {
    state.configDraft.benefit_overlays.timing.end_seconds = event.target.value === "" ? null : Number(event.target.value);
  });
  $("#simpleOverlayFile")?.addEventListener("change", event => uploadOverlayImage(event.target.files?.[0], event.target));
  $("#simpleVisualBorderFile")?.addEventListener("change", event => uploadVisualBorder(event.target.files?.[0], event.target));

  $("#simpleLoudnessEnabled")?.addEventListener("change", event => {
    state.configDraft.output.loudness.enabled = event.target.checked;
  });
}

function bindSimpleSourceDrag() {
  let dragged = null;
  $$('[data-simple-source-card][draggable="true"]').forEach(card => {
    card.addEventListener("dragstart", event => {
      dragged = card.dataset.simpleSourceCard;
      card.classList.add("dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", dragged);
    });
    card.addEventListener("dragover", event => {
      const target = card.dataset.simpleSourceCard;
      const generic = state.configDraft.workflow_type === "generic";
      if (!dragged || dragged === target || (!generic && (!isBenefitCategory(dragged) || !isBenefitCategory(target)))) return;
      event.preventDefault();
      card.classList.add("drag-over");
    });
    card.addEventListener("dragleave", () => card.classList.remove("drag-over"));
    card.addEventListener("drop", event => {
      event.preventDefault();
      const target = card.dataset.simpleSourceCard;
      if (!dragged || dragged === target) return;
      mutateSimpleConfig(config => {
        const next = config.timeline.filter(category => category !== dragged);
        next.splice(next.indexOf(target), 0, dragged);
        config.timeline = next;
      });
    });
    card.addEventListener("dragend", () => {
      dragged = null;
      $$('[data-simple-source-card]').forEach(item => item.classList.remove("dragging", "drag-over"));
    });
  });
}

async function copyOverlayDirectory() {
  const directory = simpleOverlayDirectory();
  try {
    await navigator.clipboard.writeText(directory);
    toast("风险提示语图片文件夹位置已复制");
  } catch (_) {
    window.prompt("复制下面的文件夹位置", directory);
  }
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 32768) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
  }
  return btoa(binary);
}

async function uploadOverlayImage(file, input) {
  if (!file) return;
  if (file.size > 20 * 1024 * 1024) {
    toast("风险提示语图片不能超过 20 MB", true);
    input.value = "";
    return;
  }
  input.disabled = true;
  try {
    const saved = await api(`/configs/${state.configId}/structured`, {
      method: "PUT",
      headers: configLeaseHeaders(),
      body: JSON.stringify({ config: currentStructuredDraft() }),
    });
    state.configHash = saved.content_hash;
    const dataBase64 = arrayBufferToBase64(await file.arrayBuffer());
    const result = await api(`/configs/${state.configId}/overlay-image`, {
      method: "POST",
      headers: configLeaseHeaders(state.configLease, saved.content_hash),
      body: JSON.stringify({
        filename: file.name,
        data_base64: dataBase64,
        current_config_hash: saved.content_hash,
      }),
    });
    await refreshConfigEditor($("#simpleConfigEditor").scrollTop);
    toast(`风险提示语图片已更换为 ${result.filename}`);
  } catch (error) {
    toast(error.message, true);
    input.disabled = false;
    input.value = "";
  }
}

async function uploadVisualBorder(file, input) {
  if (!file) return;
  if (file.size > 500 * 1024 * 1024) {
    toast("视觉去重边框不能超过 500 MB", true);
    input.value = "";
    return;
  }
  input.disabled = true;
  try {
    const saved = await api(`/configs/${state.configId}/structured`, {
      method: "PUT",
      headers: configLeaseHeaders(),
      body: JSON.stringify({ config: currentStructuredDraft() }),
    });
    state.configHash = saved.content_hash;
    const result = await api(`/configs/${state.configId}/visual-border`, {
      method: "POST",
      headers: {
        ...configLeaseHeaders(state.configLease, saved.content_hash),
        "Content-Type": "application/octet-stream",
        "X-SmartStitch-Filename": encodeURIComponent(file.name),
      },
      body: file,
    });
    await refreshConfigEditor($("#simpleConfigEditor").scrollTop);
    toast(`视觉去重边框已更换为 ${result.filename}`);
  } catch (error) {
    toast(error.message, true);
    input.disabled = false;
    input.value = "";
  }
}

function configInput(label, path, value, options = {}) {
  const { type = "text", hint = "", wide = false, placeholder = "", className = "", pathInput = false } = options;
  const dataType = type === "number" ? "number" : type === "nullable-number" ? "nullable-number" : type === "list" ? "list" : "string";
  const inputType = ["number", "nullable-number"].includes(type) ? "number" : "text";
  const renderedValue = Array.isArray(value) ? value.join(", ") : (value ?? "");
  return `<div class="config-field ${wide ? "wide" : ""} ${className}"><label>${label}${hint ? `<small>${hint}</small>` : ""}</label><input type="${inputType}" data-config-path="${path}" data-config-type="${dataType}" value="${escapeHtml(renderedValue)}" placeholder="${escapeHtml(placeholder)}" ${pathInput ? 'data-path-input' : ''} ${inputType === "number" ? 'step="any"' : ""}></div>`;
}

function configTextarea(label, path, value) {
  return `<div class="config-field wide"><label>${label}</label><textarea data-config-path="${path}" data-config-type="string">${escapeHtml(value || "")}</textarea></div>`;
}

function configHelp(title, items) {
  return `<span class="field-help-wrap">
    <button class="field-help-button" type="button" aria-label="${escapeHtml(title)}">?</button>
    <span class="field-help-popover" role="tooltip">
      <strong>${escapeHtml(title)}</strong>
      ${items.map(([name, description]) => `<span><b>${escapeHtml(name)}</b>${escapeHtml(description)}</span>`).join("")}
    </span>
  </span>`;
}

function configSelect(label, path, value, choices, hint = "", help = null) {
  const labelTitle = `<span class="config-label-title">${label}${help ? configHelp(help.title, help.items) : ""}</span>`;
  return `<div class="config-field"><label>${labelTitle}${hint ? `<small>${hint}</small>` : ""}</label><select data-config-path="${path}" data-config-type="string">${choices.map(([key, text]) => `<option value="${key}" ${value === key ? "selected" : ""}>${text}</option>`).join("")}</select></div>`;
}

function configSwitch(label, path, value, help = "") {
  return `<div class="config-field"><label>${help || "开关"}</label><div class="config-switch"><span>${label}</span><input class="switch-input" type="checkbox" data-config-path="${path}" data-config-type="boolean" ${value ? "checked" : ""}></div></div>`;
}

function renderVisualConfig() {
  const config = state.configDraft;
  const generic = config.workflow_type === "generic";
  const modeChoices = [["required", "必需"], ["optional", "可选"], ["disabled", "停用"]];
  const orderedCategories = [
    ...config.timeline,
    ...Object.keys(config.sources).filter(category => !config.timeline.includes(category)),
  ];
  const benefitCategories = config.timeline.filter(isBenefitCategory);
  const sourceCards = orderedCategories.map(category => {
    const group = config.sources[category];
    if (generic) {
      const poolIndex = config.timeline.indexOf(category);
      return `
      <div class="source-config-card pool-config-card" draggable="true" data-pool-card="${escapeHtml(category)}">
        <div class="source-config-heading">
          <div class="source-config-title"><span class="pool-drag-handle" title="拖动改变拼接顺序">⠿</span><b>${String(poolIndex + 1).padStart(2, "0")}</b>${escapeHtml(group.label || category)} <small>${escapeHtml(category)}</small></div>
          <div class="source-config-actions">
            <button type="button" class="text-btn" data-pool-action="up" data-pool-id="${escapeHtml(category)}" ${poolIndex <= 0 ? "disabled" : ""}>上移</button>
            <button type="button" class="text-btn" data-pool-action="down" data-pool-id="${escapeHtml(category)}" ${poolIndex === config.timeline.length - 1 ? "disabled" : ""}>下移</button>
            <button type="button" class="text-btn danger-text" data-pool-action="delete" data-pool-id="${escapeHtml(category)}">删除</button>
          </div>
        </div>
        <div class="config-form-grid three">
          ${configInput("显示名称", `sources.${category}.label`, group.label || category)}
          ${configSelect("使用方式", `sources.${category}.mode`, group.mode, modeChoices)}
          ${configInput("默认权重", `sources.${category}.default_weight`, group.default_weight, { type: "number" })}
          ${configInput("扩展名", `sources.${category}.extensions`, group.extensions, { type: "list", hint: "逗号分隔" })}
          <div class="config-field wide"><label>受管素材目录 <small>稳定路径，不随名称修改</small></label><code class="managed-pool-path">${escapeHtml(group.directory)}</code></div>
          ${configTextarea("说明", `sources.${category}.description`, group.description || "")}
        </div>
      </div>`;
    }
    const benefitIndex = benefitCategories.indexOf(category);
    const benefitControls = benefitIndex >= 0 ? `<div class="source-config-actions">
      <button type="button" class="text-btn" data-benefit-action="up" data-benefit-category="${category}" ${benefitIndex === 0 ? "disabled" : ""}>上移</button>
      <button type="button" class="text-btn" data-benefit-action="down" data-benefit-category="${category}" ${benefitIndex === benefitCategories.length - 1 ? "disabled" : ""}>下移</button>
      <button type="button" class="text-btn danger-text" data-benefit-action="delete" data-benefit-category="${category}" ${benefitCategories.length <= 1 ? "disabled" : ""}>删除</button>
    </div>` : "";
    return `
    <div class="source-config-card">
      <div class="source-config-heading"><div class="source-config-title"><i></i>${escapeHtml(categoryLabel(category))} <small>${escapeHtml(category)}</small></div>${benefitControls}</div>
      <div class="config-form-grid three">
        ${configSelect("使用方式", `sources.${category}.mode`, group.mode, modeChoices)}
        ${configInput("默认权重", `sources.${category}.default_weight`, group.default_weight, { type: "number" })}
        ${configInput("扩展名", `sources.${category}.extensions`, group.extensions, { type: "list", hint: "逗号分隔" })}
        ${configInput("素材目录", `sources.${category}.directory`, group.directory, { wide: true, pathInput: true })}
        ${category === "end_card" ? configInput("静态尾帧时长", `sources.${category}.image_duration_seconds`, group.image_duration_seconds, { type: "number", hint: "秒" }) : ""}
      </div>
    </div>`;
  }).join("");

  const overlay = config.benefit_overlays;
  const visual = ensureVisualDedup(config);
  const visualBorderAsset = (state.scan?.assets?.visual_border || [])[0];
  const visualBorderProbe = visualBorderAsset?.probe;
  const output = config.output;
  const loudness = output.loudness || {
    enabled: false,
    target_lufs: -14,
    loudness_range_lu: 7,
    true_peak_dbtp: -1.5,
    preview_duration_seconds: 12,
  };
  const naming = generic ? ensureOutputNaming(config) : null;
  const feishu = ensureFeishuBaseSync(config);
  const namingSection = generic ? `
    <details class="config-section" data-config-section="output-naming">
      <summary>成片命名 <small>产品、利益点与素材文件名解析</small></summary>
      <div class="config-section-body config-form-grid three">
        ${configSwitch("启用业务动态命名", "output.naming.enabled", naming.enabled, "命名方式")}
        ${configInput("产品", "output.naming.product", naming.product, { className: "naming-option", placeholder: "例如 燕麦奶" })}
        ${configInput("利益点", "output.naming.benefit", naming.benefit, { className: "naming-option", placeholder: "例如 第二件半价" })}
        ${configInput("命名模板", "output.naming.template", naming.template, { wide: true, className: "naming-option", hint: "可用 product、benefit、talents、restriction_date、sequence" })}
        ${configInput("参与解析的视频库", "output.naming.source_metadata.categories", naming.source_metadata.categories, { type: "list", wide: true, className: "naming-option", hint: "pool_* 表示所有通用视频库" })}
        <div class="naming-option">${configSwitch("移除 SmartStitch 切片后缀", "output.naming.source_metadata.strip_smartstitch_suffix", naming.source_metadata.strip_smartstitch_suffix)}</div>
        ${configInput("文件名解析正则", "output.naming.source_metadata.pattern", naming.source_metadata.pattern, { wide: true, className: "naming-option", hint: "必须包含 talent 和 restriction_date 命名分组" })}
        ${configInput("允许的日期格式", "output.naming.source_metadata.restriction_date_formats", naming.source_metadata.restriction_date_formats, { type: "list", className: "naming-option", hint: "默认 %Y-%m-%d，逗号分隔" })}
        <div class="naming-option">${configSelect("解析失败", "output.naming.source_metadata.on_unmatched", naming.source_metadata.on_unmatched, [["error", "报错并停止"]])}</div>
        <div class="naming-option">${configSelect("达人合并", "output.naming.talent.merge", naming.talent.merge, [["ordered_unique", "按时间线去重"]])}</div>
        ${configInput("达人连接符", "output.naming.talent.separator", naming.talent.separator, { className: "naming-option" })}
        <div class="naming-option">${configSelect("限制日期合并", "output.naming.restriction_date.merge", naming.restriction_date.merge, [["earliest", "取最早日期"]])}</div>
        ${configInput("日期输出格式", "output.naming.restriction_date.output_format", naming.restriction_date.output_format, { className: "naming-option" })}
        ${configInput("重名后缀", "output.naming.duplicate_suffix", naming.duplicate_suffix, { className: "naming-option", hint: "例如 -{serial:02d}" })}
        <div class="config-field wide naming-option"><label>文件名示例 <small>使用当前扫描素材</small></label><code id="advancedNamingPreview" class="managed-pool-path">${escapeHtml(namingExample(config))}</code></div>
        <div class="config-field wide naming-option naming-test-row"><button id="testNamingPatternBtn" class="button secondary small" type="button">用已扫描素材测试解析</button><div id="namingTestResult" class="naming-test-result"></div></div>
      </div>
    </details>` : "";
  const feishuSecretHint = state.feishuSettings.app_secret_configured
    ? "已配置，留空则不修改"
    : "填写 App Secret";
  const feishuSection = `
    <details class="config-section" data-config-section="feishu-sync">
      <summary>飞书多维表格同步 <small>凭证、目标 Base 与数据表</small></summary>
      <div class="config-section-body config-form-grid three">
        <div class="config-field"><label>同步状态</label><div class="config-switch"><span>任务结束后自动同步</span><input id="advancedFeishuEnabled" class="switch-input" type="checkbox" data-config-path="output.feishu_base_sync.enabled" data-config-type="boolean" ${feishu.enabled ? "checked" : ""}></div></div>
        <div class="config-field"><label>App ID <small>全局凭证</small></label><input id="advancedFeishuAppId" value="${escapeHtml(state.feishuSettingsDraft.app_id)}" placeholder="cli_xxxxxxxxxxxxx"></div>
        <div class="config-field"><label>App Secret <small>不会读回</small></label><input id="advancedFeishuAppSecret" type="password" value="${escapeHtml(state.feishuSecretDraft)}" placeholder="${escapeHtml(feishuSecretHint)}" autocomplete="new-password"></div>
        <div class="config-field wide"><label>多维表格链接</label><input id="advancedFeishuUrl" data-config-path="output.feishu_base_sync.base_url" data-config-type="string" value="${escapeHtml(feishu.base_url)}" placeholder="支持 /wiki/... 或 /base/... 链接"></div>
        <div class="config-field"><label>数据表</label><select id="advancedFeishuTable" data-config-path="output.feishu_base_sync.table_id" data-config-type="string">${feishuTableOptions(feishu.table_id)}</select></div>
        ${configSelect("同步条目", "output.feishu_base_sync.row_scope", feishu.row_scope, [["all_items", "成功与失败都同步"], ["succeeded_only", "只同步成功成片"]])}
        <div class="config-field"><label>写入方式</label><code class="managed-pool-path">upsert · 稳定键去重</code></div>
        <div class="config-field wide simple-feishu-actions"><button id="advancedTestFeishuBtn" class="button secondary small" type="button">测试连接</button><span class="${state.feishuConnection ? "ok" : ""}">${escapeHtml(feishuConnectionStatus())}</span><small>Secret 仅保存在本机 data/integrations.yaml。</small>${feishuConnectionErrorMarkup()}</div>
      </div>
    </details>`;
  const batch = config.batch;
  const timelineEditor = generic
    ? `<div class="config-field wide"><label>拼接顺序 <small>在下方拖动视频库卡片调整</small></label><div class="managed-pool-path">${config.timeline.map(category => escapeHtml(config.sources[category]?.label || category)).join(" → ") || "尚未添加视频库"}</div></div>`
    : configInput("时间线顺序", "timeline", config.timeline, { type: "list", hint: "逗号分隔" });
  const sourceToolbar = generic
    ? `<div class="benefit-config-toolbar"><div><strong>自定义视频库</strong><small>每个库抽取一个视频；拖动卡片决定最终拼接顺序</small></div><button id="addPoolBtn" class="button secondary small" type="button" ${config.timeline.length >= 50 ? "disabled" : ""}>+添加视频库</button></div>${config.timeline.length ? "" : '<div class="empty-pool-state">还没有视频库。添加第一个视频库后即可放入素材并生成。</div>'}`
    : `<div class="benefit-config-toolbar"><div><strong>多人利益点</strong><small>每段从自己的素材池中抽取 1 个片段</small></div><button id="addBenefitBtn" class="button secondary small" type="button" ${benefitCategories.length >= 20 ? "disabled" : ""}>+添加利益点</button></div>`;
  const overlayTimingChoices = generic
    ? [["full", "整条成片"], ["custom", "自定义时段"]]
    : [["full", "整条成片"], ["main", "主片段"], ["benefits", "全部利益点段"], ["custom", "自定义时段"]];
  const overlaySection = `
    <details class="config-section" data-config-section="benefit-overlay" open>
      <summary>风险提示语图片 <small>最高图层叠加设置</small></summary>
      <div class="config-section-body config-form-grid three">
        ${configSelect("使用方式", "benefit_overlays.mode", overlay.mode, modeChoices)}
        ${configInput("唯一图片文件", "benefit_overlays.file", overlay.file, { wide: true, pathInput: true, hint: "受管项目库可留空自动识别 · 固定且不参与随机", placeholder: "/路径/风险提示语图片.png" })}
        ${configSelect("缩放方式", "benefit_overlays.placement.scale_mode", overlay.placement.scale_mode, [["original", "保持原尺寸"], ["fit", "等比适配画布"], ["stretch", "拉伸铺满"]])}
        ${configInput("整体透明度", "benefit_overlays.placement.opacity", overlay.placement.opacity, { type: "number", hint: "0～1" })}
        ${configSwitch("超出画布时自动缩小", "benefit_overlays.placement.shrink_if_oversized", overlay.placement.shrink_if_oversized)}
        ${configInput("横向位置 X", "benefit_overlays.placement.x", overlay.placement.x, { placeholder: "0 或 (W-w)/2" })}
        ${configInput("纵向位置 Y", "benefit_overlays.placement.y", overlay.placement.y, { placeholder: "0 或 (H-h)/2" })}
        ${configSelect("显示时段", "benefit_overlays.timing.scope", overlay.timing.scope, overlayTimingChoices)}
        ${configInput("自定义开始", "benefit_overlays.timing.start_seconds", overlay.timing.start_seconds, { type: "number", hint: "秒" })}
        ${configInput("自定义结束", "benefit_overlays.timing.end_seconds", overlay.timing.end_seconds, { type: "nullable-number", hint: "留空到片尾" })}
      </div>
    </details>`;
  const visualDedupSection = `
    <details class="config-section" data-config-section="visual-dedup" open>
      <summary>视觉去重 <small>模糊背景、缩小前景与透明边框</small></summary>
      <div class="config-section-body config-form-grid three">
        ${configSwitch("启用视觉去重", "visual_dedup.enabled", visual.enabled, "功能状态")}
        ${configInput("前景缩放比例", "visual_dedup.foreground.scale", visual.foreground.scale, { type: "number", hint: "0.70～1.00" })}
        ${configSelect("背景处理", "visual_dedup.background.mode", visual.background.mode, [["gaussian_blur", "高斯模糊"]])}
        ${configInput("模糊 Sigma", "visual_dedup.background.sigma", visual.background.sigma, { type: "number", hint: "0～100" })}
        ${configInput("模糊步数", "visual_dedup.background.steps", visual.background.steps, { type: "number", hint: "1～6" })}
        ${configInput("背景亮度", "visual_dedup.background.brightness", visual.background.brightness, { type: "number", hint: "-1～1" })}
        ${configSelect("边框使用方式", "visual_dedup.border_overlay.mode", visual.border_overlay.mode, modeChoices)}
        ${configInput("边框文件", "visual_dedup.border_overlay.file", visual.border_overlay.file, { wide: true, pathInput: true, hint: "受管项目可留空自动识别", placeholder: "/路径/透明边框.mov" })}
        ${configSelect("边框素材类型", "visual_dedup.border_overlay.media_kind", visual.border_overlay.media_kind, [["auto", "自动识别"]])}
        ${configSelect("边框尺寸", "visual_dedup.border_overlay.scale_mode", visual.border_overlay.scale_mode, [["exact", "必须与画布一致"], ["stretch", "拉伸铺满画布"]])}
        ${configSelect("播放方式", "visual_dedup.border_overlay.playback", visual.border_overlay.playback, [["loop", "循环到成片结束"]])}
        ${configInput("边框透明度", "visual_dedup.border_overlay.opacity", visual.border_overlay.opacity, { type: "number", hint: "0～1" })}
        ${configSelect("Alpha 模式", "visual_dedup.border_overlay.alpha_mode", visual.border_overlay.alpha_mode, [["straight", "直通 Alpha"], ["premultiplied", "预乘 Alpha（黑边时尝试）"]])}
        <div class="config-field wide"><label>边框预检 <small>由后端 FFmpeg 统一检查</small></label><code class="managed-pool-path">${escapeHtml(visualBorderAsset
          ? (visualBorderAsset.valid
            ? `${visualBorderAsset.name} · ${visualBorderProbe?.video_codec || "未知编码"} · ${visualBorderProbe?.pixel_format || "未知像素格式"} · ${visualBorderProbe?.width || "?"}×${visualBorderProbe?.height || "?"} · ${visualBorderProbe?.fps || "静态"} fps · ${visualBorderProbe?.duration || "?"} s${visualBorderProbe?.has_audio ? " · 含音轨（将忽略）" : ""}`
            : `${visualBorderAsset.name} · ${visualBorderAsset.error || "不可用"}`)
          : "尚未识别到边框素材")}</code></div>
      </div>
    </details>`;
  const previewAssets = Object.entries(state.scan?.assets || {}).flatMap(([category, assets]) =>
    assets
      .filter(asset => asset.media_type === "video" && asset.valid && asset.probe?.has_audio)
      .map(asset => ({ ...asset, category }))
  );
  const previewOptions = previewAssets.map(asset =>
    `<option value="${escapeHtml(asset.path)}">${escapeHtml(categoryLabel(asset.category))} · ${escapeHtml(asset.name)}</option>`
  ).join("");
  $("#visualConfigEditor").innerHTML = `
    <details class="config-section" data-config-section="basic" open>
      <summary>基础信息 <small>名称、主目录与时间线</small></summary>
      <div class="config-section-body config-form-grid">
        ${configInput("配置 ID", "id", config.id, { hint: "不可修改" })}
        ${configInput("配置名称", "name", config.name)}
        ${configSwitch("启用此配置", "enabled", config.enabled, "配置状态")}
        ${timelineEditor}
        ${configInput("主素材根目录", "source_root", config.source_root, { wide: true, pathInput: true })}
        ${configTextarea("配置说明", "description", config.description)}
      </div>
    </details>

    <details class="config-section" data-config-section="sources" open>
      <summary>视频素材 <small>${generic ? "可自由新增、编辑、删除和拖动排序" : "利益点段可独立增删和排序"}</small></summary>
      <div class="config-section-body">
        ${sourceToolbar}
        ${sourceCards}
      </div>
    </details>

    ${overlaySection}

    ${visualDedupSection}

    <details class="config-section randomization-section" data-config-section="randomization">
      <summary>随机组合 <small>仅用于视频片段和尾帧</small></summary>
      <div class="config-section-body config-form-grid three">
        ${configSelect("权重算法", "randomization.mode", config.randomization.mode, [["quota_shuffle", "按批次配额后洗牌"], ["independent_random", "逐条独立随机"]], "", {
          title: "权重算法怎么选？",
          items: [
            ["按批次配额后洗牌：", "先按权重分配整批的出现次数，再打乱顺序。数量更稳定，适合批量生成。"],
            ["逐条独立随机：", "每生成一条都重新抽一次。结果更随机，小批量时可能和设置的比例有偏差。"],
          ],
        })}
        ${configSelect("核心组合去重策略", "randomization.duplicate_policy", config.randomization.duplicate_policy, [["allow", "允许重复（权重优先）"], ["best_effort", "尽量去重（权重优先）— 推荐"], ["strict", "严格去重（组合优先）"]], generic ? "全部启用的视频库" : "引子 + 全部利益点段 + 结尾", {
          title: "去重策略怎么选？",
          items: [
            ["允许重复：", "完全按权重选择，相同的引子、利益点和结尾组合可以再次出现。"],
            ["尽量去重：", "优先保留权重设置，同时尽量换一种组合。适合大多数批量生成。"],
            ["严格去重：", "每条核心组合都不同；组合不够时会停止并提示。"],
          ],
        })}
        ${configInput("默认随机种子", "randomization.default_seed", config.randomization.default_seed, { type: "nullable-number", hint: "留空自动" })}
      </div>
    </details>

    <details class="config-section" data-config-section="output">
      <summary>成片输出 <small>尺寸、编码质量与文件名</small></summary>
      <div class="config-section-body config-form-grid three">
        ${configInput("默认输出目录", "output.directory", output.directory, { wide: true, pathInput: true })}
        ${configInput("宽度", "output.width", output.width, { type: "number", hint: "px" })}
        ${configInput("高度", "output.height", output.height, { type: "number", hint: "px" })}
        ${configInput("帧率", "output.fps", output.fps, { type: "number", hint: "fps" })}
        ${configSelect("画面适配", "output.resize_mode", output.resize_mode, [["fit_pad", "等比缩放并补边"], ["fill_crop", "铺满并居中裁剪"], ["stretch", "直接拉伸"]])}
        ${configInput("补边颜色", "output.background_color", output.background_color)}
        ${configInput("视频编码器", "output.video_codec", output.video_codec)}
        ${configSelect("编码速度", "output.video_preset", output.video_preset, [["ultrafast", "ultrafast（最快）"], ["veryfast", "veryfast"], ["fast", "fast"], ["medium", "medium（推荐）"], ["slow", "slow（更省体积）"]])}
        ${configSelect("码率控制", "output.rate_control", output.rate_control, [["vbr", "VBR 目标平均码率"], ["crf", "CRF 恒定质量"]])}
        ${configInput("VBR 目标码率", "output.video_bitrate_kbps", output.video_bitrate_kbps, { type: "number", hint: "kbps", className: "rate-option rate-vbr" })}
        ${configInput("CRF 质量值", "output.crf", output.crf, { type: "number", hint: "数值越低画质越高", className: "rate-option rate-crf" })}
        ${configInput("音频码率", "output.audio_bitrate", output.audio_bitrate)}
        ${configInput("音频采样率", "output.audio_sample_rate", output.audio_sample_rate, { type: "number" })}
        ${configSelect("声道", "output.audio_channels", String(output.audio_channels), [["1", "单声道"], ["2", "双声道"]])}
        ${configSwitch("启用响度均衡", "output.loudness.enabled", loudness.enabled, "响度均衡")}
        <div class="config-field loudness-option">
          <label>目标响度 <small>-24 ～ -8 LUFS</small></label>
          <div class="range-number-control">
            <input id="loudnessTargetSlider" type="range" min="-24" max="-8" step="0.5" value="${loudness.target_lufs}">
            <div class="number-with-unit"><input id="loudnessTargetNumber" type="number" min="-24" max="-8" step="0.5" value="${loudness.target_lufs}" data-config-path="output.loudness.target_lufs" data-config-type="number"><span>LUFS</span></div>
          </div>
        </div>
        ${configInput("试听时长", "output.loudness.preview_duration_seconds", loudness.preview_duration_seconds, { type: "number", hint: "3～30 秒", className: "loudness-option" })}
        ${configInput("响度范围", "output.loudness.loudness_range_lu", loudness.loudness_range_lu, { type: "number", hint: "LU", className: "loudness-option" })}
        ${configInput("真峰值上限", "output.loudness.true_peak_dbtp", loudness.true_peak_dbtp, { type: "number", hint: "dBTP", className: "loudness-option" })}
        <div class="config-field loudness-option">
          <label>试听素材 <small>当前配置中的有声视频</small></label>
          <select id="loudnessPreviewSource" ${previewAssets.length ? "" : "disabled"}>${previewOptions || '<option>没有可试听的音频素材</option>'}</select>
        </div>
        <div class="config-field wide loudness-option">
          <label>响度试听 <small id="loudnessPreviewStatus">选择同一素材对比试听</small></label>
          <div class="loudness-audition-controls">
            <button id="previewOriginalAudioBtn" class="button secondary small" type="button" ${previewAssets.length ? "" : "disabled"}>试听原音</button>
            <button id="previewNormalizedAudioBtn" class="button primary small" type="button" ${previewAssets.length ? "" : "disabled"}>试听均衡后</button>
            <audio id="loudnessPreviewAudio" controls preload="none"></audio>
          </div>
        </div>
        ${configInput(naming?.enabled ? "备用文件名模板" : "文件名模板", "output.filename_template", output.filename_template, { wide: true, hint: naming?.enabled ? "业务动态命名已启用，当前不生效" : "" })}
        ${configSelect(naming?.enabled ? "备用重名处理" : "重名处理", "output.collision_policy", output.collision_policy, [["increment", "自动递增"], ["error", "报错"], ["overwrite", "覆盖"]], naming?.enabled ? "业务动态命名已启用" : "")}
        ${configSwitch("启用 Faststart", "output.faststart", output.faststart)}
      </div>
    </details>

    ${namingSection}

    ${feishuSection}

    <details class="config-section" data-config-section="batch-scanner">
      <summary>批处理与扫描 <small>默认数量、并发和文件过滤</small></summary>
      <div class="config-section-body config-form-grid three">
        ${configInput("默认生成数量", "batch.default_count", batch.default_count, { type: "number" })}
        ${configInput("单批最大数量", "batch.max_count", batch.max_count, { type: "number" })}
        ${configInput("并发任务", "batch.concurrency", batch.concurrency, { type: "number" })}
        ${configInput("失败重试次数", "batch.retry_count", batch.retry_count, { type: "number" })}
        ${configInput("最小剩余空间", "batch.minimum_free_space_gb", batch.minimum_free_space_gb, { type: "number", hint: "GB" })}
        ${configSwitch("递归扫描子目录", "scanner.recursive", config.scanner.recursive)}
        ${configSwitch("忽略隐藏文件", "scanner.ignore_hidden_files", config.scanner.ignore_hidden_files)}
        ${configInput("忽略文件前缀", "scanner.ignore_prefixes", config.scanner.ignore_prefixes, { type: "list" })}
        ${configInput("忽略文件名", "scanner.ignore_names", config.scanner.ignore_names, { type: "list" })}
      </div>
    </details>`;

  const idInput = $('[data-config-path="id"]');
  if (idInput) idInput.disabled = true;
  if (generic) bindPoolConfigControls();
  else bindBenefitConfigControls();
  bindOutputControls();
  bindFeishuControls("advanced");
}

function mutateBenefitConfig(mutator) {
  state.configDraft = currentStructuredDraft();
  const scrollTop = configEditorScrollTop();
  mutator(state.configDraft);
  renderSimpleConfig();
  renderVisualConfig();
  restoreActiveConfigScroll(scrollTop);
}

function currentStructuredDraft() {
  return state.configMode === "advanced"
    ? collectVisualConfig()
    : structuredClone(state.configDraft);
}

function configEditorScrollTop() {
  return state.configMode === "simple"
    ? $("#simpleConfigEditor").scrollTop
    : $("#visualConfigEditor").scrollTop;
}

function restoreActiveConfigScroll(scrollTop) {
  const editor = state.configMode === "simple"
    ? $("#simpleConfigEditor")
    : $("#visualConfigEditor");
  if (editor) editor.scrollTop = scrollTop;
}

function bindBenefitConfigControls() {
  $("#addBenefitBtn")?.addEventListener("click", event => addBenefitFromEditor(event.currentTarget));

  $$('[data-benefit-action]').forEach(button => button.addEventListener("click", () => {
    const category = button.dataset.benefitCategory;
    const action = button.dataset.benefitAction;
    if (action === "delete" && !window.confirm(`删除${categoryLabel(category)}的配置？\n对应文件夹和本地素材会原样保留。`)) return;
    mutateBenefitConfig(config => {
      const benefits = config.timeline.filter(isBenefitCategory);
      if (action === "delete") {
        if (benefits.length <= 1) { toast("至少需要保留一个利益点段", true); return; }
        config.timeline = config.timeline.filter(item => item !== category);
        delete config.sources[category];
        return;
      }
      const position = benefits.indexOf(category);
      const other = action === "up" ? benefits[position - 1] : benefits[position + 1];
      if (!other) return;
      const currentIndex = config.timeline.indexOf(category);
      const otherIndex = config.timeline.indexOf(other);
      [config.timeline[currentIndex], config.timeline[otherIndex]] = [config.timeline[otherIndex], config.timeline[currentIndex]];
    });
  }));
}

function bindPoolConfigControls() {
  $("#addPoolBtn")?.addEventListener("click", event => addPoolFromEditor(event.currentTarget));
  $$('[data-pool-action]').forEach(button => button.addEventListener("click", async () => {
    const poolId = button.dataset.poolId;
    const action = button.dataset.poolAction;
    if (action === "delete") {
      const group = state.configDraft.sources[poolId];
      const label = group?.label || poolId;
      const assetCount = state.scan?.assets?.[poolId]?.length ?? 0;
      const directory = `${state.configDraft.source_root.replace(/\/+$/, "")}/${group.directory}`;
      if (!window.confirm(`删除视频库“${label}”的编排配置？\n目录：${directory}\n当前扫描到 ${assetCount} 个素材。\n磁盘目录和其中素材会原样保留。`)) return;
      await deletePoolFromEditor(poolId, button);
      return;
    }
    mutateBenefitConfig(config => {
      const position = config.timeline.indexOf(poolId);
      const otherPosition = action === "up" ? position - 1 : position + 1;
      if (position < 0 || otherPosition < 0 || otherPosition >= config.timeline.length) return;
      [config.timeline[position], config.timeline[otherPosition]] = [config.timeline[otherPosition], config.timeline[position]];
    });
  }));

  let draggedPoolId = null;
  $$('[data-pool-card]').forEach(card => {
    card.addEventListener("dragstart", event => {
      draggedPoolId = card.dataset.poolCard;
      card.classList.add("dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", draggedPoolId);
    });
    card.addEventListener("dragover", event => {
      event.preventDefault();
      if (draggedPoolId && draggedPoolId !== card.dataset.poolCard) card.classList.add("drag-over");
    });
    card.addEventListener("dragleave", () => card.classList.remove("drag-over"));
    card.addEventListener("drop", event => {
      event.preventDefault();
      const targetPoolId = card.dataset.poolCard;
      if (!draggedPoolId || draggedPoolId === targetPoolId) return;
      mutateBenefitConfig(config => {
        const next = config.timeline.filter(category => category !== draggedPoolId);
        next.splice(next.indexOf(targetPoolId), 0, draggedPoolId);
        config.timeline = next;
      });
    });
    card.addEventListener("dragend", () => {
      draggedPoolId = null;
      $$('[data-pool-card]').forEach(item => item.classList.remove("dragging", "drag-over"));
    });
  });
}

async function refreshConfigEditor(scrollTop = 0) {
  const refreshed = await api(`/configs/${state.configId}`);
  state.config = refreshed.config;
  state.configDraft = structuredClone(refreshed.config);
  state.configHash = refreshed.content_hash;
  state.yaml = refreshed.yaml_text;
  state.library = await api(`/libraries/by-config/${state.configId}`);
  state.timeline.sliceTargets = (await api(`/libraries/by-config/${state.configId}/slice-targets`)).targets;
  $("#yamlEditor").value = state.yaml;
  renderSimpleConfig();
  renderVisualConfig();
  if (state.timeline.analysis) renderTimeline();
  await scanAssets(false);
  renderSimpleConfig();
  restoreActiveConfigScroll(scrollTop);
}

async function addPoolFromEditor(button) {
  const label = window.prompt("新视频库名称", `视频库 ${state.configDraft.timeline.length + 1}`)?.trim();
  if (!label) return;
  const scrollTop = configEditorScrollTop();
  button.disabled = true;
  button.textContent = "正在创建文件夹…";
  try {
    const saved = await api(`/configs/${state.configId}/structured`, {
      method: "PUT",
      headers: configLeaseHeaders(),
      body: JSON.stringify({ config: currentStructuredDraft() }),
    });
    state.configHash = saved.content_hash;
    const added = await api(`/configs/${state.configId}/pools`, {
      method: "POST",
      headers: configLeaseHeaders(state.configLease, saved.content_hash),
      body: JSON.stringify({
        label,
        description: "",
        mode: "required",
        default_weight: 1,
        client_request_id: clientRequestId(),
        current_config_hash: saved.content_hash,
      }),
    });
    await refreshConfigEditor(scrollTop);
    toast(`视频库“${label}”已创建：${added.directory}`);
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
    button.textContent = "+添加视频库";
  }
}

async function deletePoolFromEditor(poolId, button) {
  const scrollTop = configEditorScrollTop();
  button.disabled = true;
  try {
    const saved = await api(`/configs/${state.configId}/structured`, {
      method: "PUT",
      headers: configLeaseHeaders(),
      body: JSON.stringify({ config: currentStructuredDraft() }),
    });
    state.configHash = saved.content_hash;
    const result = await api(`/configs/${state.configId}/pools/${poolId}`, {
      method: "DELETE",
      headers: configLeaseHeaders(state.configLease, saved.content_hash),
      body: JSON.stringify({ current_config_hash: saved.content_hash }),
    });
    await refreshConfigEditor(scrollTop);
    toast(`已移除视频库配置；素材仍保留在 ${result.retained_directory}`);
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
  }
}

async function addBenefitFromEditor(button) {
  if (!state.library?.managed) {
    mutateBenefitConfig(config => {
      const numbers = Object.keys(config.sources)
        .map(category => category.match(benefitCategoryPattern))
        .filter(Boolean)
        .map(match => Number(match[1]));
      const nextNumber = Math.max(0, ...numbers) + 1;
      const category = `benefit_${nextNumber}`;
      config.sources[category] = {
        mode: "required",
        directory: `利益点/${nextNumber}`,
        extensions: [".mp4"],
        default_weight: 1,
        image_duration_seconds: 1.5,
        items: [],
      };
      const endingIndex = config.timeline.indexOf("ending");
      config.timeline.splice(endingIndex >= 0 ? endingIndex : config.timeline.length, 0, category);
    });
    toast("已添加利益点草稿；外部素材配置请确认目录后保存");
    return;
  }

  const scrollTop = configEditorScrollTop();
  button.disabled = true;
  button.textContent = "正在创建文件夹…";
  try {
    const draft = currentStructuredDraft();
    const saved = await api(`/configs/${state.configId}/structured`, {
      method: "PUT",
      headers: configLeaseHeaders(),
      body: JSON.stringify({ config: draft }),
    });
    state.configHash = saved.content_hash;
    const added = await api(`/configs/${state.configId}/benefits`, {
      method: "POST",
      headers: configLeaseHeaders(state.configLease, saved.content_hash),
      body: JSON.stringify({
        client_request_id: clientRequestId(),
        current_config_hash: saved.content_hash,
      }),
    });
    const refreshed = await api(`/configs/${state.configId}`);
    state.config = refreshed.config;
    state.configDraft = structuredClone(refreshed.config);
    state.configHash = refreshed.content_hash;
    state.yaml = refreshed.yaml_text;
    state.library = await api(`/libraries/by-config/${state.configId}`);
    state.timeline.sliceTargets = (await api(`/libraries/by-config/${state.configId}/slice-targets`)).targets;
    $("#yamlEditor").value = state.yaml;
    renderSimpleConfig();
    renderVisualConfig();
    if (state.timeline.analysis) renderTimeline();
    restoreActiveConfigScroll(scrollTop);
    const warning = added.warnings?.length ? `；${added.warnings.join("；")}` : "";
    toast(`${categoryLabel(added.category)}已创建，文件夹：${added.directory}${warning}`, Boolean(added.warnings?.length));
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
    button.textContent = "+添加利益点";
  }
}

function refreshAdvancedNamingPreview() {
  const preview = $("#advancedNamingPreview");
  if (!preview) return;
  try {
    preview.textContent = namingExample(collectVisualConfig());
  } catch (error) {
    preview.textContent = `无法预览：${error.message}`;
  }
}

function testAdvancedNamingPattern() {
  const result = $("#namingTestResult");
  if (!result) return;
  let config;
  try {
    config = collectVisualConfig();
    const naming = ensureOutputNaming(config);
    const browserPattern = naming.source_metadata.pattern.replaceAll("(?P<", "(?<");
    new RegExp(browserPattern);
  } catch (error) {
    result.className = "naming-test-result invalid";
    result.textContent = `解析规则无效：${error.message}`;
    return;
  }
  const failures = [];
  let matched = 0;
  for (const category of config.timeline) {
    if (!namingCategoryMatches(config.output.naming, category)) continue;
    for (const asset of state.scan?.assets?.[category] || []) {
      if (!asset.enabled || !asset.valid || Number(asset.weight) <= 0) continue;
      try {
        parseNamingAsset(config, asset);
        matched += 1;
      } catch (error) {
        failures.push(`${config.sources[category]?.label || category}：${asset.name}`);
      }
    }
  }
  result.className = `naming-test-result ${failures.length ? "invalid" : "valid"}`;
  result.innerHTML = failures.length
    ? `成功 ${matched} 个，失败 ${failures.length} 个<br>${failures.slice(0, 10).map(escapeHtml).join("<br>")}`
    : `全部通过，成功解析 ${matched} 个素材`;
}

function bindOutputControls() {
  const slider = $("#loudnessTargetSlider");
  const number = $("#loudnessTargetNumber");
  if (slider && number) {
    slider.addEventListener("input", () => { number.value = slider.value; });
    number.addEventListener("input", () => {
      const value = Math.min(-8, Math.max(-24, Number(number.value)));
      if (Number.isFinite(value)) slider.value = String(value);
    });
  }
  $("#previewOriginalAudioBtn")?.addEventListener("click", () => playLoudnessPreview(false));
  $("#previewNormalizedAudioBtn")?.addEventListener("click", () => playLoudnessPreview(true));
  const loudnessSwitch = $('[data-config-path="output.loudness.enabled"]');
  const rateControl = $('[data-config-path="output.rate_control"]');
  const namingSwitch = $('[data-config-path="output.naming.enabled"]');
  loudnessSwitch?.addEventListener("change", syncConditionalOutputFields);
  rateControl?.addEventListener("change", syncConditionalOutputFields);
  namingSwitch?.addEventListener("change", syncConditionalOutputFields);
  $$('.naming-option input, .naming-option select, .naming-option textarea').forEach(input => {
    input.addEventListener("input", refreshAdvancedNamingPreview);
    input.addEventListener("change", refreshAdvancedNamingPreview);
  });
  $("#testNamingPatternBtn")?.addEventListener("click", testAdvancedNamingPattern);
  syncConditionalOutputFields();
}

function syncConditionalOutputFields() {
  const loudnessEnabled = $('[data-config-path="output.loudness.enabled"]')?.checked ?? false;
  $$(".loudness-option").forEach(field => field.classList.toggle("hidden", !loudnessEnabled));

  const rateControl = $('[data-config-path="output.rate_control"]')?.value;
  $$(".rate-vbr").forEach(field => field.classList.toggle("hidden", rateControl !== "vbr"));
  $$(".rate-crf").forEach(field => field.classList.toggle("hidden", rateControl !== "crf"));

  const namingEnabled = $('[data-config-path="output.naming.enabled"]')?.checked ?? false;
  $$(".naming-option").forEach(field => field.classList.toggle("hidden", !namingEnabled));
}

async function playLoudnessPreview(normalized) {
  const source = $("#loudnessPreviewSource")?.value;
  if (!source) return;
  const buttons = [$("#previewOriginalAudioBtn"), $("#previewNormalizedAudioBtn")].filter(Boolean);
  const status = $("#loudnessPreviewStatus");
  const audio = $("#loudnessPreviewAudio");
  const settings = collectVisualConfig().output.loudness;
  state.previewAudioCleanup?.();
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  buttons.forEach(button => { button.disabled = true; });
  status.textContent = normalized ? "正在生成均衡试听…" : "正在生成原音试听…";
  const url = new URL("/api/v1/audio/preview", window.location.origin);
  Object.entries({
    config_id: state.configId,
    asset_path: source,
    normalized,
    target_lufs: settings.target_lufs,
    loudness_range_lu: settings.loudness_range_lu,
    true_peak_dbtp: settings.true_peak_dbtp,
    duration_seconds: settings.preview_duration_seconds,
  }).forEach(([key, value]) => url.searchParams.set(key, String(value)));

  let finished = false;
  const finish = (message, error = false) => {
    if (finished) return;
    finished = true;
    clearTimeout(timeout);
    audio.removeEventListener("canplay", onCanPlay);
    audio.removeEventListener("error", onError);
    buttons.forEach(button => { button.disabled = false; });
    status.textContent = message;
    if (error) toast(message, true);
    state.previewAudioCleanup = null;
  };
  const onCanPlay = () => finish(normalized ? `均衡后 · ${settings.target_lufs} LUFS` : "原音（未处理）");
  const onError = () => finish("试听加载失败，请重试或检查后端是否已重启", true);
  const timeout = setTimeout(() => finish("试听生成超时，请重试", true), 15000);
  state.previewAudioCleanup = () => {
    if (finished) return;
    finished = true;
    clearTimeout(timeout);
    audio.removeEventListener("canplay", onCanPlay);
    audio.removeEventListener("error", onError);
    buttons.forEach(button => { button.disabled = false; });
  };
  audio.addEventListener("canplay", onCanPlay);
  audio.addEventListener("error", onError);
  audio.src = url.toString();
  audio.load();
  audio.play().catch(error => {
    const message = error.name === "NotAllowedError"
      ? "浏览器阻止了自动播放，请点击播放器的播放键"
      : "试听播放失败，请重试";
    finish(message, error.name !== "NotAllowedError");
  });
}

function collectVisualConfig() {
  const draft = structuredClone(state.configDraft);
  $$('[data-config-path]').forEach(input => {
    const type = input.dataset.configType;
    let value;
    if (type === "boolean") value = input.checked;
    else if (type === "number") value = Number(input.value);
    else if (type === "nullable-number") value = input.value.trim() === "" ? null : Number(input.value);
    else if (type === "list") value = input.value.split(",").map(item => item.trim()).filter(Boolean);
    else value = input.matches("[data-path-input]") ? normalizePathField(input) : input.value;
    setConfigPath(draft, input.dataset.configPath, value);
  });
  draft.output.audio_channels = Number(draft.output.audio_channels);
  return draft;
}

function setConfigPath(target, path, value) {
  const parts = path.split(".");
  const leaf = parts.pop();
  let cursor = target;
  for (const part of parts) cursor = cursor[part];
  cursor[leaf] = value;
}
async function cloneConfig() {
  const suggestedId = `${state.configId}-copy`;
  const newId = window.prompt("新配置 ID（小写英文、数字、短横线）", suggestedId);
  if (!newId) return;
  const newName = window.prompt("新配置名称", `${state.config.name}-副本`);
  if (!newName) return;
  try {
    await api(`/configs/${state.configId}/clone`, { method: "POST", body: JSON.stringify({ new_id: newId.trim(), new_name: newName.trim() }) });
    toast("配置已复制，可在高级配置中修改素材路径");
    await loadConfigs(newId.trim());
  } catch (error) { toast(error.message, true); }
}
async function saveConfig() {
  const button = $("#saveConfigBtn"); button.disabled = true; button.textContent = "校验中…";
  try {
    const lease = state.configLease;
    if (lease?.expiresAt && Date.now() >= lease.expiresAt) {
      await expireConfigLease(lease);
      return;
    }
    if (state.configMode !== "yaml") {
      const sync = ensureFeishuBaseSync(currentStructuredDraft());
      if (sync.enabled) {
        if (!state.feishuSettingsDraft.app_id.trim()) throw new Error("启用飞书同步时必须填写 App ID");
        if (!state.feishuSecretDraft && !state.feishuSettings.app_secret_configured) throw new Error("启用飞书同步时必须填写 App Secret");
      }
    }
    if (feishuCredentialsDirty()) {
      const settings = await api("/integrations/feishu/settings", {
        method: "PUT",
        body: JSON.stringify({
          app_id: state.feishuSettingsDraft.app_id.trim(),
          app_secret: state.feishuSecretDraft || null,
        }),
      });
      state.feishuSettings = settings;
      state.feishuSettingsDraft = structuredClone(settings);
      state.feishuSecretDraft = "";
    }
    let saved;
    if (state.configMode !== "yaml") {
      const config = currentStructuredDraft();
      saved = await api(`/configs/${state.configId}/structured`, {
        method: "PUT",
        headers: configLeaseHeaders(),
        body: JSON.stringify({ config }),
      });
    } else {
      saved = await api(`/configs/${state.configId}`, {
        method: "PUT",
        headers: configLeaseHeaders(),
        body: JSON.stringify({ yaml_text: $("#yamlEditor").value }),
      });
    }
    state.config = saved.config;
    state.configDraft = structuredClone(saved.config);
    state.configHash = saved.content_hash;
    state.yaml = state.configMode === "yaml" ? $("#yamlEditor").value : state.yaml;
    await closeConfig({ skipConfirm: true });
    toast("配置已保存并备份");
    await loadConfigs(state.configId);
  } catch (error) {
    if (!isTerminalConfigLeaseError(error) || !await expireConfigLease()) toast(error.message, true);
  }
  finally { button.disabled = Boolean(state.configLease?.lost); button.textContent = "校验并保存"; }
}

function formatDuration(seconds) { const mins = Math.floor(seconds/60); const secs = Math.round(seconds%60); return mins ? `${mins}m${secs}s` : `${secs}s`; }
function formatDate(value) { try { return new Intl.DateTimeFormat("zh-CN", { month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit" }).format(new Date(value)); } catch (_) { return value; } }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char])); }

document.addEventListener("DOMContentLoaded", init);
