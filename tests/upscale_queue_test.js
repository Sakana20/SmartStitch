const RenderEta = require("../frontend/render-eta.js");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname, "../frontend/app.js"), "utf8");
const source = app.match(/function renderJobs\(\) \{[\s\S]*?\n\}\n\nasync function openUpscaleJob/)?.[0];
assert.ok(source, "任务列表应包含超分详情入口");

const list = { innerHTML: "" };
const deleteButton = { disabled: false };
const deleteCount = { textContent: "" };
let opened = null;
let click = null;
const context = { RenderEta,
  state: { jobs: [], sliceJobs: [], upscaleJobs: [{
    id: "upscale-1", mode: "cluster", kind: "batch", model: "animevideo",
    status: "running", created_at: 1790215200, processed_frames: 20,
    total_frames: 40, completed_files: 1, failed_files: 0,
  }] },
  $: selector => ({ "#jobsList": list, "#deleteAllJobsBtn": deleteButton,
    "#deleteAllJobsCount": deleteCount })[selector],
  $$: selector => selector === ".job-row" ? [{ dataset: { jobType: "upscale", jobId: "upscale-1" },
    addEventListener: (_event, callback) => { click = callback; } }] : [],
  statusInfo: status => [status, "running"],
  sliceJobStatusInfo: () => ["", ""],
  escapeHtml: value => String(value ?? ""),
  formatDate: () => "09/24",
  hideDeleteAllJobsConfirm: () => {},
  openUpscaleJob: id => { opened = id; },
  openSliceJob: () => assert.fail("opened slice job"),
  openJob: () => assert.fail("opened render job"),
};
vm.runInNewContext(source.replace(/\n\nasync function openUpscaleJob$/, ""), context);
vm.runInNewContext("renderJobs()", context);
assert.match(list.innerHTML, /data-job-type="upscale"/);
assert.match(list.innerHTML, /集群超分 · animevideo/);
assert.match(list.innerHTML, /20\/40 帧/);
assert.match(list.innerHTML, /width:50%/);
click();
assert.equal(opened, "upscale-1");
context.state.upscaleJobs = [{ ...context.state.upscaleJobs[0], mode: "local" }];
vm.runInNewContext("renderJobs()", context);
assert.match(list.innerHTML, /本机超分批次/);
context.state.upscaleJobs = [{ ...context.state.upscaleJobs[0], mode: "cluster",
  phase: "prechecking", prechecked_files: 2, total_files: 4, total_frames: 0 }];
vm.runInNewContext("renderJobs()", context);
assert.match(list.innerHTML, /视频预检/);
assert.match(list.innerHTML, /2\/4 条已预检/);
assert.match(list.innerHTML, /width:50%/);
const detailSource = app.slice(app.indexOf('function renderUpscaleJobDetail(job)'), app.indexOf('\nasync function', app.indexOf('function renderUpscaleJobDetail(job)')));
const detail = { innerHTML: "" };
const detailContext = { ...context, $: selector => selector === "#jobDetail" ? detail : null };
vm.createContext(detailContext);
vm.runInContext(detailSource, detailContext);
detailContext.job = { ...context.state.upscaleJobs[0], items: [] };
vm.runInContext("renderUpscaleJobDetail(job)", detailContext);
assert.match(detail.innerHTML, /视频预检进度/);
assert.match(detail.innerHTML, /已检查 2\/4 条视频/);
console.log("upscale queue rendering, precheck progress and detail routing ok");

detailContext.job = {
  id: 'upscale-2', kind: 'batch', mode: 'local', status: 'running', phase: 'upscaling',
  model: 'x2plus', scale: 2, source: '/NAS/超分/原素材', output_directory: '/NAS/超分/已处理',
  total_files: 4, completed_files: 1, failed_files: 1, processed_frames: 108, total_frames: 1410,
  scheduled_at: '2026-09-29T09:30:00', items: [
    { name: '长文件名.mp4', status: 'running', output_path: '/NAS/超分/已处理/长文件名_SR2x.mp4',
      width: 720, height: 1280, fps: '30000/1001', frames: 350, processed_frames: 108, frame_rate_mode: 'vfr' },
    { name: '完成.mp4', status: 'completed', frames: 350, processed_frames: 350 },
    { name: '失败.mp4', status: 'failed', error: '素材错误 <diagnostic>' },
    { name: '取消.mp4', status: 'cancelled' },
  ],
};
vm.runInContext('renderUpscaleJobDetail(job)', detailContext);
assert.match(detail.innerHTML, /class="slice-job-stats"/);
assert.match(detail.innerHTML, /文件 <b>4<\/b>/);
assert.match(detail.innerHTML, /成功 <b>1<\/b>/);
assert.match(detail.innerHTML, /失败 <b>1<\/b>/);
assert.match(detail.innerHTML, /取消 <b>1<\/b>/);
assert.match(detail.innerHTML, /class="slice-encoder-summary"/);
assert.match(detail.innerHTML, /源目录：\/NAS\/超分\/原素材/);
assert.match(detail.innerHTML, /输出目录：\/NAS\/超分\/已处理/);
assert.match(detail.innerHTML, /x2plus/);
assert.match(detail.innerHTML, /超分处理/);
assert.match(detail.innerHTML, /已处理 108\/1410 帧/);
assert.match(detail.innerHTML, /已处理 108\/350 帧/);
assert.match(detail.innerHTML, /可变帧率，保留原帧时间戳/);
assert.match(detail.innerHTML, /预约开始/);
assert.match(detail.innerHTML, /<strong>长文件名.mp4<\/strong><small class="item-selections">/);
assert.match(detail.innerHTML, /class="portrait-item-description"/);
assert.match(detail.innerHTML, /id="cancelUpscaleJobBtn"[^>]*width:100%/);
assert.match(detail.innerHTML, /width:100%/);
assert.doesNotMatch(detail.innerHTML, /undefined|NaN/);
detailContext.job.status = 'partial_failed';
detailContext.job.mode = 'cluster';
vm.runInContext('renderUpscaleJobDetail(job)', detailContext);
assert.match(detail.innerHTML, /NAS超分批次/);
assert.match(detail.innerHTML, /渲染集群/);
assert.match(detail.innerHTML, /class="record-delete-zone"><button id="deleteUpscaleJobBtn"/);
assert.doesNotMatch(detail.innerHTML, /id="cancelUpscaleJobBtn"/);
detailContext.job = { id: 'single', status: 'completed', phase: 'completed', model: 'animevideo', scale: 4,
  source: '/NAS/单条.mp4', output_path: '/NAS/单条_SR4x.mp4', width: 1920, height: 1080,
  fps: '30/1', frame_rate_mode: 'vfr', processed_frames: 400, total_frames: 400 };
vm.runInContext('renderUpscaleJobDetail(job)', detailContext);
assert.match(detail.innerHTML, /1920×1080/);
assert.match(detail.innerHTML, /已处理 400\/400 帧/);
assert.match(detail.innerHTML, /成功 <b>1<\/b>/);
assert.match(detail.innerHTML, /输出文件：\/NAS\/单条_SR4x.mp4/);
assert.doesNotMatch(detail.innerHTML, /undefined|NaN/);
console.log('upscale detail cards, retained metadata, file layout and terminal actions ok');
