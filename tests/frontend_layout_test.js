const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "frontend/index.html"), "utf8");
const css = fs.readFileSync(path.join(root, "frontend/styles.css"), "utf8");
const app = fs.readFileSync(path.join(root, "frontend/app.js"), "utf8");

assert.match(
  html,
  /href="\/styles\.css\?v=20260916-6"/,
  "新增片段列表样式后必须刷新 CSS 缓存版本",
);
const staticVersions = [...html.matchAll(/(?:styles\.css|timeline-math\.js|app\.js)\?v=([^"]+)/g)]
  .map(match => match[1]);
assert.equal(staticVersions.length, 3, "三个前端静态资源都必须声明缓存版本");
assert.equal(new Set(staticVersions).size, 1, "CSS 与 JS 必须使用同一个发布版本，避免新旧资源混用");
assert.match(
  html,
  /id="timelineVideoStage" class="video-stage"/,
  "播放窗口必须提供稳定的悬停区域",
);
assert.match(
  html,
  /id="timelineMediaGrid" class="timeline-media-grid"/,
  "视频和审核面板需要可根据方向切换布局",
);
assert.match(
  app,
  /videoStage\.addEventListener\("pointerenter"[\s\S]*pointerOverVideo = true;[\s\S]*videoStage\.addEventListener\("pointerleave"[\s\S]*pointerOverVideo = false;/,
  "播放窗口必须跟踪鼠标进入和离开",
);

assert.match(
  html,
  /<div id="timelineSegmentList" class="segment-list"><\/div>\s*<div class="timeline-list-footer">/,
  "片段列表说明区必须放在独立 footer 中",
);
assert.match(
  html,
  /id="mergeSegmentsBtn"[^>]*disabled>合并所选（0）<\/button>/,
  "片段列表必须提供默认禁用的合并所选按钮",
);
assert.match(
  html,
  /id="clearMergeSelectionBtn"[^>]*disabled>取消选择<\/button>/,
  "片段列表必须提供取消合并选择操作",
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
assert.match(
  css,
  /\.timeline-segments \.timeline-segment\.composite-member::after\s*\{[^}]*content:\s*attr\(data-group-label\)/s,
  "时间线组合成员必须显示共同组号",
);
assert.match(
  css,
  /\.timeline-media-grid\.portrait\s*\{[^}]*width:\s*100%;[^}]*grid-template-columns:\s*minmax\(420px, \.95fr\) minmax\(480px, 1\.05fr\);[^}]*max-width:\s*none;[^}]*margin:\s*24px 0 0;/s,
  "竖屏播放区与审核面板必须铺满整行并对齐上下区域",
);
assert.match(
  app,
  /updateTimelineMediaLayout\(analysis\.width, analysis\.height\);/,
  "分析完成后必须立即应用视频方向布局",
);

console.log("frontend timeline layout contract ok");
