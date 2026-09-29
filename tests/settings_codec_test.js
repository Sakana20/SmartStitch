const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync(require("node:path").join(__dirname, "../frontend/app.js"), "utf8");
const source = app.match(/function mountSettingsTool\(container\) \{[\s\S]*?\n\}\n\nfunction openToolboxTool/)?.[0].replace(/\n\nfunction openToolboxTool$/, "");
assert.ok(source);
function fixture(api) {
  const handlers = {};
  const toggle = { checked: false, disabled: true, addEventListener: (name, handler) => { handlers[name] = handler; } };
  const hint = { textContent: "" };
  const container = { innerHTML: "", querySelector: selector => selector === "#softwareCodecEnabled" ? toggle : hint };
  const mount = vm.runInNewContext(`${source}; mountSettingsTool`, { api, toast: () => {} });
  const dispose = mount(container);
  return { toggle, hint, container, dispose, change: () => handlers.change() };
}
(async () => {
  const calls = [];
  const ui = fixture(async (path, options) => {
    calls.push([path, options]);
    return { software_codec_enabled: options ? JSON.parse(options.body).software_codec_enabled : true };
  });
  await Promise.resolve();
  assert.equal(ui.toggle.checked, true);
  assert.equal(ui.toggle.disabled, false);
  assert.match(ui.container.innerHTML, /软解编解码/);
  assert.doesNotMatch(ui.container.innerHTML, /设置占位|功能待接入/);
  ui.toggle.checked = false;
  await ui.change();
  assert.equal(calls[1][0], "/settings/codecs");
  assert.equal(calls[1][1].method, "PUT");
  assert.equal(JSON.parse(calls[1][1].body).software_codec_enabled, false);
  const failing = fixture(async (_path, options) => {
    if (options) throw new Error("disk full");
    return { software_codec_enabled: true };
  });
  await Promise.resolve();
  failing.toggle.checked = false;
  await failing.change();
  assert.equal(failing.toggle.checked, true);
  assert.equal(failing.toggle.disabled, false);
  let resolve;
  const closed = fixture(() => new Promise(done => { resolve = done; }));
  closed.dispose();
  resolve({ software_codec_enabled: true });
  await Promise.resolve();
  assert.equal(closed.toggle.disabled, true);
  assert.equal(closed.toggle.checked, false);
  console.log("codec settings persistence and failure recovery ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
