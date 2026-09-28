const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync(require('node:path').join(__dirname, '../frontend/app.js'), 'utf8');
const source = app.slice(app.indexOf('async function selectConfig(id)'), app.indexOf('\nfunction updateGenerateAvailability()'));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const tick = () => new Promise(resolve => setImmediate(resolve));
const fields = new Map();
const field = id => {
  if (!fields.has(id)) fields.set(id, { value: '', textContent: '', innerHTML: '', replaceChildren() { this.innerHTML = ''; } });
  return fields.get(id);
};
const requests = new Map();
const request = path => {
  const pending = deferred();
  if (!requests.has(path)) requests.set(path, []);
  requests.get(path).push(pending);
  return pending.promise;
};
const latest = path => requests.get(path)?.at(-1);
const state = { configId: null, configLoadRevision: 0, scanRevision: 0, configs: [], timeline: { sourceVideos: [], analysis: null } };
const context = { state, $: field, api: request, escapeHtml: s => s, resetTimelineHistory() {}, resetPreview() {}, renderTimeline() {}, updateGenerateAvailability() {}, renderAssetTabs() {}, renderAssets() {}, toast() {} };
vm.createContext(context);
vm.runInContext(source, context);
const config = id => ({ config: { id, name: id, batch: { default_count: 10, concurrency: 1 } }, content_hash: id, yaml_text: id });
const scan = id => ({ config_id: id, ok: true, assets: {}, errors: [] });

(async () => {
  // Startup finishes after config arrives even while NAS inspection stays pending.
  const selectedA = context.selectConfig('a');
  latest('/configs/a').resolve(config('a'));
  await selectedA;
  assert.equal(field('#heroConfigName').textContent, 'a');
  assert(latest('/libraries/by-config/a'));
  assert.equal(state.scan, null);
  assert.match(field('#heroAssetCount').textContent, /后台加载/);

  // A slower old library response must not start a scan for the new config.
  const selectedB = context.selectConfig('b');
  latest('/configs/b').resolve(config('b'));
  await selectedB;
  latest('/libraries/by-config/a').resolve({ managed: false });
  await tick();
  assert(!latest('/configs/a/scan'));
  latest('/libraries/by-config/b').resolve({ managed: false });
  await tick();
  assert(latest('/configs/b/source-inventory'));
  assert(latest('/configs/b/scan'));
  // Scan and inventory start despite both global effect requests still pending.
  latest('/configs/b/scan').resolve(scan('b'));
  await tick();
  assert.equal(state.scan.config_id, 'b');

  // Revisit the same config: old successes and failures cannot overwrite it.
  const oldScan = context.scanAssets(false);
  const pendingOldScan = latest('/configs/b/scan');
  const selectedAgain = context.selectConfig('b');
  latest('/configs/b').resolve(config('b'));
  await selectedAgain;
  latest('/libraries/by-config/b').resolve({ managed: false });
  await tick();
  const newInventory = latest('/configs/b/source-inventory');
  requests.get('/configs/b/source-inventory')[0].resolve({ config_id: 'b', stale: true });
  pendingOldScan.reject(new Error('old scan failed'));
  await oldScan;
  assert.equal(state.sourceInventory, null);
  assert.equal(field('#scanBtn').disabled, true);
  assert.equal(field('#scanSummary').textContent, '正在用 ffprobe 检查素材，请稍候…');
  latest('/configs/b/scan').resolve(scan('b'));
  newInventory.resolve({ config_id: 'b', fresh: true });
  for (const path of ['/global-assets/visual-borders', '/global-assets/visual-effect-libraries']) {
    for (const pending of requests.get(path)) pending.resolve({});
  }
  await tick();
  assert.equal(state.scan.config_id, 'b');
  assert.equal(state.sourceInventory.fresh, true);
  assert.equal(field('#scanBtn').disabled, false);
  console.log('Background config loading, parallel requests and stale-response protection passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
