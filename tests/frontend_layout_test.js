const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "frontend/index.html"), "utf8");
const css = fs.readFileSync(path.join(root, "frontend/styles.css"), "utf8");

assert.match(
  html,
  /<div id="timelineSegmentList" class="segment-list"><\/div>\s*<div class="timeline-list-footer">/,
  "片段列表说明区必须放在独立 footer 中",
);
assert.match(
  css,
  /\.segment-list\s*\{[^}]*min-height:\s*210px;[^}]*max-height:\s*330px;/s,
  "片段列表必须保留稳定的最小高度",
);
assert.match(
  css,
  /\.segment-list\s*>\s*\.breakpoint-empty\s*\{[^}]*min-height:\s*210px;/s,
  "空状态必须与片段列表使用相同最小高度",
);
assert.match(
  css,
  /\.timeline-list-footer\s*\{[^}]*margin-top:\s*18px;[^}]*padding:\s*14px 2px 2px;[^}]*border-top:/s,
  "说明区必须与片段列表保持独立间距和分隔线",
);

console.log("frontend timeline layout contract ok");
