const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync(require('node:path').join(__dirname, '../frontend/app.js'), 'utf8');
const helper = app.slice(app.indexOf('let clusterStartPromptCleanup ='), app.indexOf('function mountClusterControlTool('));
const listeners = new Map();
let current;
const element = (value = '') => ({ value, handlers: {}, focus() {}, addEventListener(e, fn) { this.handlers[e] = fn; } });
const document = {
  activeElement: { focus() {} },
  addEventListener(e, fn) { listeners.set(e, fn); }, removeEventListener(e) { listeners.delete(e); },
  body: { append(modal) { current = modal; } },
  createElement() {
    const mode = element('now'), input = element(), row = {}, submit = {}, error = {};
    const form = element();
    const cancel = element();
    const fields = { '[name="start_mode"]': mode, '[name="scheduled_at"]': input, '[data-schedule-time]': row, '[type="submit"]': submit, '[data-schedule-error]': error };
    form.querySelector = key => fields[key];
    return { fields, form, cancel, querySelector: () => form, querySelectorAll: () => [cancel], remove() { this.removed = true; } };
  },
};
const context = { document, Date, escapeHtml: value => value };
vm.createContext(context); vm.runInContext(helper, context);
const submit = modal => modal.form.handlers.submit({ preventDefault() {} });
(async () => {
  let result = context.requestClusterStart('集群渲染');
  submit(current);
  assert.equal(await result, null);
  assert(current.removed);
  result = context.requestClusterStart('集群超分');
  const modal = current;
  modal.fields['[name="start_mode"]'].value = 'scheduled';
  modal.fields['[name="start_mode"]'].handlers.change();
  assert.equal(modal.fields['[data-schedule-time]'].hidden, false);
  assert.equal(modal.fields['[name="scheduled_at"]'].required, true);
  modal.fields['[name="scheduled_at"]'].value = '2000-01-01T20:00';
  submit(modal);
  assert.match(modal.fields['[data-schedule-error]'].textContent, /晚于当前时间/);
  assert(!modal.removed);
  modal.fields['[name="scheduled_at"]'].value = '2099-01-01T20:00';
  submit(modal);
  assert.equal(await result, new Date('2099-01-01T20:00').toISOString());
  result = context.requestClusterStart('集群横改竖');
  current.cancel.handlers.click();
  assert.equal(await result, undefined);
  result = context.requestClusterStart('集群竖改横');
  listeners.get('keydown')({ key: 'Escape', preventDefault() {} });
  assert.equal(await result, undefined);
  const old = context.requestClusterStart('old');
  result = context.requestClusterStart('new');
  assert.equal(await old, undefined);
  current.cancel.handlers.click();
  await result;
  assert.equal(listeners.size, 0);
  console.log('Cluster start prompt: immediate, appointment, validation and cancellation passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
