const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync(require('node:path').join(__dirname, '../frontend/app.js'), 'utf8');
const mount = app.slice(app.indexOf('function mountBatchDedupTool(container)'), app.indexOf('\nfunction renderAssetEditStatus'));
const cards = app.slice(app.indexOf('function visualEffectLibrary('), app.indexOf('\nfunction renderSimpleConfig()'));
const tick = () => new Promise(resolve => setImmediate(resolve));

function setup() {
  const document = { activeElement: null };
  let elements = [];
  let html = '';
  const container = {
    scrollTop: 20,
    get innerHTML() { return html; },
    set innerHTML(value) {
      html = value;
      elements = [...value.matchAll(/<(input|button|select)\b([^>]*)>/g)].map(match => {
        const attributes = Object.fromEntries([...match[2].matchAll(/([\w-]+)="([^"]*)"/g)].map(item => [item[1], item[2]]));
        return {
          attributes, value: attributes.value || '', disabled: /\sdisabled(?:\s|$)/.test(match[2]), handlers: {},
          selectionStart: null, selectionEnd: null,
          dataset: Object.fromEntries(Object.entries(attributes).filter(([key]) => key.startsWith('data-'))
            .map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), value])),
          addEventListener(event, fn) { this.handlers[event] = fn; },
          getAttribute(name) { return this.attributes[name]; },
          hasAttribute(name) { return name in this.attributes; },
          focus() { document.activeElement = this; },
          setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
        };
      });
    },
    contains(element) { return elements.includes(element); },
    querySelectorAll(selector) {
      return elements.filter(element => selector.split(',').some(part => {
        part = part.trim();
        if (part.startsWith('#')) return element.attributes.id === part.slice(1);
        const match = part.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
        return match && element.hasAttribute(match[1]) && (match[2] === undefined || element.getAttribute(match[1]) === match[2]);
      }));
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0]; },
  };
  const requests = new Map();
  const state = { visualEffectLibraries: null };
  const context = { state, document, URL, escapeHtml: value => String(value ?? ''), ensureVisualDedup: config => config.visual_dedup,
    normalizePathInput: value => value, toast() {}, loadJobs: async () => {}, openJob: async () => {},
    api: path => new Promise((resolve, reject) => {
      if (!requests.has(path)) requests.set(path, []);
      requests.get(path).push({ resolve, reject });
    }),
  };
  vm.createContext(context);
  vm.runInContext(`${cards}\n${mount}`, context);
  const dispose = context.mountBatchDedupTool(container);
  const latest = path => requests.get(path)?.at(-1);
  return { context, state, container, document, dispose, latest, requests };
}

async function loadSettings(test) {
  test.latest('/tools/batch-dedup/settings').resolve({ enabled: true, effect_layers: [
    { layer_id: 'effect-effect_2', library_id: 'effect_2', name: '烟花', type: 'overlay', enabled: true, opacity_percent: 100 },
  ] });
  test.latest('/tools/batch-dedup/sync-settings').resolve({ enabled: false, base_url: '', table_id: '' });
  test.latest('/integrations/feishu/settings').resolve({ app_id: '', app_secret_configured: false });
  test.latest('/tools/batch-dedup/directory-settings').resolve({
    source_directory: '/Volumes/home/Smartstitch/批量去重/原素材',
    output_directory: '/Volumes/home/Smartstitch/批量去重/已处理',
  });
  await tick();
}
const libraries = { revision: 2, libraries: [{ library_id: 'effect_2', name: '烟花', enabled: true, directory: '/effects/effect_2', assets: [{ enabled: true }] }] };

(async () => {
  const test = setup();
  await loadSettings(test);
  const { container, latest, document } = test;
  assert.match(container.innerHTML, /源视频文件夹/);
  assert.equal(container.querySelector('#batchDedupDirectory').value, '/Volumes/home/Smartstitch/批量去重/原素材');
  assert.equal(container.querySelector('#batchDedupOutputDirectory').value, '/Volumes/home/Smartstitch/批量去重/已处理');
  assert.match(container.innerHTML, /后台加载/);
  assert.doesNotMatch(container.innerHTML, /素材库不可用|库不可用/);
  assert(latest('/global-assets/visual-effect-libraries'), 'library scan starts after settings render');
  assert.equal(container.querySelector('#batchDedupStart').disabled, true);
  assert.equal(container.querySelector('[data-effect-library-name]').disabled, true);
  assert.equal(container.querySelector('[data-effect-action="delete"]').disabled, true);
  const directory = container.querySelector('#batchDedupDirectory');
  directory.value = '/new-videos';
  directory.handlers.input();
  const output = container.querySelector('#batchDedupOutputDirectory');
  output.value = '/chosen-output';
  output.handlers.input({ target: output });
  const opacity = container.querySelector('[data-effect-opacity]');
  opacity.value = '42';
  opacity.handlers.change();
  const focused = container.querySelector('#batchDedupDirectory');
  focused.focus();
  focused.selectionStart = 4;
  focused.selectionEnd = 4;
  latest('/global-assets/visual-effect-libraries').resolve(libraries);
  await tick();
  assert.match(container.innerHTML, /1 个素材可参与去重/);
  assert.equal(container.querySelector('#batchDedupDirectory').value, '/new-videos');
  assert.equal(container.querySelector('#batchDedupOutputDirectory').value, '/chosen-output');
  assert.equal(container.querySelector('[data-effect-opacity]').value, '42');
  assert.equal(document.activeElement, container.querySelector('#batchDedupDirectory'));
  assert.equal(document.activeElement.selectionStart, 4);
  assert.equal(container.scrollTop, 20);
  assert.equal(container.querySelector('#batchDedupStart').disabled, false);

  const chooseButton = container.querySelector('#batchDedupChooseOutput');
  let choosing = chooseButton.handlers.click({ currentTarget: chooseButton });
  assert.equal(chooseButton.disabled, true);
  latest('/system/directory-picker').resolve({ cancelled: true });
  await choosing;
  assert.equal(container.querySelector('#batchDedupOutputDirectory').value, '/chosen-output');
  choosing = chooseButton.handlers.click({ currentTarget: chooseButton });
  latest('/system/directory-picker').resolve({ cancelled: false, path: '/picked-output' });
  await choosing;
  assert.equal(container.querySelector('#batchDedupOutputDirectory').value, '/picked-output');
  assert.equal(chooseButton.disabled, false);

  const refresh = container.querySelector('#batchDedupRefreshLibraries').handlers.click();
  assert.match(container.innerHTML, /后台加载/);
  assert.equal(container.querySelector('#batchDedupStart').disabled, true);
  latest('/global-assets/visual-effect-libraries').reject(new Error('NAS unavailable'));
  await refresh;
  assert.match(container.innerHTML, /素材库加载失败：NAS unavailable/);
  assert.equal(container.querySelector('#batchDedupRefreshLibraries').disabled, false);
  assert.equal(container.querySelector('#batchDedupStart').disabled, true);
  const retry = container.querySelector('#batchDedupRefreshLibraries').handlers.click();
  latest('/global-assets/visual-effect-libraries').resolve({ ...libraries, libraries: [{ ...libraries.libraries[0], assets: [{ enabled: true }, { enabled: true }] }] });
  await retry;
  assert.match(container.innerHTML, /2 个素材可参与去重/);
  assert.equal(container.querySelector('#batchDedupStart').disabled, false);

  const stale = setup();
  await loadSettings(stale);
  const before = stale.container.innerHTML;
  stale.dispose();
  stale.latest('/global-assets/visual-effect-libraries').resolve(libraries);
  await tick();
  assert.equal(stale.state.visualEffectLibraries, null);
  assert.equal(stale.container.innerHTML, before, 'closed tool ignores late library responses');
  console.log('Batch dedup background loading, edits, refresh, retry and disposal passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
