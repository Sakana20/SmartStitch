const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync(require("node:path").join(__dirname, "../frontend/app.js"), "utf8");
const mount = app.slice(app.indexOf("function mountPortraitTool(container,"), app.indexOf("\nasync function openPortraitJob"));
const jobs = app.slice(app.indexOf("function renderJobs()"), app.indexOf("\nasync function openUpscaleJob"));

async function main(landscape = false, mode = "local") {
  const title = landscape ? "竖改横" : "横改竖";
  const lowSize = landscape ? "1280×720" : "720×1280";
  const highSize = landscape ? "1920×1080" : "1080×1920";
  const elements = new Map();
  const element = selector => {
    if (!elements.has(selector)) elements.set(selector, { value: "", disabled: false, innerHTML: "", handlers: {}, attributes: {},
      classList: { values: new Set(), toggle(name, active) { active ? this.values.add(name) : this.values.delete(name); } },
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(event, callback) { this.handlers[event] = callback; } });
    return elements.get(selector);
  };
  const container = { innerHTML: "", querySelector: element };
  let pendingResolve;
  let request;
  let opened;
  let chosenView;
  const context = { container, landscape, mode, requestClusterStart: async () => null, toast: () => {}, escapeHtml: value => String(value ?? ""),
    api: async (path, options) => {
      assert.ok(!path.includes("cluster-control/status"), "转换面板不应重复探测集群状态");
      request = { path, body: JSON.parse(options.body) };
      if (path.endsWith("/preview")) return new Promise(resolve => { pendingResolve = resolve; });
      return { id: "job-1" };
    }, loadJobs: async () => {}, switchView: view => { chosenView = view; },
    openPortraitJob: async id => { opened = id; } };
  vm.createContext(context);
  vm.runInContext(mount, context);
  const dispose = vm.runInContext("mountPortraitTool(container, landscape, mode)", context);
  if (mode === "cluster") {
    await Promise.resolve();
    assert.doesNotMatch(container.innerHTML, /执行工作机|data-conversion-nodes/);
    assert.match(container.innerHTML, /自动使用所有支持转换的在线工作机/);
    assert.doesNotMatch(container.innerHTML, /id="portraitSource"|id="portraitOutput"|选择文件夹|本机素材/);
    assert.ok(container.innerHTML.includes(`/Volumes/home/Smartstitch/${title}/原素材`));
    assert.ok(container.innerHTML.includes(`/Volumes/home/Smartstitch/${title}/已处理`));
  }
  const source = element("#portraitSource");
  const lowResolution = element("#portrait720");
  const highResolution = element("#portrait1080");
  const read = element("#portraitRead");
  const start = element("#portraitStart");
  const result = element("#portraitPreview");
  const preview = { id: "preview-1", pending_count: 1, skipped_count: 0, invalid_count: 0,
    output_width: landscape ? 1280 : 720, output_height: landscape ? 720 : 1280, output_directory: "/source/横改竖", items: [{ name: "clip.mp4", status: "pending" }] };
  source.value = "/source";
  const reading = read.handlers.click();
  assert.equal(request.body.resolution, "720p");
  if (mode === "cluster") {
    assert.equal(request.body.mode, "cluster");
    assert.equal(request.body.source_directory, undefined);
    assert.equal(request.body.output_directory, undefined);
  }
  assert.equal(request.path, `/tools/${landscape ? "portrait-to-landscape" : "landscape-to-portrait"}/preview`);
  assert.equal(read.textContent, "正在读取文件列表…");
  source.value = "/new-source";
  if (mode === "local") source.handlers.input();
  else { highResolution.handlers.click(); lowResolution.handlers.click(); }
  pendingResolve(preview);
  await reading;
  assert.equal(result.innerHTML, "", "changing folder must discard late preflight response");
  assert.equal(start.disabled, true);
  const newReading = read.handlers.click();
  pendingResolve(preview);
  await newReading;
  assert.equal(start.disabled, false);
  assert.match(result.innerHTML, /待处理 1 条/);
  assert.match(result.innerHTML, /开始后逐个检查视频信息/);
  assert.doesNotMatch(result.innerHTML, /undefined|NaN|fps|无声音/);
  lowResolution.handlers.click();
  assert.equal(start.disabled, false, "clicking the current resolution preserves preflight");
  highResolution.handlers.click();
  assert.equal(highResolution.attributes["aria-pressed"], "true");
  assert.equal(lowResolution.attributes["aria-pressed"], "false");
  assert.equal(highResolution.classList.values.has("primary"), true);
  assert.equal(lowResolution.classList.values.has("secondary"), true);
  assert.equal(start.disabled, true, "resolution changes invalidate preflight");
  assert.equal(result.innerHTML, "");
  const highResolutionReading = read.handlers.click();
  assert.equal(request.body.resolution, "1080p");
  pendingResolve({ ...preview, output_width: landscape ? 1920 : 1080, output_height: landscape ? 1080 : 1920 });
  await highResolutionReading;
  assert.ok(result.innerHTML.includes(`输出规格：${highSize}`));
  await start.handlers.click();
  assert.equal(request.body.preview_id, "preview-1");
  assert.equal(request.body.mode, mode);
  assert.equal(request.body.node_ids, undefined);
  assert.equal(chosenView, "jobs");
  assert.equal(opened, "job-1");
  assert.equal(start.disabled, true, "consumed preflight cannot be submitted twice");
  const staleReading = read.handlers.click();
  dispose();
  pendingResolve(preview);
  await staleReading;
  assert.equal(result.innerHTML, "", "leaving tool discards its outstanding response");

  let rowClick;
  const list = { innerHTML: "" };
  const renderContext = { state: { jobs: [], sliceJobs: [], upscaleJobs: [], [landscape ? "landscapeJobs" : "portraitJobs"]: [{
    id: "job-1", status: "running", created_at: "2026-09-28", total: 2,
    completed: 1, current_progress: .5, succeeded: 1, failed: 0,
  }] }, $: selector => selector === "#jobsList" ? list : {},
    $$: () => [{ dataset: { jobType: landscape ? "landscape" : "portrait", jobId: "job-1" }, addEventListener: (_, fn) => { rowClick = fn; } }],
    statusInfo: () => ["生成中", "running"], escapeHtml: value => String(value), formatDate: () => "09/28",
    hideDeleteAllJobsConfirm: () => {}, openPortraitJob: id => { opened = id; }, openLandscapeJob: id => { opened = id; } };
  vm.createContext(renderContext);
  vm.runInContext(jobs, renderContext);
  vm.runInContext("renderJobs()", renderContext);
  assert.ok(list.innerHTML.includes(`${title}批次`));
  assert.match(list.innerHTML, /width:75%/);
  opened = null;
  rowClick();
  assert.equal(opened, "job-1");
  const detail = { innerHTML: "" };
  const detailSource = app.slice(app.indexOf("function renderPortraitJobDetail(job, landscape = false)"), app.indexOf("\nasync function loadJobs()"));
  const detailContext = { landscape, $: selector => selector === "#jobDetail" ? detail : null,
    statusInfo: () => ["已完成", "success"], escapeHtml: value => String(value ?? "") };
  vm.createContext(detailContext);
  vm.runInContext(detailSource, detailContext);
  detailContext.job = { id: "job-1080", status: "completed", output_width: landscape ? 1920 : 1080, output_height: landscape ? 1080 : 1920,
    total: 1, completed: 1, succeeded: 1, failed: 0, items: [] };
  vm.runInContext("renderPortraitJobDetail(job, landscape)", detailContext);
  assert.ok(detail.innerHTML.includes(`输出规格</span><b>${highSize}`));
  delete detailContext.job.output_width;
  delete detailContext.job.output_height;
  vm.runInContext("renderPortraitJobDetail(job, landscape)", detailContext);
  assert.ok(detail.innerHTML.includes(`输出规格</span><b>${lowSize}`), "old task records retain 720p display");
  detailContext.job = { id: "job-running", status: "running", total: 2, completed: 0,
    succeeded: 0, failed: 0, current_file: "clip.mp4", current_progress: .025,
    items: [{ name: "clip.mp4", status: "running", width: 160, height: 90, fps: "25" },
      { name: "cancelled.mp4", status: "cancelled" }] };
  vm.runInContext("renderPortraitJobDetail(job, landscape)", detailContext);
  assert.match(detail.innerHTML, /class="slice-job-stats"/);
  assert.match(detail.innerHTML, /取消 <b>1<\/b>/);
  assert.match(detail.innerHTML, /class="slice-encoder-summary"/);
  assert.match(detail.innerHTML, /class="mini-progress"[^]*width:2.5%/);
  assert.match(detail.innerHTML, /id="cancelPortraitJob"[^>]*width:100%/);
  assert.doesNotMatch(detail.innerHTML, /<p>已处理|<p>当前：|<p>输出规格：/);
  detailContext.job.current_phase = "inspecting";
  vm.runInContext("renderPortraitJobDetail(job, landscape)", detailContext);
  assert.match(detail.innerHTML, /正在检查视频…/);
  detailContext.job.status = "completed";
  vm.runInContext("renderPortraitJobDetail(job, landscape)", detailContext);
  assert.match(detail.innerHTML, /class="record-delete-zone"/);
  assert.ok(detail.innerHTML.includes(`删除${title}任务记录`));
  detailContext.job.encoding = { planned_video_encoder: "h264_videotoolbox", actual_video_encoders: ["h264_videotoolbox"], fallback_count: 0 };
  detailContext.job.items[0].actual_video_encoder = "h264_videotoolbox";
  vm.runInContext("renderPortraitJobDetail(job, landscape)", detailContext);
  assert.match(detail.innerHTML, /视频编码<\/span><b>VideoToolbox/);
  detailContext.job.encoding.actual_video_encoders = ["libx264"];
  detailContext.job.encoding.fallback_count = 1;
  detailContext.job.items[0].actual_video_encoder = "libx264";
  detailContext.job.items[0].encoder_fallback_reason = "compression session failed";
  vm.runInContext("renderPortraitJobDetail(job, landscape)", detailContext);
  assert.match(detail.innerHTML, /视频编码<\/span><b>libx264/);
  assert.match(detail.innerHTML, /1 个条目已自动回退/);
  assert.match(detail.innerHTML, /硬编回退原因：compression session failed/);
  const actionHandlers = {};
  const actionRequests = [];
  detailContext.$ = selector => selector === "#jobDetail" ? detail : {
    addEventListener: (_, handler) => { actionHandlers[selector] = handler; },
  };
  detailContext.api = async (path, options) => { actionRequests.push([path, options.method]); };
  detailContext.loadUpscaleJobs = async () => {};
  detailContext.loadJobs = async () => {};
  detailContext.closeDrawer = () => {};
  detailContext.toast = () => {};
  detailContext.job.status = "running";
  vm.runInContext("renderPortraitJobDetail(job, landscape)", detailContext);
  await actionHandlers["#cancelPortraitJob"]({ currentTarget: {} });
  const endpoint = landscape ? "portrait-to-landscape" : "landscape-to-portrait";
  assert.deepEqual(actionRequests.pop(), [`/tools/${endpoint}/job-running/cancel`, "POST"]);
  detailContext.job.status = "completed";
  vm.runInContext("renderPortraitJobDetail(job, landscape)", detailContext);
  await actionHandlers["#deletePortraitJob"]({ currentTarget: {} });
  assert.deepEqual(actionRequests.pop(), [`/tools/${endpoint}/job-running`, "DELETE"]);
  detailContext.job.mode = "cluster";
  detailContext.job.status = "running";
  detailContext.job.total = 2;
  detailContext.job.completed = 0;
  detailContext.job.items = [{ name: "one", status: "running", progress: .5 }, { name: "two", status: "running", progress: .25 }];
  vm.runInContext("renderPortraitJobDetail(job, landscape)", detailContext);
  assert.match(detail.innerHTML, /width:37.5%/);
  assert.match(detail.innerHTML, /class="mini-progress"[^]*width:50%/);
  assert.match(detail.innerHTML, /class="mini-progress"[^]*width:25%/);
  console.log("portrait preflight invalidation, cleanup, submission and task routing passed");
}
main().then(() => main(true)).then(() => main(false, "cluster")).then(() => main(true, "cluster")).catch(error => { console.error(error); process.exitCode = 1; });
