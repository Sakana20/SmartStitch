const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname, "../frontend/app.js"), "utf8");
const tool = app.match(/function mountAutoVideoSplitTool\(container\)[\s\S]*?\n\}\n\nfunction mountSettingsTool/)[0];
const handler = tool.match(/const validateNaming = async event => \{[\s\S]*?\n  \};/)[0];

async function main() {
  const result = { textContent: "", innerHTML: "" };
  const button = { disabled: false };
  const calls = [];
  let resolveRequest;
  const context = {
    disposed: false, revision: 1,
    values: () => ({ source_file: "/素材/商品|!|混剪.mp4", sequence_start: 21, naming: { enabled: true }, output_directory: "/outputs" }),
    input: id => {
      assert.equal(id, "autoSplitNamingEditor", "命名验证不应操作分段结果和开始按钮");
      return { querySelector: () => result };
    },
    toast: () => assert.fail("不应显示错误"), escapeHtml: text => text,
    api: (url, options) => {
      assert.equal(button.disabled, true);
      calls.push({ url, body: JSON.parse(options.body) });
      return new Promise(resolve => { resolveRequest = resolve; });
    },
    invalidate: () => assert.fail("命名验证不应清空分段预览"),
    detect: () => assert.fail("命名验证不应检测黑屏"),
  };
  const sandbox = vm.createContext(context);
  const validate = vm.runInContext(`${handler}; validateNaming`, sandbox);
  const pending = validate({ currentTarget: button });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/tools/auto-video-split/naming-preview");
  assert.deepEqual(Object.keys(calls[0].body).sort(), ["naming", "sequence_start", "source_file"]);
  resolveRequest({ output_name: "商品21.mp4", source_name: "商品|!|混剪.mp4", block_values: [{ value: "商品" }, { value: "21" }] });
  await pending;
  assert.match(result.innerHTML, /商品21\.mp4/);
  assert.equal(button.disabled, false);

  result.innerHTML = "unchanged";
  const stale = validate({ currentTarget: button });
  sandbox.revision += 1;
  resolveRequest({ output_name: "old.mp4", source_name: "old.mp4", block_values: [] });
  await stale;
  assert.equal(result.innerHTML, "unchanged", "修改规则后不能回填旧的验证结果");
  assert.equal(button.disabled, false);
  console.log("auto video split naming validation is independent of detection");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
