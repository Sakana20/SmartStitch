const state = {
  configs: [],
  configId: null,
  config: null,
  yaml: "",
  scan: null,
  assetCategory: "pre_roll",
  preview: null,
  jobs: [],
  activeJob: null,
  eventSource: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const categoryNames = {
  pre_roll: "前贴",
  hook: "引子",
  benefit_video: "利益点视频",
  ending: "结尾",
  end_card: "尾帧",
  benefit_overlay: "利益点图片",
};
const terminalStates = new Set(["completed", "partial_failed", "failed", "cancelled", "interrupted"]);

async function api(path, options = {}) {
  const response = await fetch(`/api/v1${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
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
  $("#cloneConfigBtn").addEventListener("click", cloneConfig);
  $("#editConfigBtn").addEventListener("click", openConfig);
  $("#saveConfigBtn").addEventListener("click", saveConfig);
  $$('[data-close-modal]').forEach(element => element.addEventListener("click", closeConfig));
  $$('[data-close-drawer]').forEach(element => element.addEventListener("click", closeDrawer));
}

function switchView(view) {
  $$(".section-tab").forEach(button => button.classList.toggle("active", button.dataset.view === view));
  $$(".view").forEach(element => element.classList.toggle("active", element.id === `${view}View`));
  if (view === "jobs") loadJobs();
}

async function loadConfigs(preferredId = null) {
  state.configs = await api("/configs");
  const select = $("#configSelect");
  select.innerHTML = state.configs.map(config => `<option value="${escapeHtml(config.id)}" ${!config.valid ? "disabled" : ""}>${escapeHtml(config.name)}${config.valid ? "" : "（配置错误）"}</option>`).join("");
  const valid = state.configs.filter(config => config.valid);
  if (!valid.length) { toast("没有可用配置", true); return; }
  await selectConfig(preferredId || state.configId || valid[0].id);
}

async function selectConfig(id) {
  state.configId = id;
  $("#configSelect").value = id;
  try {
    const result = await api(`/configs/${id}`);
    state.config = result.config;
    state.yaml = result.yaml_text;
    $("#heroConfigName").textContent = state.config.name;
    $("#countInput").value = state.config.batch.default_count;
    $("#concurrencyInput").value = state.config.batch.concurrency;
    state.preview = null;
    resetPreview();
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
    return `<button class="asset-tab ${state.assetCategory === category ? "active" : ""}" data-category="${category}">${categoryNames[category] || category} · ${count}</button>`;
  }).join("");
  $$(".asset-tab").forEach(button => button.addEventListener("click", () => {
    syncVisibleAssetValues(false);
    state.assetCategory = button.dataset.category; renderAssetTabs(); renderAssets();
  }));
}

function renderAssets() {
  const assets = state.scan?.assets[state.assetCategory] || [];
  const total = assets.filter(asset => asset.enabled && asset.valid).reduce((sum, asset) => sum + Number(asset.weight), 0);
  $("#assetTable").innerHTML = assets.length ? assets.map(asset => {
    const probe = asset.probe;
    const meta = probe ? (asset.media_type === "image" ? `${probe.width}×${probe.height} · 图片` : `${probe.width}×${probe.height} · ${formatDuration(probe.duration)} · ${probe.fps ? probe.fps.toFixed(2) + "fps" : "—"}`) : "无法读取";
    const percent = asset.enabled && asset.valid && total ? (asset.weight / total * 100).toFixed(1) : "0.0";
    return `<tr data-id="${asset.id}">
      <td><input class="check asset-enabled" type="checkbox" ${asset.enabled ? "checked" : ""}></td>
      <td><div class="file-name" title="${escapeHtml(asset.name)}">${escapeHtml(asset.name)}</div><div class="file-path" title="${escapeHtml(asset.path)}">${escapeHtml(asset.path)}</div></td>
      <td><span class="media-meta">${escapeHtml(meta)}</span></td>
      <td><input class="tags-input asset-tags" value="${escapeHtml(asset.tags.join(","))}" placeholder="通用"></td>
      <td><input class="weight-input asset-weight" type="number" min="0" step="0.1" value="${asset.weight}"></td>
      <td>${percent}%</td>
      <td><span class="${asset.valid ? "valid" : "invalid"}" title="${escapeHtml(asset.error || "")}">${asset.valid ? "可用" : "异常"}</span></td>
    </tr>`;
  }).join("") : `<tr><td colspan="7" style="text-align:center;padding:50px;color:var(--muted)">此类别当前没有素材</td></tr>`;
  $$(".asset-weight,.asset-enabled,.asset-tags").forEach(input => input.addEventListener("change", () => syncVisibleAssetValues(true)));
}

function syncVisibleAssetValues(rerender = true) {
  if (!state.scan?.assets[state.assetCategory]) return;
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
  const items = Object.entries(state.scan.assets).flatMap(([category, assets]) => assets.map(asset => ({ category, path: asset.path, enabled: asset.enabled, weight: Number(asset.weight), tags: asset.tags })));
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
      return `<div class="dist-group"><h4>${categoryNames[category] || category}</h4>${rows}</div>`;
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
    <div class="item-list">${job.items.map(item => { const [itemLabel,itemCls] = statusInfo(item.status); return `<div class="item-row"><b>${String(item.index).padStart(2,"0")}</b><div><strong>${escapeHtml(item.output_name)}</strong><div class="mini-progress" style="margin-top:7px"><i style="width:${item.progress*100}%"></i></div></div><span class="status ${itemCls}">${itemLabel}</span>${item.error ? `<div class="error-text">${escapeHtml(item.error)}</div>` : ""}</div>`; }).join("")}</div>`;
  $("#cancelJobBtn")?.addEventListener("click", () => cancelJob(job.id));
}

async function cancelJob(id) {
  try { await api(`/jobs/${id}/cancel`, { method: "POST" }); toast("正在取消任务"); } catch (error) { toast(error.message, true); }
}
function closeDrawer() { $("#jobDrawer").classList.remove("open"); if (state.eventSource) state.eventSource.close(); }

function openConfig() { $("#yamlEditor").value = state.yaml; $("#configModal").classList.add("open"); $("#configModal").setAttribute("aria-hidden","false"); }
function closeConfig() { $("#configModal").classList.remove("open"); $("#configModal").setAttribute("aria-hidden","true"); }
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
    await api(`/configs/${state.configId}`, { method: "PUT", body: JSON.stringify({ yaml_text: $("#yamlEditor").value }) });
    closeConfig(); toast("配置已保存并备份"); await loadConfigs(state.configId);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "校验并保存"; }
}

function formatDuration(seconds) { const mins = Math.floor(seconds/60); const secs = Math.round(seconds%60); return mins ? `${mins}m${secs}s` : `${secs}s`; }
function formatDate(value) { try { return new Intl.DateTimeFormat("zh-CN", { month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit" }).format(new Date(value)); } catch (_) { return value; } }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char])); }

document.addEventListener("DOMContentLoaded", init);
