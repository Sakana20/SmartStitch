const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

async function run() {
  const context = vm.createContext({ document: { addEventListener() {} }, crypto: webcrypto,
    structuredClone, setTimeout: () => 1, clearTimeout() {},
  });
  vm.runInContext(fs.readFileSync('frontend/app.js', 'utf8') + '\nglobalThis.mount = mountFolderConcatTool;', context);
  const controls = new Map();
  for (const id of ['A', 'B', 'Output', 'Result', 'NamingStatus', 'NamingEditor', 'NamingEnabled', 'Preview', 'Start',
    'OverlayEnabled', 'OverlayBody', 'OverlayFile', 'OverlayPick', 'OverlayStatus', 'OverlayScale',
    'OverlaySource', 'OverlayExisting', 'OverlayLocal', 'OverlayConfig', 'OverlayRefresh', 'OverlayConfigStatus',
    'OverlayOpacity', 'OverlayScope', 'OverlayRange', 'OverlayStart', 'OverlayEnd']) {
    const classes = new Set(id === 'OverlayBody' || id === 'OverlayRange' ? ['hidden'] : []);
    controls.set(`#folderConcat${id}`, { value: '', checked: false, innerHTML: '', textContent: '', disabled: false, handlers: {},
      addEventListener(type, callback) { this.handlers[type] = callback; },
      replaceChildren() { this.innerHTML = ''; }, querySelector() { return null; }, querySelectorAll() { return []; },
      classList: { toggle(name, force) { if (force) classes.add(name); else classes.delete(name); }, contains: name => classes.has(name) },
    });
  }
  const item = id => controls.get(`#folderConcat${id}`);
  for (const [id, value] of Object.entries({ A: '/A', B: '/B', OverlayScale: 'original', OverlaySource: 'config',
    OverlayOpacity: '100', OverlayScope: 'full', OverlayStart: '0' })) item(id).value = value;
  const container = { innerHTML: '', querySelector: selector => controls.get(selector), querySelectorAll: () => [] };
  const requests = [], errors = [], opened = [];
  let picker = { cancelled: true, path: null }, defer = false, finish;
  const files = { pair_count: 1, count_a: 1, count_b: 1, unpaired_a: 0, unpaired_b: 0, files_a: ['A.mp4'], files_b: ['B.mp4'],
    pairs: [{ a_name: 'A.mp4', b_name: 'B.mp4', output_name: 'A_拼接.mp4', overlay_range: { start_seconds: 0, end_seconds: 2 } }] };
  context.mockApi = async (endpoint, options) => {
    const payload = options?.body ? JSON.parse(options.body) : null;
    requests.push({ endpoint, payload });
    if (endpoint === '/system/image-file-picker') return picker;
    if (endpoint === '/tools/folder-concat/overlay-images') return { items: [
      { config_id: 'existing-risk', config_name: '<现有配置>', name: '提示.png', available: true },
      { config_id: 'empty', config_name: '无图片配置', available: false },
    ] };
    if (endpoint === '/tools/folder-concat') return { id: 'new-job', count: 1 };
    if (payload.benefit_overlays) {
      const result = { ...files, benefit_overlays: { ...payload.benefit_overlays, enabled: true, name: '<风险>.png',
        width: 100, height: 30, size_bytes: 512, format: 'PNG' }, warnings: ['<警告>'] };
      if (defer) return new Promise(resolve => { finish = () => resolve(result); });
      return result;
    }
    return { ...files, benefit_overlays: { enabled: false } };
  };
  context.mockToast = text => errors.push(text);
  context.mockOpen = id => opened.push(id);
  vm.runInContext('api = globalThis.mockApi; toast = globalThis.mockToast; loadJobs = async () => {}; openJob = globalThis.mockOpen;', context);
  const cleanup = context.mount(container);
  const click = id => item(id).handlers.click({ currentTarget: item(id) });
  const change = (id, value) => { item(id).value = value; item(id).handlers.change({ target: item(id) }); };
  const enable = checked => { item('OverlayEnabled').checked = checked; return item('OverlayEnabled').handlers.change({ target: item('OverlayEnabled') }); };
  assert.ok(!container.innerHTML.includes('folderConcatOverlayPosition'), 'position control is removed');
  assert.ok(container.innerHTML.includes('仅 A 文件夹') && container.innerHTML.includes('仅 B 文件夹'));
  await click('Start');
  assert.equal(requests.at(-1).payload.benefit_overlays, null, 'default submits plain concatenation');
  await enable(true);
  assert.ok(!item('OverlayBody').classList.contains('hidden'));
  assert.equal(item('OverlayConfig').value, 'existing-risk');
  assert.ok(item('OverlayConfig').innerHTML.includes('&lt;现有配置&gt;'));
  change('OverlayScope', 'b');
  await click('Start');
  assert.equal(requests.at(-1).payload.benefit_overlays.config_id, 'existing-risk');
  assert.equal(requests.at(-1).payload.benefit_overlays.file, '');
  assert.equal(requests.at(-1).payload.benefit_overlays.timing.scope, 'b');
  assert.equal(requests.at(-1).payload.benefit_overlays.timing.start_seconds, 0);
  change('OverlaySource', 'local');
  assert.ok(item('OverlayExisting').classList.contains('hidden'));
  assert.ok(!item('OverlayLocal').classList.contains('hidden'));
  item('OverlayFile').value = '/risk.png';
  await click('OverlayPick');
  assert.equal(item('OverlayFile').value, '/risk.png', 'cancel keeps the current picture');
  picker = { cancelled: false, path: '/picked.png' };
  await click('OverlayPick');
  assert.equal(item('OverlayFile').value, '/picked.png');
  item('OverlayOpacity').value = '50';
  change('OverlayScope', 'custom');
  item('OverlayStart').value = '0.2'; item('OverlayEnd').value = '';
  assert.ok(!item('OverlayRange').classList.contains('hidden'));
  requests.length = 0;
  await click('Preview');
  assert.equal(requests[0].payload.benefit_overlays, null, 'naming samples stay independent of picture validation');
  const settings = requests[1].payload.benefit_overlays;
  assert.equal(settings.placement.x, '(W-w)/2');
  assert.equal(settings.placement.y, '(H-h)/2');
  assert.equal(settings.config_id, null);
  assert.equal(settings.placement.opacity, .5);
  assert.equal(settings.timing.start_seconds, .2);
  assert.equal(settings.timing.end_seconds, null);
  assert.ok(item('OverlayStatus').textContent.includes('100×30'));
  assert.ok(item('Result').innerHTML.includes('&lt;风险&gt;'));
  assert.ok(item('Result').innerHTML.includes('&lt;警告&gt;'));
  item('OverlayOpacity').value = '0'; item('OverlayOpacity').handlers.input();
  assert.equal(item('Result').innerHTML, '', 'changing overlay settings invalidates the preview');
  change('OverlayScale', 'stretch');
  await click('Start');
  assert.equal(requests.at(-1).payload.benefit_overlays.placement.x, '0');
  assert.equal(requests.at(-1).payload.benefit_overlays.placement.y, '0');
  assert.deepEqual(opened, ['new-job', 'new-job', 'new-job']);
  enable(false);
  assert.ok(item('OverlayBody').classList.contains('hidden'));
  await click('Start');
  assert.equal(requests.at(-1).payload.benefit_overlays, null);
  enable(true);
  assert.equal(item('OverlayFile').value, '/picked.png', 're-enabling retains local settings');
  defer = true;
  const pending = click('Preview');
  for (let i = 0; i < 5 && !finish; i++) await Promise.resolve();
  assert.ok(finish);
  item('OverlayFile').value = '/new.png'; item('OverlayFile').handlers.input();
  finish(); await pending;
  assert.equal(item('Result').innerHTML, '', 'a stale response cannot restore the old image preview');
  assert.ok(!item('OverlayStatus').textContent.includes('已检查'));
  cleanup();
  assert.deepEqual(errors.filter(message => !message.startsWith('已提交')), []);
  console.log('folder concat optional overlay controls, isolated payloads, preview invalidation and stale responses passed');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
