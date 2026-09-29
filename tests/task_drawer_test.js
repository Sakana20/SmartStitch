const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname, "../frontend/app.js"), "utf8");
function source(name) {
  const match = app.match(new RegExp(`(?:async )?function ${name}\\([^\\n]*\\) \\{[\\s\\S]*?\\n\\}`));
  assert.ok(match, `${name} must exist`);
  return match[0];
}

async function run() {
  let onJobsPage = false;
  let timerCount = 0;
  const timers = new Set();
  const requests = [];
  const rendered = [];
  const drawer = { classList: { remove() {} }, setAttribute() {} };
  const state = { activeJob: null, upscaleJobTimer: null };
  const context = vm.createContext({
    state,
    terminalStates: new Set(["completed", "partial_failed", "failed", "cancelled", "interrupted"]),
    $: selector => selector === "#jobDrawer" ? drawer : { classList: { contains: () => onJobsPage } },
    setInterval: () => { const id = ++timerCount; timers.add(id); return id; },
    clearInterval: id => timers.delete(id), clearTimeout() {},
    api: async url => { requests.push(url); return { id: "task-1", status: "running" }; },
    renderJobs() {},
    renderUpscaleJobDetail: job => rendered.push([job.id, "upscale"]),
    renderPortraitJobDetail: (job, landscape) => rendered.push([job.id, landscape ? "landscape" : "portrait"]),
    toast: message => { throw new Error(message); },
  });
  vm.runInContext(["syncToolJobPolling", "loadUpscaleJobs", "closeDrawer"].map(source).join("\n"), context);

  for (const [type, endpoint, renderer] of [
    ["video_upscale", "video-upscale", "upscale"],
    ["landscape_to_portrait", "landscape-to-portrait", "portrait"],
    ["portrait_to_landscape", "portrait-to-landscape", "landscape"],
  ]) {
    state.activeJob = { id: "task-1", job_type: type, status: "running" };
    context.syncToolJobPolling();
    context.syncToolJobPolling();
    assert.equal(timers.size, 1, "opening a tool drawer outside task records starts one timer");
    requests.length = 0;
    await context.loadUpscaleJobs();
    assert.deepEqual(requests, [`/tools/${endpoint}/task-1`], "only fetch the open detail outside task records");
    assert.deepEqual(rendered.pop(), ["task-1", renderer]);
    context.closeDrawer();
    assert.equal(timers.size, 0, "closing the drawer stops polling outside task records");
  }

  state.activeJob = { id: "task-1", job_type: "video_upscale", status: "running" };
  context.syncToolJobPolling();
  let resolve;
  context.api = () => new Promise(done => { resolve = done; });
  const pending = context.loadUpscaleJobs();
  context.closeDrawer();
  resolve({ id: "task-1", status: "running" });
  await pending;
  assert.equal(state.activeJob, null, "late responses cannot restore a closed drawer");
  assert.equal(rendered.length, 0);

  state.activeJob = { id: "task-1", job_type: "video_upscale", status: "running" };
  context.syncToolJobPolling();
  context.api = async () => ({ id: "task-1", status: "completed" });
  await context.loadUpscaleJobs();
  assert.equal(timers.size, 0, "completion stops detail polling outside task records");

  onJobsPage = true;
  context.closeDrawer();
  assert.equal(timers.size, 1, "task records still refresh after closing a drawer");
  onJobsPage = false;
  context.syncToolJobPolling();
  assert.equal(timers.size, 0);

  const button = { disabled: false, querySelector: () => ({ textContent: "" }) };
  let opened;
  const startContext = vm.createContext({
    $: selector => selector === "#startBtn" ? button : { value: "2" },
    requestValues: () => ({ count: 1 }),
    api: async () => ({ id: "created-1" }), toast() {}, loadJobs: async () => {},
    switchView: () => { throw new Error("creating a task must retain the current page"); },
    openJob: async id => { opened = id; },
  });
  vm.runInContext(source("startJob"), startContext);
  await startContext.startJob();
  assert.equal(opened, "created-1");
  assert.equal(button.disabled, false);
  console.log("task creation retains its page; drawer polling and cleanup passed");
}
run().catch(error => { console.error(error); process.exitCode = 1; });
