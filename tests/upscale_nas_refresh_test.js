const RenderEta = require("../frontend/render-eta.js");
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync(require('node:path').join(__dirname, '../frontend/app.js'), 'utf8');
const source = app.slice(app.indexOf('function mountVideoUpscaleTool('), app.indexOf('\nfunction mountProResAlphaTool'));
const fields = new Map();
const get = id => {
  if (!fields.has(id)) fields.set(id, { value: id === '#upscaleModelSelect' ? 'x2plus' : '', innerHTML: '', textContent: '', handlers: {},
    addEventListener(e, fn) { this.handlers[e] = fn; }, replaceChildren() {}, querySelector() { return null; } });
  return fields.get(id);
};
const timers = new Map();
let timerId = 0;
const nas = [];
const context = { RenderEta, AbortController, normalizePathInput: s => s, escapeHtml: s => s,
  setTimeout: (fn, ms) => { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
  clearTimeout: id => timers.delete(id), setInterval: () => 0, clearInterval() {},
  toast() {}, videoUpscaleJobId: null, loadJobs: async () => {}, switchView: async () => {}, openUpscaleJob() {},
  api: async (path, options = {}) => {
    if (path.endsWith('/nodes')) return [];
    if (path.endsWith('/latest')) return null;
    assert(path.includes('/nas?'));
    return new Promise((resolve, reject) => {
      nas.push({ path, signal: options.signal, resolve });
      options.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
    });
  },
};
vm.createContext(context);
vm.runInContext(source, context);
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  const cleanup = context.mountVideoUpscaleTool({ innerHTML: '', querySelector: get }, 'cluster');
  await tick();
  const button = get('#upscalePreview');
  assert.equal(nas.length, 1);
  assert.equal(button.disabled, true);
  const repeated = button.handlers.click({ currentTarget: button });
  assert.equal(nas.length, 1, 'manual refresh must join initial scan');
  [...timers.values()].find(timer => timer.ms === 60000).fn();
  await repeated;
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, '刷新 NAS 视频');
  assert.match(get('#upscaleNas').textContent, /超过 60 秒/);
  assert.equal(get('#upscaleStart').disabled, true);

  const retry = button.handlers.click({ currentTarget: button });
  assert.equal(nas.length, 2);
  get('#upscaleModelSelect').value = 'animevideo';
  get('#upscaleModelSelect').handlers.change();
  await tick();
  assert(nas[1].signal.aborted, 'model change must cancel old scan');
  assert.equal(nas.length, 3);
  assert.equal(button.disabled, true, 'old cleanup must not reenable newer scan');
  nas[2].resolve({ model: 'animevideo', pending_count: 1, skipped_count: 0, invalid_count: 0, items: [{ name: 'clip.mp4', status: 'pending' }] });
  await retry;
  await tick();
  assert.equal(button.disabled, false);
  assert.match(get('#upscaleNas').innerHTML, /NAS 集群批次/);
  assert.match(get('#upscalePreviewResult').innerHTML, /待处理 · 加入队列后预检/);
  assert.doesNotMatch(get('#upscalePreviewResult').innerHTML, /undefined|fps|帧/);
  const final = button.handlers.click({ currentTarget: button });
  cleanup();
  await final;
  assert(nas[3].signal.aborted, 'closing panel must abort preview');
  assert.equal(timers.size, 0);
  console.log('NAS refresh deduplication, timeout recovery, model changes and cleanup passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
