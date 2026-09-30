const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const timers = new Map();
let timerId = 0;
const context = { document: { addEventListener() {} }, crypto: webcrypto,
  setTimeout: callback => { timers.set(++timerId, callback); return timerId; },
  clearTimeout: id => timers.delete(id), structuredClone,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/app.js', 'utf8') + `
globalThis.testNaming = { state, ensureOutputNaming, renderBuilderEditor, bindBuilderControls, mountFolderConcatTool, renderFolderConcatNamingPreview, builderTokens };
`, context);
const api = context.testNaming;
assert.deepEqual(Array.from(api.builderTokens('商品|!|开头-甲.mp4')), ['商品', '开头-甲']);
assert.deepEqual(Array.from(api.builderTokens('达人_乙|!|结尾__001_pool-2_f0-50.mp4')), ['达人_乙', '结尾']);
api.state.namingPicker = { category: 'pool_9', sample: '项目素材.mp4' };
api.state.scan = { assets: { pool_1: [{ name: '项目文件.mp4' }] } };
const local = {
  configDraft: { timeline: ['pool_1', 'pool_2'], sources: {
    pool_1: { mode: 'required', label: 'A 文件夹' },
    pool_2: { mode: 'required', label: 'B 文件夹' },
  }, output: {} },
  scan: { assets: { pool_1: [{ name: '商品-开头.mp4' }], pool_2: [{ name: '达人-结尾.mp4' }] } },
  namingPicker: null,
};
const naming = api.ensureOutputNaming(local.configDraft);
naming.enabled = naming.builder.enabled = true;
let html = api.renderBuilderEditor(local.configDraft, local);
assert.ok(html.includes('商品-开头.mp4'));
assert.ok(!html.includes('项目文件.mp4'));
local.namingPicker.category = 'pool_2';
local.namingPicker.sample = '';
html = api.renderBuilderEditor(local.configDraft, local);
assert.ok(html.includes('达人-结尾.mp4'));
assert.equal(api.state.namingPicker.category, 'pool_9');
const elements = new Map();
function element(selector, value = '') {
  const item = { value, textContent: '', handlers: {},
    addEventListener(type, callback) { this.handlers[type] = callback; },
  };
  elements.set(selector, item);
  return item;
}
const text = element('#builderNewText', '拼接成片');
const addText = element('#builderAddText');
const addSequence = element('#builderAddSequence');
const sequenceStart = element('#builderSequenceStart');
const preview = element('#builderPreview');
const result = element('#builderPreviewResult');
let renders = 0, invalidations = 0, previews = 0;
api.bindBuilderControls({ context: local,
  root: { querySelector: selector => elements.get(selector), querySelectorAll: () => [] },
  rerender: () => renders++, onChange: () => invalidations++, preview: async () => previews++,
});
addText.handlers.click();
assert.equal(naming.builder.blocks[0].text, '拼接成片');
addSequence.handlers.click();
addSequence.handlers.click();
assert.equal(naming.builder.blocks.filter(block => block.type === 'sequence').length, 1);
sequenceStart.handlers.change({ target: { value: '12' } });
assert.equal(naming.sequence_start, 12);
assert.equal(renders, 2);
assert.equal(invalidations, 3);
assert.ok(result.textContent.includes('重新验证'));
preview.handlers.click({ currentTarget: preview }).then(() => {
  assert.equal(previews, 1);
  assert.equal(api.state.namingPicker.category, 'pool_9');
  console.log('folder concat naming editor isolation and controls ok');
});


(async () => {
  const controls = new Map();
  function control(id) {
    const item = { value: '', checked: false, textContent: '', innerHTML: '', handlers: {},
      addEventListener(type, callback) { this.handlers[type] = callback; },
      replaceChildren() { this.innerHTML = ''; },
      querySelector() { return null; }, querySelectorAll() { return []; },
    };
    controls.set(`#${id}`, item);
    return item;
  }
  for (const id of ['folderConcatA', 'folderConcatB', 'folderConcatOutput',
    'folderConcatResult', 'folderConcatNamingStatus', 'folderConcatNamingEditor',
    'folderConcatNamingEnabled', 'folderConcatPreview', 'folderConcatStart']) control(id);
  const container = { innerHTML: '', querySelector: selector => controls.get(selector), querySelectorAll: () => [] };
  const requests = [];
  context.mockApi = async (endpoint, options) => {
    requests.push({ endpoint, payload: JSON.parse(options.body) });
    return { count_a: 2, count_b: 1,
      files_a: ['当前商品-开头.mp4', '未配对商品.mp4'], files_b: ['当前达人-结尾.mp4'],
      pairs: [{ a_name: '当前商品-开头.mp4', b_name: '当前达人-结尾.mp4' }],
    };
  };
  vm.runInContext('api = globalThis.mockApi;', context);
  const cleanup = api.mountFolderConcatTool(container);
  assert.ok(!container.innerHTML.includes('folderConcatImportNaming'));
  controls.get('#folderConcatA').value = '/tmp/当前 A';
  controls.get('#folderConcatA').handlers.input();
  assert.equal(timers.size, 0);
  controls.get('#folderConcatB').value = '/tmp/当前 B';
  controls.get('#folderConcatB').handlers.input();
  assert.equal(timers.size, 1);
  for (const callback of timers.values()) await callback();
  timers.clear();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].payload.directory_a, '/tmp/当前 A');
  assert.equal(requests[0].payload.directory_b, '/tmp/当前 B');
  assert.equal(requests[0].payload.naming, null);
  controls.get('#folderConcatNamingEnabled').handlers.change({ target: { checked: true } });
  const markup = controls.get('#folderConcatNamingEditor').innerHTML;
  assert.ok(markup.includes('当前商品-开头.mp4'));
  assert.ok(markup.includes('未配对商品.mp4'));
  assert.ok(!markup.includes('项目文件.mp4'));
  assert.ok(controls.get('#folderConcatNamingStatus').textContent.includes('已读取 A 2 个、B 1 个'));
  // Changing only the output location keeps the scanned source names available.
  controls.get('#folderConcatOutput').handlers.input();
  assert.equal(timers.size, 0);
  controls.get('#folderConcatB').value = '/tmp/另一个 B';
  controls.get('#folderConcatB').handlers.input();
  assert.equal(timers.size, 1);
  cleanup();
  assert.equal(timers.size, 0);
  console.log('folder concat automatically parses current folders independently ok');
})().catch(error => { console.error(error); process.exitCode = 1; });


const singlePreview = api.renderFolderConcatNamingPreview({ pairs: [
  { output_name: '示例成片1.mp4', a_name: '开头甲.mp4', b_name: '结尾甲.mp4',
    naming: { block_values: [{ value: '示例成片' }, { value: '1' }] } },
  { output_name: '示例成片2.mp4', a_name: '开头乙.mp4', b_name: '结尾乙.mp4',
    naming: { block_values: [{ value: '示例成片' }, { value: '2' }] } },
] });
assert.ok(singlePreview.includes('<strong>示例成片1.mp4</strong>'));
assert.ok(singlePreview.includes('块取值：示例成片 | 1'));
assert.ok(singlePreview.includes('A 文件夹：开头甲.mp4；B 文件夹：结尾甲.mp4'));
assert.ok(!singlePreview.includes('示例成片2.mp4'));
assert.ok(!singlePreview.includes('开头乙.mp4'));
console.log('folder concat filename validation shows one example with block values and sources ok');
