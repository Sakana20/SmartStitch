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
const context = {
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
