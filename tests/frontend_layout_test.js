const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "frontend/index.html"), "utf8");
const css = fs.readFileSync(path.join(root, "frontend/styles.css"), "utf8");
const app = fs.readFileSync(path.join(root, "frontend/app.js"), "utf8");

assert.match(
  html,
  /href="\/styles\.css\?v=20260917-29"/,
  "源视频切换样式更新后必须刷新 CSS 缓存版本",
);
const staticVersions = [...html.matchAll(/(?:styles\.css|timeline-math\.js|app\.js)\?v=([^"]+)/g)]
  .map(match => match[1]);
assert.equal(staticVersions.length, 3, "三个前端静态资源都必须声明缓存版本");
assert.equal(new Set(staticVersions).size, 1, "CSS 与 JS 必须使用同一个发布版本，避免新旧资源混用");
assert.match(
  html,
  /data-config-mode="simple">简单模式[\s\S]*data-config-mode="advanced">高级模式[\s\S]*data-config-mode="yaml">YAML 专家模式/,
  "配置管理必须提供简单、高级和 YAML 三级模式",
);
assert.match(
  html,
  /id="simpleConfigEditor"[\s\S]*id="visualConfigEditor"[^>]*hidden/,
  "简单模式必须默认显示，原可视化配置应作为隐藏的高级模式",
);
assert.match(
  app,
  /function renderSimpleConfig\(\)[\s\S]*simple-half-card[\s\S]*simple-half-card[\s\S]*simple-wide-card[\s\S]*simple-wide-card[\s\S]*组合重复规则/,
  "简单模式必须使用大区块展示核心配置逻辑",
);
assert.match(
  app,
  /function currentStructuredDraft\(\)[\s\S]*state\.configMode === "advanced"[\s\S]*collectVisualConfig\(\)[\s\S]*structuredClone\(state\.configDraft\)/,
  "简单模式保存时必须保留未展示的高级配置字段",
);
assert.match(
  app,
  /function restoreConfigUiPreferences\(\)\s*\{[\s\S]*applyAdvancedSectionPreferences\(preferences\);[\s\S]*setConfigMode\("simple"\);/,
  "每次打开配置管理必须默认进入简单模式",
);
assert.match(
  app,
  /simpleOverlayFile[\s\S]*uploadOverlayImage[\s\S]*\/overlay-image/,
  "简单模式必须支持直接更换风险提示语图片",
);
assert.doesNotMatch(
  app,
  /copyOverlayDirectoryBtn|复制图片文件夹位置/,
  "简单模式不应显示复制风险提示语图片文件夹入口",
);
assert.match(
  css,
  /\.simple-half-card \.simple-overlay-row\s*\{[^}]*align-items:\s*start/,
  "风险提示语图片选择按钮必须与左侧状态框顶部对齐",
);
assert.match(
  css,
  /\.simple-half-card \.simple-overlay-actions label\.button\s*\{[^}]*flex:\s*1/,
  "风险提示语图片选择按钮必须拉伸到与左侧状态框等高",
);
assert.match(
  app,
  /simpleChoiceButtons[\s\S]*data-simple-choice[\s\S]*生成速度与画质[\s\S]*组合重复规则/,
  "简单模式应使用大选项呈现常用成片设置",
);
assert.doesNotMatch(
  app,
  /simpleConfigEnabled/,
  "简单模式不应展示意义不明确的配置启用开关",
);
assert.doesNotMatch(
  app,
  /data-simple-source-mode|simpleSourceModeOptions/,
  "简单模式的视频库不应继续展示使用规则下拉框",
);
assert.match(
  app,
  /data-simple-source-enabled=.*data-active-mode=.*group\.mode !== "disabled"/,
  "简单模式必须用开关控制视频库是否参与拼接",
);
assert.match(
  app,
  /data-open-source-directory[\s\S]*\/sources\/\$\{category\}\/open-directory/,
  "简单模式必须显示可点击的视频库文件夹名称",
);
assert.doesNotMatch(
  app,
  /configSourceModeSwitch|data-config-type="source-mode"/,
  "高级模式不应被简单模式的二态开关取代",
);
assert.match(
  app,
  /configSelect\("使用方式", `sources\.\$\{category\}\.mode`, group\.mode, modeChoices\)/,
  "高级模式必须保留必需、可选和停用的详细设置",
);
assert.match(
  css,
  /\.simple-config-editor\s*\{[^}]*grid-template-columns:\s*repeat\(2,[^}]*grid-auto-rows:\s*max-content/,
  "桌面端项目信息与风险提示语应各占一半，卡片必须按内容撑高",
);
assert.match(
  html,
  /id="newWorkflowType"[\s\S]*value="taobao_flash"[\s\S]*value="generic"/,
  "新建项目库时必须允许选择淘宝闪购或通用模式",
);
assert.match(
  app,
  /function libraryTargetPath[\s\S]*parentName\.toLocaleLowerCase\(\) === folder\.toLocaleLowerCase\(\)[\s\S]*不再创建同名子目录/,
  "保存位置与项目文件夹同名时不得预览为双层目录",
);
assert.match(
  app,
  /data-pool-card[\s\S]*dragstart[\s\S]*dragover[\s\S]*drop/,
  "通用视频库卡片必须支持拖拽调整拼接顺序",
);
assert.match(
  app,
  /\/configs\/\$\{state\.configId\}\/pools[\s\S]*current_config_hash/,
  "通用视频库新增操作必须携带配置版本哈希",
);
assert.match(
  html,
  /<span>源视频文件夹[\s\S]*id="chooseTimelineDirectoryBtn"[\s\S]*id="previousVideoBtn"[\s\S]*id="timelineCurrentSource"[\s\S]*id="nextVideoBtn"/,
  "时间线审核台必须支持选择源视频文件夹和切换上一个、下一个视频",
);
assert.match(
  html,
  /id="timelineCurrentSourceButton"[^>]*aria-haspopup="listbox"[\s\S]*id="timelineSourceSearchInput"[^>]*type="search"[\s\S]*id="timelineSourceList"[^>]*role="listbox"/,
  "点击当前视频后必须提供可搜索、可滚动的视频选择列表",
);
assert.match(
  app,
  /api\("\/timeline\/sources"[\s\S]*source_directory: sourceDirectory/,
  "前端必须从源视频文件夹载入可切换的视频清单",
);
assert.match(
  app,
  /previousVideoBtn[\s\S]*navigateTimelineSource\(-1\)[\s\S]*nextVideoBtn[\s\S]*navigateTimelineSource\(1\)/,
  "上一个和下一个视频按钮必须触发相邻视频切换",
);
assert.match(
  app,
  /timelineCurrentSourceButton[\s\S]*toggleTimelineSourcePicker[\s\S]*function filteredTimelineSourceIndexes\(\)[\s\S]*function renderTimelineSourcePickerList\(\)/,
  "当前视频区域必须能打开并筛选完整视频列表",
);
assert.match(
  css,
  /\.timeline-source-list\s*\{[^}]*max-height:\s*330px;[^}]*overflow-y:\s*auto;/s,
  "大量源视频必须在固定高度的列表中滚动选择",
);
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
