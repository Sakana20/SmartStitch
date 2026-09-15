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
  activeJob: null,
  eventSource: null,
  configMode: "visual",
  configDraft: null,
  previewAudioCleanup: null,
  timeline: {
    analysis: null,
    breakpoints: [],
    machineBreakpoints: [],
    selectedFrame: null,
    playheadFrame: 0,
    dragging: false,
    drag: null,
    snapTargetFrame: null,
    suppressClickUntil: 0,
    videoFrameCallbackId: null,
    pixelsPerSecond: 30,
    shiftPressed: false,
    dragAnimationFrameId: null,
    pointerOverTimeline: false,
    reviewSaved: false,
    reviewRevision: null,
    selectedSegmentId: null,
    segmentCategories: {},
    sliceTargets: [],
  },
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const categoryNames = {
  pre_roll: "前贴",
  hook: "引子",
  ending: "结尾",
  end_card: "尾帧",
  benefit_overlay: "利益点图片",
};
const benefitCategoryPattern = /^benefit_([1-9][0-9]*)$/;
function isBenefitCategory(category) { return benefitCategoryPattern.test(category); }
function categoryLabel(category) {
  const match = category.match(benefitCategoryPattern);
  return match ? `利益点 ${match[1]}` : (categoryNames[category] || category);
}
const terminalStates = new Set(["completed", "partial_failed", "failed", "cancelled", "interrupted"]);
const configUiStoragePrefix = "smartstitch.config-ui.";

function clientRequestId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}-${Math.random().toString(16).slice(2)}`;
}

async function api(path, options = {}) {
  const response = await fetch(`/api/v1${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try {
      const payload = await response.json();
      if (Array.isArray(payload.detail)) {
        detail = payload.detail.map(item => `${item.loc?.slice(1).join(".") || "配置"}: ${item.msg}`).join("；");
      } else {
        detail = payload.detail || detail;
      }
    } catch (_) {}
    throw new Error(detail);
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
  await loadConfigs();
  await loadJobs();
}

function bindEvents() {
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
  $$(".config-mode-tab").forEach(button => button.addEventListener("click", () => setConfigMode(button.dataset.configMode)));
  $$('[data-close-modal]').forEach(element => element.addEventListener("click", closeConfig));
  $$('[data-close-new-config]').forEach(element => element.addEventListener("click", closeNewConfig));
  $$('[data-close-drawer]').forEach(element => element.addEventListener("click", closeDrawer));
  bindTimelineEvents();
}

function switchView(view) {
  $$(".section-tab").forEach(button => button.classList.toggle("active", button.dataset.view === view));
  $$(".view").forEach(element => element.classList.toggle("active", element.id === `${view}View`));
  if (view === "jobs") loadJobs();
}

function bindTimelineEvents() {
  $("#analyzeTimelineBtn").addEventListener("click", analyzeTimeline);
  $("#previousFrameBtn").addEventListener("click", () => stepTimelineFrame(-1));
  $("#nextFrameBtn").addEventListener("click", () => stepTimelineFrame(1));
  $("#addBreakpointBtn").addEventListener("click", addBreakpointAtPlayhead);
  $("#deleteBreakpointBtn").addEventListener("click", deleteSelectedBreakpoint);
  $("#saveTimelineBtn").addEventListener("click", saveTimelineDecision);
  $("#exportTimelineSlicesBtn").addEventListener("click", exportTimelineSlices);
  $("#selectedFrameInput").addEventListener("change", event => moveSelectedBreakpoint(Number(event.target.value)));
  const video = $("#timelineVideo");
  video.addEventListener("play", startTimelineVideoSync);
  video.addEventListener("pause", syncTimelineFromVideo);
  video.addEventListener("timeupdate", syncTimelineFromVideo);
  video.addEventListener("seeking", syncTimelineFromVideo);
  video.addEventListener("seeked", syncTimelineFromVideo);
  $("#timelineRulerBar").addEventListener("pointerdown", event => {
    const frame = frameFromPointer(event, 0, event.shiftKey);
    seekTimelineFrame(frame, "timeline_scrub");
    beginTimelineDrag(event, { kind: "playhead", anchorFrame: frame });
  });
  const timelineViewport = $("#timelineViewport");
  timelineViewport.addEventListener("scroll", renderTimelineRuler);
  timelineViewport.addEventListener("pointerenter", () => {
    state.timeline.pointerOverTimeline = true;
  });
  timelineViewport.addEventListener("pointerleave", () => {
    state.timeline.pointerOverTimeline = false;
  });
  timelineViewport.addEventListener("wheel", handleTimelineWheel, { passive: false });
  $("#timelineZoomInput").addEventListener("input", event => {
    setTimelineZoom(Number(event.target.value));
  });
  $("#fitTimelineBtn").addEventListener("click", fitTimelineToViewport);
  window.addEventListener("resize", () => {
    if (state.timeline.analysis) renderTimeline();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Shift") {
      state.timeline.shiftPressed = true;
      scheduleTimelineDragFrame();
    }
    if (!$("#timelineView").classList.contains("active")) return;
    if (event.code === "Space" && state.timeline.pointerOverTimeline && state.timeline.analysis) {
      event.preventDefault();
      if (!event.repeat) toggleTimelinePlayback();
      return;
    }
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      stepTimelineFrame(event.key === "ArrowLeft" ? -1 : 1);
    }
  });
  document.addEventListener("keyup", event => {
    if (event.key !== "Shift") return;
    state.timeline.shiftPressed = false;
    scheduleTimelineDragFrame();
  });
}

async function analyzeTimeline() {
  const sourcePath = $("#timelinePathInput").value.trim();
  if (!sourcePath) return toast("请先填写原始视频路径", true);
  const button = $("#analyzeTimelineBtn");
  button.disabled = true;
  button.textContent = "正在分析画面与静音…";
  setTimelineStatus("分析中", "running");
  try {
    const analysis = await api("/timeline/analyze", {
      method: "POST",
      body: JSON.stringify({
        source_path: sourcePath,
        scene_threshold: Number($("#timelineThresholdInput").value),
        silence_duration_seconds: 0.35,
      }),
    });
    state.timeline.analysis = analysis;
    state.timeline.breakpoints = analysis.breakpoints.map(point => ({
      ...point,
      machine_origin_frame: point.frame_index,
    }));
    state.timeline.machineBreakpoints = analysis.breakpoints.map(point => ({ ...point }));
    state.timeline.selectedFrame = null;
    state.timeline.playheadFrame = 0;
    state.timeline.snapTargetFrame = null;
    state.timeline.reviewSaved = false;
    state.timeline.reviewRevision = null;
    state.timeline.selectedSegmentId = null;
    state.timeline.segmentCategories = {};
    stopTimelineVideoSync();
    const video = $("#timelineVideo");
    video.src = analysis.media_url;
    video.load();
    $("#timelineEmpty").classList.add("hidden");
    $("#timelineWorkspace").classList.remove("hidden");
    fitTimelineToViewport(false);
    renderTimeline();
    setTimelineStatus("待人工审核", "running");
    toast(`机器给出了 ${state.timeline.breakpoints.length} 个候选断点`);
  } catch (error) {
    setTimelineStatus("分析失败", "danger");
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "机器粗分析";
  }
}

function setTimelineStatus(text, type) {
  const element = $("#timelineStatus");
  element.textContent = text;
  element.className = `status ${type}`;
}

function renderTimeline() {
  const analysis = state.timeline.analysis;
  if (!analysis) return;
  layoutTimelineCanvas();
  const segments = timelineSegments();
  $("#timelineSegments").innerHTML = segments.map(segment => {
    const left = timelineXForFrame(segment.startFrame);
    const width = timelineXForFrame(segment.endFrame) - left;
    const category = state.timeline.segmentCategories[segment.id] || "pending";
    const selected = state.timeline.selectedSegmentId === segment.id ? "selected" : "";
    return `<span class="${segmentCategoryClass(category)} ${selected}" style="left:${left}px;width:${width}px" title="片段 ${segment.index} · ${escapeHtml(categoryLabelForTimeline(category))} · ${segment.durationFrames} 帧"></span>`;
  }).join("");
  $("#timelineMarkers").innerHTML = state.timeline.breakpoints.map(point => `
    <button class="timeline-marker ${point.frame_index === state.timeline.selectedFrame ? "selected" : ""} ${point.frame_index === state.timeline.snapTargetFrame ? "snap-target" : ""} ${point.review_status === "machine_suggested" ? "machine" : "human"}"
      style="left:${timelineXForFrame(point.frame_index)}px" data-frame="${point.frame_index}"
      title="第 ${point.frame_index} 帧 · ${timelineReasonLabel(point)}"><i></i></button>`).join("");
  $$(".timeline-marker").forEach(marker => bindTimelineMarker(marker));
  $("#timelineMeta").innerHTML = `<span>${escapeHtml(analysis.source_name)}</span><span>${analysis.width}×${analysis.height}</span><span>${analysis.fps.toFixed(3)} fps</span><span>${analysis.frame_count} 帧</span><span>${formatPreciseTime(analysis.duration)}</span>`;
  const assignedCount = segments.filter(segment => state.timeline.segmentCategories[segment.id]).length;
  $("#timelineBreakpointSummary").textContent = `${segments.length} 个片段 · ${state.timeline.breakpoints.length} 个断点 · ${assignedCount} 个已分类 · ${segments.length - assignedCount} 个待分类`;
  renderTimelineRuler();
  renderSegmentList();
  renderSliceAssignments();
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
    state.timeline.selectedFrame = originalFrame;
    state.timeline.selectedSegmentId = null;
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
    const marker = $(`.timeline-marker[data-frame="${drag.originalFrame}"]`);
    if (marker) marker.style.left = `${timelineXForFrame(drag.targetFrame)}px`;
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
      state.timeline.selectedFrame = occupied.frame_index;
    } else if (drag.targetFrame !== drag.originalFrame) {
      drag.point.frame_index = drag.targetFrame;
      drag.point.time_seconds = drag.targetFrame / state.timeline.analysis.fps;
      drag.point.review_status = "human_adjusted";
      drag.point.reasons = ["human_adjusted"];
      state.timeline.selectedFrame = drag.targetFrame;
      sortTimelineBreakpoints();
      timelineReviewChanged();
    } else {
      state.timeline.selectedFrame = drag.originalFrame;
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
  state.timeline.breakpoints.forEach(point => {
    if (point === drag?.point) return;
    const human = point.review_status !== "machine_suggested";
    add(point.frame_index, human ? 3 : 2, human ? "人工断点" : "机器断点");
  });
  state.timeline.machineBreakpoints.forEach(point => {
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
  const base = state.timeline.selectedFrame ?? currentTimelineFrame();
  const next = base + delta;
  if (state.timeline.selectedFrame !== null) moveSelectedBreakpoint(next);
  else seekTimelineFrame(next);
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
  state.timeline.breakpoints.push({
    frame_index: frame,
    time_seconds: frame / analysis.fps,
    reasons: ["human_added"],
    confidence: 1,
    review_status: reviewStatus,
  });
  sortTimelineBreakpoints();
  state.timeline.selectedFrame = frame;
  state.timeline.selectedSegmentId = null;
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
  point.frame_index = frame;
  point.time_seconds = frame / analysis.fps;
  point.review_status = "human_adjusted";
  point.reasons = ["human_adjusted"];
  state.timeline.selectedFrame = frame;
  sortTimelineBreakpoints();
  state.timeline.selectedSegmentId = null;
  timelineReviewChanged();
  seekTimelineFrame(frame);
  renderTimeline();
}

function deleteSelectedBreakpoint() {
  const frame = state.timeline.selectedFrame;
  if (frame === null) return;
  state.timeline.breakpoints = state.timeline.breakpoints.filter(point => point.frame_index !== frame);
  state.timeline.selectedFrame = null;
  timelineReviewChanged();
  renderTimeline();
}

function timelineReviewChanged() {
  state.timeline.reviewSaved = false;
  state.timeline.reviewRevision = null;
  const validSegmentIds = new Set(timelineSegments().map(segment => segment.id));
  state.timeline.segmentCategories = Object.fromEntries(
    Object.entries(state.timeline.segmentCategories).filter(([segmentId]) => validSegmentIds.has(segmentId)),
  );
  if (!validSegmentIds.has(state.timeline.selectedSegmentId)) {
    state.timeline.selectedSegmentId = null;
  }
  $("#timelineSliceResult").textContent = "断点已变更，请重新保存审核后入库";
  $("#timelineSliceResult").className = "timeline-slice-result";
}

function selectTimelineBreakpoint(frame) {
  state.timeline.selectedFrame = frame;
  state.timeline.selectedSegmentId = null;
  renderTimeline();
}

function sortTimelineBreakpoints() {
  state.timeline.breakpoints.sort((a, b) => a.frame_index - b.frame_index);
}

function syncSelectedBreakpointControls() {
  const selected = state.timeline.selectedFrame;
  const input = $("#selectedFrameInput");
  input.disabled = selected === null;
  input.value = selected ?? "";
  input.max = String((state.timeline.analysis?.frame_count || 1) - 1);
  $("#deleteBreakpointBtn").disabled = selected === null;
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
  const boundaries = [0, ...state.timeline.breakpoints.map(point => point.frame_index), analysis.frame_count];
  return boundaries.slice(0, -1).map((startFrame, offset) => {
    const endFrame = boundaries[offset + 1];
    const endpoint = state.timeline.breakpoints[offset];
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
    { category: "skip", label: "不入库" },
    { category: "unclassified", label: "未归类" },
  ];
  if (!state.config) return options;
  state.config.timeline.forEach(category => {
    if (
      state.config.sources[category]
      && (category in categoryNames || isBenefitCategory(category))
    ) {
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

function selectTimelineSegment(segmentId, scrollIntoView = false) {
  const segment = timelineSegments().find(item => item.id === segmentId);
  if (!segment) return;
  state.timeline.selectedSegmentId = segment.id;
  state.timeline.selectedFrame = null;
  $("#timelineVideo").pause();
  seekTimelineFrame(segment.startFrame, "segment_select");
  renderTimeline();
  if (scrollIntoView) {
    requestAnimationFrame(() => {
      const row = $$(".segment-row").find(item => item.dataset.segmentId === segmentId);
      row?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  }
}

function renderSegmentList() {
  const analysis = state.timeline.analysis;
  const segments = timelineSegments();
  const categoryOptions = timelineCategoryOptions();
  $("#timelineSegmentList").innerHTML = segments.map(segment => {
    const selectedCategory = state.timeline.segmentCategories[segment.id] || "";
    const options = categoryOptions.map(option => (
      `<option value="${escapeHtml(option.category)}" ${selectedCategory === option.category ? "selected" : ""}>${escapeHtml(option.label)}</option>`
    )).join("");
    return `<div class="segment-row ${state.timeline.selectedSegmentId === segment.id ? "selected" : ""}" data-segment-id="${segment.id}" tabindex="0">
      <span>${String(segment.index).padStart(2, "0")}</span>
      <div class="segment-row-time"><strong>${frameTimecode(segment.startFrame, analysis.fps)}</strong><b>→</b><strong>${frameTimecode(segment.endFrame, analysis.fps)}</strong></div>
      <small>${segment.durationFrames} 帧 · ${formatPreciseTime(segment.durationFrames / analysis.fps)}</small>
      <select class="segment-type-select ${selectedCategory ? "" : "pending"}" data-segment-category="${segment.id}" ${state.configId ? "" : "disabled"} aria-label="片段 ${segment.index} 类型">
        <option value="" disabled ${selectedCategory ? "" : "selected"}>选择类型</option>${options}
      </select>
      <i>结束：${escapeHtml(segment.endReason)}</i>
    </div>`;
  }).join("");
  $$(".segment-row").forEach(row => {
    row.addEventListener("click", event => {
      if (event.target.closest("select")) return;
      selectTimelineSegment(row.dataset.segmentId);
    });
    row.addEventListener("keydown", event => {
      if ((event.key === "Enter" || event.key === " ") && !event.target.closest("select")) {
        event.preventDefault();
        selectTimelineSegment(row.dataset.segmentId);
      }
    });
  });
  $$("[data-segment-category]").forEach(select => {
    select.addEventListener("click", event => event.stopPropagation());
    select.addEventListener("change", event => {
      state.timeline.segmentCategories[event.target.dataset.segmentCategory] = event.target.value;
      state.timeline.selectedSegmentId = event.target.dataset.segmentCategory;
      renderTimeline();
    });
  });
}

function renderSliceAssignments() {
  const analysis = state.timeline.analysis;
  const container = $("#timelineSliceAssignments");
  const button = $("#exportTimelineSlicesBtn");
  if (!analysis) {
    container.innerHTML = '<div class="breakpoint-empty">分析视频后，已归类片段会出现在这里。</div>';
    button.disabled = true;
    return;
  }

  const healthyLibrary = Boolean(state.library?.managed && state.library.health === "healthy");
  if (!state.configId) {
    $("#timelineSliceLibrary").textContent = "请先选择配置";
  } else if (healthyLibrary) {
    $("#timelineSliceLibrary").textContent = `入库到：${state.config.name} · ${state.library.root_path}`;
  } else {
    $("#timelineSliceLibrary").textContent = "已归类；执行切片前需选择或修复 SmartStitch 标准视频库";
  }

  const segments = timelineSegments();
  const optionOrder = [
    { category: "pending", label: "待分类" },
    ...timelineCategoryOptions(),
  ];
  const groups = new Map(optionOrder.map(option => [option.category, { ...option, items: [] }]));
  segments.forEach(segment => {
    const category = state.timeline.segmentCategories[segment.id] || "pending";
    if (!groups.has(category)) {
      groups.set(category, { category, label: categoryLabelForTimeline(category), items: [] });
    }
    groups.get(category).items.push(segment);
  });
  container.innerHTML = [...groups.values()].filter(group => group.items.length).map(group => `
    <section class="slice-category-group ${group.category === "pending" ? "pending" : ""}">
      <div class="slice-category-heading"><strong>${escapeHtml(group.label)}</strong><span>${group.items.length}</span></div>
      <div class="slice-category-items">${group.items.map(segment => `
        <button class="slice-category-item" type="button" data-queued-segment="${segment.id}">
          <strong>片段 ${String(segment.index).padStart(2, "0")}</strong>
          <small>${frameTimecode(segment.startFrame, analysis.fps)} → ${frameTimecode(segment.endFrame, analysis.fps)} · ${formatPreciseTime(segment.durationFrames / analysis.fps)}</small>
        </button>`).join("")}</div>
    </section>`).join("");
  $$("[data-queued-segment]").forEach(item => item.addEventListener("click", () => {
    selectTimelineSegment(item.dataset.queuedSegment, true);
  }));

  const pendingCount = segments.filter(segment => !state.timeline.segmentCategories[segment.id]).length;
  const cuttableCount = segments.filter(segment => {
    const category = state.timeline.segmentCategories[segment.id];
    return category && category !== "skip";
  }).length;
  button.disabled = !state.timeline.reviewSaved
    || !state.timeline.reviewRevision
    || pendingCount > 0
    || !healthyLibrary
    || cuttableCount === 0;
}

async function exportTimelineSlices() {
  const analysis = state.timeline.analysis;
  if (!analysis || !state.configId) return;
  if (!state.timeline.reviewSaved) return toast("请先保存当前审核断点", true);
  if (!state.timeline.reviewRevision) return toast("审核版本缺失，请重新保存断点", true);
  if (!state.library?.managed || state.library.health !== "healthy") {
    return toast("请先选择或修复 SmartStitch 标准视频库", true);
  }
  const segments = timelineSegments();
  const pending = segments.find(segment => !state.timeline.segmentCategories[segment.id]);
  if (pending) {
    selectTimelineSegment(pending.id, true);
    return toast(`请先为片段 ${pending.index} 选择类型`, true);
  }
  const assignments = segments.map(segment => ({
    segment_index: segment.index,
    category: state.timeline.segmentCategories[segment.id],
  }));
  const cuttableCount = assignments.filter(item => item.category !== "skip").length;
  if (!cuttableCount) return toast("至少需要一个非“不入库”片段", true);
  const button = $("#exportTimelineSlicesBtn");
  const resultElement = $("#timelineSliceResult");
  button.disabled = true;
  button.textContent = "正在批量切割…";
  resultElement.textContent = `正在用 FFmpeg 切割 ${cuttableCount} 个片段，请稍候…`;
  resultElement.className = "timeline-slice-result";
  try {
    const result = await api("/timeline/slices", {
      method: "POST",
      body: JSON.stringify({
        analysis_id: analysis.analysis_id,
        config_id: state.configId,
        review_revision: state.timeline.reviewRevision,
        current_config_hash: state.configHash,
        assignments,
        client_request_id: clientRequestId(),
      }),
    });
    resultElement.textContent = `入库完成：${result.success_count} 个成功，${result.failure_count} 个失败，${result.skipped_count || 0} 个不入库 · 清单 ${result.manifest_path}`;
    resultElement.className = `timeline-slice-result ${result.failure_count ? "error" : "success"}`;
    toast(result.failure_count ? "切片部分完成，请查看清单" : "全部片段已切割并入库", Boolean(result.failure_count));
    await scanAssets(false);
  } catch (error) {
    resultElement.textContent = error.message;
    resultElement.className = "timeline-slice-result error";
    toast(error.message, true);
  } finally {
    button.textContent = "切割并入库";
    renderSliceAssignments();
  }
}

function timelineReasonLabel(point) {
  if (point.review_status === "human_added") return "人工添加";
  if (point.review_status === "human_adjusted") return "人工调整";
  const labels = {
    scene_change: "画面转场",
    silence_end: "静音结束",
    human_added: "人工添加",
    human_adjusted: "人工调整",
  };
  return (point.reasons || []).map(reason => labels[reason] || reason).join(" + ") || "机器建议";
}

async function saveTimelineDecision() {
  const analysis = state.timeline.analysis;
  if (!analysis) return;
  const button = $("#saveTimelineBtn");
  button.disabled = true;
  button.textContent = "保存中…";
  try {
    const result = await api("/timeline/decisions", {
      method: "PUT",
      body: JSON.stringify({
        analysis_id: analysis.analysis_id,
        frame_indexes: state.timeline.breakpoints.map(point => point.frame_index),
      }),
    });
    state.timeline.breakpoints.forEach(point => { point.review_status = "human_confirmed"; });
    state.timeline.reviewSaved = true;
    state.timeline.reviewRevision = result.review_revision;
    setTimelineStatus("已保存审核", "success");
    renderTimeline();
    toast("审核断点已按帧保存");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "保存人工审核"; }
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
    if (state.library?.managed && state.library.health === "healthy") {
      try {
        const targets = await api(`/libraries/by-config/${id}/slice-targets`);
        state.timeline.sliceTargets = targets.targets;
      } catch (_) {
        state.timeline.sliceTargets = [];
      }
    } else {
      state.timeline.sliceTargets = [];
    }
    state.timeline.segmentCategories = {};
    state.timeline.selectedSegmentId = null;
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
  finally { button.disabled = false; button.textContent = "重新扫描"; }
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
  const fixedOverlay = state.assetCategory === "benefit_overlay";
  const total = assets.filter(asset => asset.enabled && asset.valid).reduce((sum, asset) => sum + Number(asset.weight), 0);
  $("#assetTable").innerHTML = assets.length ? assets.map(asset => {
    const probe = asset.probe;
    const meta = probe ? (asset.media_type === "image" ? `${probe.width}×${probe.height} · 图片` : `${probe.width}×${probe.height} · ${formatDuration(probe.duration)} · ${probe.fps ? probe.fps.toFixed(2) + "fps" : "—"}`) : "无法读取";
    const percent = asset.enabled && asset.valid && total ? (asset.weight / total * 100).toFixed(1) : "0.0";
    return `<tr data-id="${asset.id}">
      <td>${fixedOverlay ? '<span class="valid">固定</span>' : `<input class="check asset-enabled" type="checkbox" ${asset.enabled ? "checked" : ""}>`}</td>
      <td><div class="file-name" title="${escapeHtml(asset.name)}">${escapeHtml(asset.name)}</div><div class="file-path" title="${escapeHtml(asset.path)}">${escapeHtml(asset.path)}</div></td>
      <td><span class="media-meta">${escapeHtml(meta)}</span></td>
      <td>${fixedOverlay ? "—" : `<input class="tags-input asset-tags" value="${escapeHtml(asset.tags.join(","))}" placeholder="通用">`}</td>
      <td>${fixedOverlay ? "不参与随机" : `<input class="weight-input asset-weight" type="number" min="0" step="0.1" value="${asset.weight}">`}</td>
      <td>${fixedOverlay ? "100%" : `${percent}%`}</td>
      <td><span class="${asset.valid ? "valid" : "invalid"}" title="${escapeHtml(asset.error || "")}">${asset.valid ? "可用" : "异常"}</span></td>
    </tr>`;
  }).join("") : `<tr><td colspan="7" style="text-align:center;padding:50px;color:var(--muted)">此类别当前没有素材</td></tr>`;
  $$(".asset-weight,.asset-enabled,.asset-tags").forEach(input => input.addEventListener("change", () => syncVisibleAssetValues(true)));
}

function syncVisibleAssetValues(rerender = true) {
  if (!state.scan?.assets[state.assetCategory]) return;
  if (state.assetCategory === "benefit_overlay") return;
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
    .filter(([category]) => category !== "benefit_overlay")
    .flatMap(([category, assets]) => assets.map(asset => ({ category, path: asset.path, enabled: asset.enabled, weight: Number(asset.weight), tags: asset.tags })));
  try {
    await api(`/configs/${state.configId}/weights`, { method: "POST", body: JSON.stringify({ items }) });
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
    output_directory: $("#outputInput").value.trim() || null,
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
  $("#deleteAllJobsBtn").disabled = state.jobs.length === 0;
  $("#deleteAllJobsCount").textContent = String(state.jobs.length);
  if (!state.jobs.length) hideDeleteAllJobsConfirm();
  if (!state.jobs.length) { list.innerHTML = `<div class="empty-state"><h3>还没有生成任务</h3><p>预览组合后，点击“开始生成”即可在这里查看进度。</p></div>`; return; }
  list.innerHTML = state.jobs.map(job => {
    const [label, cls] = statusInfo(job.status); const done = job.success_count + job.failure_count; const pct = job.count ? done/job.count*100 : 0;
    return `<div class="job-row" data-job-id="${job.id}"><div><strong>${escapeHtml(job.config_name)}</strong><small>${formatDate(job.created_at)} · #${job.short_id}</small></div><div><div class="mini-progress"><i style="width:${pct}%"></i></div><small>${done}/${job.count} 已处理</small></div><span class="status ${cls}">${label}</span><span class="job-count">成功 ${job.success_count} · 失败 ${job.failure_count}</span><b>›</b></div>`;
  }).join("");
  $$(".job-row").forEach(row => row.addEventListener("click", () => openJob(row.dataset.jobId)));
}

async function openJob(jobId) {
  $("#jobDrawer").classList.add("open"); $("#jobDrawer").setAttribute("aria-hidden", "false");
  if (state.eventSource) state.eventSource.close();
  const update = job => { state.activeJob = job; renderJobDetail(job); };
  try { update(await api(`/jobs/${jobId}`)); } catch (error) { toast(error.message, true); return; }
  if (!terminalStates.has(state.activeJob.status)) {
    state.eventSource = new EventSource(`/api/v1/jobs/${jobId}/events`);
    state.eventSource.addEventListener("job_update", event => {
      const job = JSON.parse(event.data); update(job); loadJobs();
      if (terminalStates.has(job.status)) state.eventSource.close();
    });
  }
}

function renderJobDetail(job) {
  const [label, cls] = statusInfo(job.status); const done = job.success_count + job.failure_count; const totalProgress = job.items.reduce((sum, item) => sum + item.progress, 0) / job.count * 100;
  $("#jobDetail").innerHTML = `<div class="job-detail-header"><p class="eyebrow">BATCH #${job.short_id}</p><h2>${escapeHtml(job.config_name)}</h2><span class="status ${cls}">${label}</span><p>${escapeHtml(job.output_directory)}</p></div>
    <div class="big-progress"><div><span>总体进度</span><b>${totalProgress.toFixed(1)}%</b></div><div class="bar"><i style="width:${totalProgress}%"></i></div></div>
    <div class="seed-card"><span>随机种子</span><strong>${job.seed}</strong></div>
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
function closeDrawer() { $("#jobDrawer").classList.remove("open"); if (state.eventSource) state.eventSource.close(); }

function openNewConfig() {
  $("#newConfigIdInput").value = "";
  $("#newConfigNameInput").value = "";
  $("#newLibraryFolderInput").value = "";
  $("#newLibraryFolderInput").dataset.automatic = "true";
  $("#newLibraryParentInput").value = "";
  updateLibraryCreatePreview();
  $("#newConfigModal").classList.add("open");
  $("#newConfigModal").setAttribute("aria-hidden", "false");
  requestAnimationFrame(() => $("#newConfigIdInput").focus());
}

function updateLibraryCreatePreview() {
  const newId = $("#newConfigIdInput").value.trim();
  const newName = $("#newConfigNameInput").value.trim();
  const folder = $("#newLibraryFolderInput").value.trim();
  const parent = $("#newLibraryParentInput").value.trim();
  const complete = /^[a-z0-9][a-z0-9-]*$/.test(newId) && newName && folder && parent;
  $("#createConfigBtn").disabled = !complete;
  $("#newLibraryFinalPath").textContent = parent && folder
    ? `${parent.replace(/\/+$/, "")}/${folder}`
    : "请先选择保存位置";
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
  const parentDirectory = $("#newLibraryParentInput").value.trim();
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
      client_request_id: clientRequestId(),
    };
    await api("/libraries/preflight", {
      method: "POST",
      body: JSON.stringify({ parent_directory: parentDirectory, folder_name: folderName }),
    });
    await api("/libraries", { method: "POST", body: JSON.stringify(payload) });
    closeNewConfig();
    await loadConfigs(newId);
    toast("标准视频库和全部文件夹已创建");
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
    await api(`/configs/${deletedId}`, { method: "DELETE" });
    try { localStorage.removeItem(`${configUiStoragePrefix}${deletedId}`); } catch (_) {}
    state.configId = null;
    hideDeleteConfigConfirm();
    await loadConfigs();
    toast("配置已删除，并已保留可恢复备份");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "确认删除"; }
}

function openConfig() {
  if (!state.config) return;
  state.configDraft = structuredClone(state.config);
  $("#yamlEditor").value = state.yaml;
  renderVisualConfig();
  restoreConfigUiPreferences();
  $("#configModal").classList.add("open");
  $("#configModal").setAttribute("aria-hidden","false");
  requestAnimationFrame(restoreConfigEditorScroll);
}
function closeConfig() {
  saveConfigUiPreferences();
  const audio = $("#loudnessPreviewAudio");
  if (audio) { audio.pause(); audio.removeAttribute("src"); audio.load(); }
  state.previewAudioCleanup?.();
  state.previewAudioCleanup = null;
  $("#configModal").classList.remove("open");
  $("#configModal").setAttribute("aria-hidden","true");
}

function setConfigMode(mode) {
  state.configMode = mode;
  $$(".config-mode-tab").forEach(button => button.classList.toggle("active", button.dataset.configMode === mode));
  $("#visualConfigEditor").classList.toggle("hidden", mode !== "visual");
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
    mode: state.configMode,
    sections,
    visualScrollTop: $("#visualConfigEditor")?.scrollTop || 0,
    yamlScrollTop: $("#yamlEditor")?.scrollTop || 0,
  };
  try { localStorage.setItem(configUiStorageKey(), JSON.stringify(preferences)); } catch (_) {}
}

function restoreConfigUiPreferences() {
  const preferences = readConfigUiPreferences();
  $$("#visualConfigEditor details[data-config-section]").forEach(section => {
    const saved = preferences.sections?.[section.dataset.configSection];
    if (typeof saved === "boolean") section.open = saved;
    section.addEventListener("toggle", saveConfigUiPreferences);
  });
  setConfigMode(preferences.mode === "yaml" ? "yaml" : "visual");
}

function restoreConfigEditorScroll() {
  const preferences = readConfigUiPreferences();
  if ($("#visualConfigEditor")) $("#visualConfigEditor").scrollTop = Number(preferences.visualScrollTop || 0);
  if ($("#yamlEditor")) $("#yamlEditor").scrollTop = Number(preferences.yamlScrollTop || 0);
}

function configInput(label, path, value, options = {}) {
  const { type = "text", hint = "", wide = false, placeholder = "", className = "" } = options;
  const dataType = type === "number" ? "number" : type === "nullable-number" ? "nullable-number" : type === "list" ? "list" : "string";
  const inputType = ["number", "nullable-number"].includes(type) ? "number" : "text";
  const renderedValue = Array.isArray(value) ? value.join(", ") : (value ?? "");
  return `<div class="config-field ${wide ? "wide" : ""} ${className}"><label>${label}${hint ? `<small>${hint}</small>` : ""}</label><input type="${inputType}" data-config-path="${path}" data-config-type="${dataType}" value="${escapeHtml(renderedValue)}" placeholder="${escapeHtml(placeholder)}" ${inputType === "number" ? 'step="any"' : ""}></div>`;
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
  const modeChoices = [["required", "必需"], ["optional", "可选"], ["disabled", "停用"]];
  const orderedCategories = [
    ...config.timeline,
    ...Object.keys(config.sources).filter(category => !config.timeline.includes(category)),
  ];
  const benefitCategories = config.timeline.filter(isBenefitCategory);
  const sourceCards = orderedCategories.map(category => {
    const group = config.sources[category];
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
        ${configInput("素材目录", `sources.${category}.directory`, group.directory, { wide: true })}
        ${category === "end_card" ? configInput("静态尾帧时长", `sources.${category}.image_duration_seconds`, group.image_duration_seconds, { type: "number", hint: "秒" }) : ""}
      </div>
    </div>`;
  }).join("");

  const overlay = config.benefit_overlays;
  const output = config.output;
  const loudness = output.loudness || {
    enabled: false,
    target_lufs: -14,
    loudness_range_lu: 7,
    true_peak_dbtp: -1.5,
    preview_duration_seconds: 12,
  };
  const batch = config.batch;
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
        ${configInput("时间线顺序", "timeline", config.timeline, { type: "list", hint: "逗号分隔" })}
        ${configInput("主素材根目录", "source_root", config.source_root, { wide: true })}
        ${configTextarea("配置说明", "description", config.description)}
      </div>
    </details>

    <details class="config-section" data-config-section="sources" open>
      <summary>视频素材 <small>利益点段可独立增删和排序</small></summary>
      <div class="config-section-body">
        <div class="benefit-config-toolbar"><div><strong>多人利益点</strong><small>每段从自己的素材池中抽取 1 个片段</small></div><button id="addBenefitBtn" class="button secondary small" type="button" ${benefitCategories.length >= 20 ? "disabled" : ""}>+添加利益点</button></div>
        ${sourceCards}
      </div>
    </details>

    <details class="config-section" data-config-section="benefit-overlay" open>
      <summary>利益点图片 <small>最高图层叠加设置</small></summary>
      <div class="config-section-body config-form-grid three">
        ${configSelect("使用方式", "benefit_overlays.mode", overlay.mode, modeChoices)}
        ${configInput("唯一图片文件", "benefit_overlays.file", overlay.file, { wide: true, hint: "固定 · 不参与随机", placeholder: "/路径/利益点图片.png" })}
        ${configSelect("缩放方式", "benefit_overlays.placement.scale_mode", overlay.placement.scale_mode, [["original", "保持原尺寸"], ["fit", "等比适配画布"], ["stretch", "拉伸铺满"]])}
        ${configInput("整体透明度", "benefit_overlays.placement.opacity", overlay.placement.opacity, { type: "number", hint: "0～1" })}
        ${configSwitch("超出画布时自动缩小", "benefit_overlays.placement.shrink_if_oversized", overlay.placement.shrink_if_oversized)}
        ${configInput("横向位置 X", "benefit_overlays.placement.x", overlay.placement.x, { placeholder: "0 或 (W-w)/2" })}
        ${configInput("纵向位置 Y", "benefit_overlays.placement.y", overlay.placement.y, { placeholder: "0 或 (H-h)/2" })}
        ${configSelect("显示时段", "benefit_overlays.timing.scope", overlay.timing.scope, [["full", "整条成片"], ["main", "主片段"], ["benefits", "全部利益点段"], ["custom", "自定义时段"]])}
        ${configInput("自定义开始", "benefit_overlays.timing.start_seconds", overlay.timing.start_seconds, { type: "number", hint: "秒" })}
        ${configInput("自定义结束", "benefit_overlays.timing.end_seconds", overlay.timing.end_seconds, { type: "nullable-number", hint: "留空到片尾" })}
      </div>
    </details>

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
        ${configSelect("核心组合去重策略", "randomization.duplicate_policy", config.randomization.duplicate_policy, [["allow", "允许重复（权重优先）"], ["best_effort", "尽量去重（权重优先）— 推荐"], ["strict", "严格去重（组合优先）"]], "引子 + 全部利益点段 + 结尾", {
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
        ${configInput("默认输出目录", "output.directory", output.directory, { wide: true })}
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
        ${configInput("文件名模板", "output.filename_template", output.filename_template, { wide: true })}
        ${configSelect("重名处理", "output.collision_policy", output.collision_policy, [["increment", "自动递增"], ["error", "报错"], ["overwrite", "覆盖"]])}
        ${configSwitch("启用 Faststart", "output.faststart", output.faststart)}
      </div>
    </details>

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
  bindBenefitConfigControls();
  bindOutputControls();
}

function mutateBenefitConfig(mutator) {
  state.configDraft = collectVisualConfig();
  const scrollTop = $("#visualConfigEditor").scrollTop;
  mutator(state.configDraft);
  renderVisualConfig();
  $("#visualConfigEditor").scrollTop = scrollTop;
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

  const scrollTop = $("#visualConfigEditor").scrollTop;
  button.disabled = true;
  button.textContent = "正在创建文件夹…";
  try {
    const draft = collectVisualConfig();
    const saved = await api(`/configs/${state.configId}/structured`, {
      method: "PUT",
      body: JSON.stringify({ config: draft }),
    });
    const added = await api(`/configs/${state.configId}/benefits`, {
      method: "POST",
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
    renderVisualConfig();
    if (state.timeline.analysis) renderTimeline();
    $("#visualConfigEditor").scrollTop = scrollTop;
    const warning = added.warnings?.length ? `；${added.warnings.join("；")}` : "";
    toast(`${categoryLabel(added.category)}已创建，文件夹：${added.directory}${warning}`, Boolean(added.warnings?.length));
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
    button.textContent = "+添加利益点";
  }
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
  loudnessSwitch?.addEventListener("change", syncConditionalOutputFields);
  rateControl?.addEventListener("change", syncConditionalOutputFields);
  syncConditionalOutputFields();
}

function syncConditionalOutputFields() {
  const loudnessEnabled = $('[data-config-path="output.loudness.enabled"]')?.checked ?? false;
  $$(".loudness-option").forEach(field => field.classList.toggle("hidden", !loudnessEnabled));

  const rateControl = $('[data-config-path="output.rate_control"]')?.value;
  $$(".rate-vbr").forEach(field => field.classList.toggle("hidden", rateControl !== "vbr"));
  $$(".rate-crf").forEach(field => field.classList.toggle("hidden", rateControl !== "crf"));
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
    else value = input.value;
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
    if (state.configMode === "visual") {
      const config = collectVisualConfig();
      await api(`/configs/${state.configId}/structured`, { method: "PUT", body: JSON.stringify({ config }) });
    } else {
      await api(`/configs/${state.configId}`, { method: "PUT", body: JSON.stringify({ yaml_text: $("#yamlEditor").value }) });
    }
    closeConfig(); toast("配置已保存并备份"); await loadConfigs(state.configId);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "校验并保存"; }
}

function formatDuration(seconds) { const mins = Math.floor(seconds/60); const secs = Math.round(seconds%60); return mins ? `${mins}m${secs}s` : `${secs}s`; }
function formatDate(value) { try { return new Intl.DateTimeFormat("zh-CN", { month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit" }).format(new Date(value)); } catch (_) { return value; } }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char])); }

document.addEventListener("DOMContentLoaded", init);
