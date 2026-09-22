const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "frontend/index.html"), "utf8");
const css = fs.readFileSync(path.join(root, "frontend/styles.css"), "utf8");
const app = fs.readFileSync(path.join(root, "frontend/app.js"), "utf8");
const openConfigSource = app.match(
  /async function openConfig\(\) \{([\s\S]*?)\n\}\nfunction configEditorIsDirty/,
)?.[1] || "";
const selectTimelineSegmentSource = app.match(
  /function selectTimelineSegment\([^\n]+\) \{([\s\S]*?)\n\}\n\nfunction renderMergeToolbar/,
)?.[1] || "";

assert.match(
  html,
  /href="\/styles\.css\?v=20260922-85"/,
  "ffprobe 扫描配置更新后必须刷新静态资源缓存版本",
);
const staticVersions = [...html.matchAll(/(?:styles\.css|timeline-math\.js|app\.js)\?v=([^"]+)/g)]
  .map(match => match[1]);
assert.equal(staticVersions.length, 3, "三个前端静态资源都必须声明缓存版本");
assert.equal(new Set(staticVersions).size, 1, "CSS 与 JS 必须使用同一个发布版本，避免新旧资源混用");
assert.match(
  html,
  /class="asset-scope-notice"[\s\S]*随机候选池[\s\S]*仅列出已开启视频库的素材[\s\S]*配置管理 → 拼接顺序/,
  "素材权重页必须说明它是生成候选池，并指引用户查看关闭视频库的素材数量",
);
assert.match(
  css,
  /\.asset-scope-notice\s*\{[^}]*background:\s*#edf4ef[^}]*color:\s*var\(--muted\)/,
  "素材权重说明必须沿用现有薄荷绿提示卡样式",
);
assert.doesNotMatch(
  html,
  /assetSelectionBar|assetBatchWeightActions|batchAssetWeightInput/,
  "素材权重页不得在表格上方保留重复的批量权重操作栏",
);
assert.match(
  app,
  /function assetSortHeader[\s\S]*data-asset-sort[\s\S]*function renderAssetTableHead[\s\S]*selectAllAssets/,
  "素材表头必须支持升降序，并支持全选当前素材库",
);
assert.match(
  app,
  /modified_at[\s\S]*文件日期[\s\S]*formatAssetDate/,
  "素材权重表必须展示并允许按文件日期排序",
);
assert.match(
  app,
  /assetLastSelectedIndex[\s\S]*event\.shiftKey[\s\S]*displayedAssets\.slice/,
  "素材选择必须支持 Shift 连续选择",
);
assert.match(
  app + css,
  /class="asset-enable-button[^\n]*data-enabled=[\s\S]*\.asset-enable-button\.enabled[\s\S]*\.asset-enable-button\.disabled/,
  "素材启用状态必须使用明确的状态按钮，避免与素材选择框混淆",
);
assert.doesNotMatch(
  app,
  /<input class="check asset-enabled"/,
  "素材启用状态不得继续使用复选框",
);
assert.match(
  css,
  /\.asset-enable-button\.disabled\s*\{[^}]*color:\s*#9c3d2a[^}]*background:\s*#fce4dd/,
  "停用状态按钮必须使用现有主题的红色警示样式",
);
assert.match(
  app + css,
  /function bindAssetMarquee[\s\S]*pointerdown[\s\S]*pointermove[\s\S]*getBoundingClientRect[\s\S]*asset-selection-marquee[\s\S]*background:\s*rgba\(63,145,104,\.18\)/,
  "素材表格必须支持 Windows 风格的浅绿色拖拽框选",
);
assert.match(
  app,
  /const originalSelection = new Set\(selected\)[\s\S]*selectionMode = originalSelection\.has\(startRow\.dataset\.id\) \? "remove" : "add"[\s\S]*selectionMode === "remove"[\s\S]*selected\.delete\(row\.dataset\.id\)[\s\S]*else selected\.add\(row\.dataset\.id\)/,
  "框选必须根据起点进入追加或取消模式，未命中素材保持原选择状态",
);
assert.match(
  app,
  /const weightHelp = configHelp\("权重说明"[\s\S]*权重不是固定拼接次数[\s\S]*权重为 0[\s\S]*6、3、1[\s\S]*asset-weight-heading/,
  "权重表头必须提供包含相对比例、零权重和分配示例的悬停说明",
);
assert.match(
  css,
  /\.asset-weight-heading \.field-help-popover\s*\{[^}]*left:\s*auto[^}]*right:\s*-10px[^}]*width:\s*clamp\(260px, 32vw, 420px\)[^}]*max-width:\s*calc\(100vw - 40px\)[^}]*white-space:\s*normal[^}]*overflow-wrap:\s*anywhere/,
  "权重帮助提示必须向左展开、按视口缩放并允许换行，避免超出表格边框",
);
assert.match(
  app,
  /\.asset-weight"\)\.forEach\(input => input\.addEventListener\("keydown"[\s\S]*event\.key !== "Enter"[\s\S]*selected\.has\(asset\.id\)[\s\S]*assets\.filter\(item => selected\.has\(item\.id\)\)[\s\S]*item\.weight = weight/,
  "在已选行的权重框输入并按回车必须批量更新全部选中素材",
);
assert.doesNotMatch(
  html + app,
  /applyBatchAssetWeightBtn|应用到选中/,
  "批量权重不得保留与最终保存语义冲突的应用按钮",
);
assert.match(
  app,
  /selected\.has\(asset\.id\)[\s\S]*assets\.filter\(item => selected\.has\(item\.id\)\)[\s\S]*targets\.forEach\(item => \{ item\.enabled = nextEnabled; \}\)/,
  "点击已选素材的状态按钮必须批量切换全部选中素材",
);
assert.match(
  html,
  /id="assetEditStatus"[^>]*>进入页面后获取编辑权[\s\S]*id="saveWeightsBtn"[^>]*>保存权重/,
  "素材权重页必须恢复唯一的手动保存按钮并显示独占编辑状态",
);
assert.match(
  app,
  /async function openAssetWeightEditor[\s\S]*acquireConfigLease\(state\.configId\)[\s\S]*installConfigLease\(state\.configId, acquired, "assets"\)[\s\S]*async function closeAssetWeightEditor[\s\S]*releaseConfigLease/,
  "进入素材权重页必须获取并持有与配置编辑器相同的独占锁，离开时释放",
);
assert.match(
  app,
  /async function saveWeights\(\)[\s\S]*state\.configLease\?\.context !== "assets"[\s\S]*headers: configLeaseHeaders\(\)[\s\S]*state\.assetEditSnapshot = assetWeightSnapshot\(\)/,
  "素材权重只能使用当前页面持有的独占锁手动保存",
);
assert.doesNotMatch(
  app,
  /scheduleAssetAutoSave|flushAssetAutoSave|assetSavePromise|assetSaveQueued/,
  "素材权重不得继续自动保存",
);
assert.match(
  css,
  /html\s*\{[^}]*scrollbar-gutter:\s*stable/,
  "页面必须预留稳定滚动条槽位，避免异步内容加载时固定队列横向跳动",
);
assert.match(
  css,
  /select\s*\{[^}]*-webkit-appearance:\s*none[^}]*appearance:\s*none[^}]*background-image:[^}]*background-position:\s*right 14px center/,
  "下拉框必须禁用 WKWebView 原生双箭头并使用统一的自定义箭头",
);
assert.match(
  css,
  /input\[type="number"\]::-webkit-inner-spin-button[^}]*-webkit-appearance:\s*none/,
  "数字输入框必须隐藏与单位文字冲突的 WKWebView 原生步进器",
);
assert.match(
  css,
  /\.number-wrap i\s*\{[^}]*top:\s*50%[^}]*transform:\s*translateY\(-50%\)[^}]*pointer-events:\s*none/,
  "数字输入框单位必须垂直居中且不拦截输入操作",
);
assert.match(
  css,
  /\.segment-type-select\s*\{[^}]*min-height:\s*34px[^}]*background-color:\s*#fff[^}]*background-position:\s*right 10px center/,
  "片段类型下拉框必须保留统一箭头并使用紧凑对齐",
);
assert.match(
  html,
  /data-config-mode="simple">简单模式[\s\S]*data-config-mode="advanced">高级模式[\s\S]*data-config-mode="yaml">YAML 专家模式/,
  "配置管理必须提供简单、高级和 YAML 三级模式",
);
assert.match(
  html,
  /id="userProfileBtn"[\s\S]*id="configLeaseBanner"[\s\S]*id="userProfileModal"/,
  "页面必须提供本机用户资料和配置编辑租约状态",
);
assert.match(
  html,
  /<span>姓名<\/span><input id="userDisplayNameInput"/,
  "登录资料弹窗必须使用姓名作为字段名称",
);
assert.doesNotMatch(
  html,
  /id="userProfileDevice"|用于配置协作锁<\/small>/,
  "顶部用户名按钮不应显示设备或协作锁副文案",
);
assert.match(
  css,
  /\.system-pill, \.user-profile-pill\s*\{[^}]*min-height:\s*40px[^}]*font-size:\s*12px/,
  "用户名按钮必须与 FFmpeg 状态卡片使用一致的高度和字号",
);
assert.doesNotMatch(
  css,
  /\.modal-backdrop\s*\{[^}]*backdrop-filter|\.drawer-backdrop, \.modal-backdrop\s*\{[^}]*backdrop-filter/,
  "全屏弹窗遮罩不得启用持续 GPU 合成的背景模糊",
);
assert.match(
  css,
  /\.config-modal-open \.noise\s*\{[^}]*display:\s*none[^}]*\}[\s\S]*\.config-modal-open \.orbit\s*\{[^}]*animation-play-state:\s*paused[\s\S]*\.config-modal-open \.panel[^}]*backdrop-filter:\s*none/,
  "配置弹窗打开时必须停止底层动态纹理、动画与次级模糊合成",
);
assert.match(
  css,
  /\.config-modal-card\s*\{[^}]*transform:\s*none;[^}]*transition:\s*opacity/,
  "大尺寸配置卡片不得使用整层 transform 动画",
);
assert.match(
  app,
  /lock\/acquire[\s\S]*lock\/takeover[\s\S]*setInterval\(renewConfigLease, 15000\)[\s\S]*lock\/release/,
  "配置编辑器必须获取、续租、接管并释放 NAS 编辑锁",
);
assert.match(
  app,
  /function scheduleConfigLeaseExpiry\(lease\)[\s\S]*setTimeout\(\(\) => expireConfigLease\(lease\)[\s\S]*function expireConfigLease[\s\S]*closeConfig\(\{ skipConfirm: true, releaseLease: false \}\)[\s\S]*配置编辑已超时过期，配置未保存/,
  "配置租约到期后必须自动关闭编辑页并提示配置未保存",
);
assert.match(
  app,
  /async function saveConfig\(\)[\s\S]*isTerminalConfigLeaseError\(error\)[\s\S]*expireConfigLease\(\)/,
  "保存时发现租约已失效也必须立即关闭配置编辑页",
);
assert.match(
  app,
  /X-SmartStitch-Lease[\s\S]*X-SmartStitch-Config-Hash/,
  "配置写入必须同时提交租约令牌和配置版本",
);
assert.ok(openConfigSource, "必须能定位配置弹窗打开逻辑");
assert.doesNotMatch(
  openConfigSource,
  /renderSimpleConfig\(\)|renderVisualConfig\(\)/,
  "打开配置时只应通过当前模式懒渲染，不得同时构建隐藏的高级编辑器",
);
assert.match(
  openConfigSource,
  /restoreConfigUiPreferences\(\)[\s\S]*classList\.add\("config-modal-open"\)/,
  "配置弹窗打开时必须启用底层静态模式",
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
  /id="refreshSimpleAssetsBtn"[\s\S]*刷新素材库[\s\S]*function bindSimpleConfigControls\(\)[\s\S]*refreshSimpleAssetsBtn[\s\S]*loadSourceInventory\(true\)[\s\S]*await scanAssets\(false\)[\s\S]*renderSimpleConfig\(\)/,
  "拼接顺序标题栏必须提供刷新素材库按钮，并在扫描后更新各视频库数量",
);
assert.match(
  app,
  /sourceInventory[\s\S]*source-inventory[\s\S]*disabled && count[\s\S]*未参与拼接/,
  "关闭的视频库必须使用独立概览数量，并明确标记为未参与拼接",
);
assert.match(
  app,
  /function currentStructuredDraft\(\)[\s\S]*state\.configMode === "advanced"[\s\S]*collectVisualConfig\(\)[\s\S]*structuredClone\(state\.configDraft\)/,
  "简单模式保存时必须保留未展示的高级配置字段",
);
assert.match(
  app,
  /function restoreConfigUiPreferences\(\)\s*\{\s*setConfigMode\("simple"\);\s*\}/,
  "每次打开配置管理必须默认进入简单模式，不提前扫描隐藏的高级编辑器",
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
  css,
  /\.timeline-slice-queue > header small\s*\{[^}]*display:\s*none[^}]*\}[\s\S]*\.timeline-slice-queue:hover > header small[^}]*\{[^}]*display:\s*block/,
  "收起的切片队列不得为隐藏的副说明预留空间",
);
assert.match(
  css,
  /\.slice-queue-list\s*\{[^}]*display:\s*none[^}]*\}[\s\S]*\.timeline-slice-queue:hover \.slice-queue-list[^}]*\{[^}]*display:\s*grid/,
  "收起的切片队列只能展示任务摘要，展开后才显示完整任务列表",
);
assert.match(
  app,
  /simpleChoiceButtons[\s\S]*data-simple-choice[\s\S]*生成速度与画质[\s\S]*组合重复规则/,
  "简单模式应使用大选项呈现常用成片设置",
);
assert.match(
  app,
  /function renderSimpleConfig\(\)[\s\S]*视觉去重[\s\S]*simpleChoiceButtons\("visualDedup"[\s\S]*simpleVisualBorderFile/,
  "视觉去重必须沿用简单模式卡片、大选项和文件选择结构",
);
assert.match(
  app,
  /simpleVisualDedupEnabled[\s\S]*visual\.enabled[\s\S]*simpleVisualBackgroundEnabled[\s\S]*visual\.background\.enabled[\s\S]*simpleVisualBorderEnabled[\s\S]*visualBorderEnabled/,
  "视觉去重必须包含总开关以及两个独立的子功能开关",
);
assert.match(
  app,
  /configSwitch\("启用视觉去重", "visual_dedup\.enabled"[\s\S]*configSwitch\("启用模糊背景与缩小主画面", "visual_dedup\.background\.enabled"[\s\S]*configSelect\("边框使用方式", "visual_dedup\.border_overlay\.mode"/,
  "高级模式必须分别保留总开关、模糊背景开关和透明边框模式",
);
assert.match(
  app + css,
  /simple-visual-dedup-body[\s\S]*\.simple-visual-dedup-body\s*\{[^}]*display:\s*grid[^}]*gap:\s*16px/,
  "视觉去重卡片各设置组之间必须保留与其他卡片一致的纵向留白",
);
assert.match(
  app + css,
  /simple-visual-border-actions[\s\S]*\.simple-visual-border-actions\s*\{[^}]*align-self:\s*stretch[^}]*align-items:\s*stretch[^}]*\}[\s\S]*\.simple-visual-border-actions label\.button\s*\{[^}]*min-height:\s*100%/,
  "添加全局边框按钮必须与左侧状态框上下对齐",
);
assert.match(
  app,
  /loadVisualBorderLibrary[\s\S]*\/global-assets\/visual-borders[\s\S]*添加到全局库[\s\S]*visualBorderSelection/,
  "视觉边框必须从全局库读取，并在简单模式支持随机或固定选择",
);
assert.match(
  app,
  /function globalVisualBordersForDraft\(config\)[\s\S]*state\.visualBorderLibrary[\s\S]*config\.output\.width[\s\S]*const draftVisualBorders = globalVisualBordersForDraft\(config\)/,
  "简单模式必须按当前草稿计算全局边框兼容数，不能依赖保存前的扫描结果",
);
assert.match(
  app,
  /data-global-border-weight[\s\S]*data-global-border-toggle[\s\S]*function bindGlobalVisualBorderControls/,
  "高级模式必须复用现有配置界面管理全局边框状态和默认权重",
);
assert.match(
  app,
  /function renderVisualConfig\(\)[\s\S]*data-config-section="visual-dedup"[\s\S]*visual_dedup\.background\.enabled[\s\S]*visual_dedup\.foreground\.scale[\s\S]*visual_dedup\.border_overlay\.alpha_mode/,
  "视觉去重复杂参数必须进入现有高级配置与 data-config-path 架构",
);
assert.match(
  app,
  /\["benefit_overlay", "visual_border"\]\.includes\(state\.assetCategory\)[\s\S]*!\["benefit_overlay", "visual_border"\]\.includes\(category\)/,
  "视觉边框必须沿用固定素材规则，不得进入权重与标签编辑",
);
assert.match(
  app,
  /function ensureVisualDedup\(config\)[\s\S]*config\.visual_dedup[\s\S]*function currentStructuredDraft\(\)/,
  "视觉去重必须复用现有 configDraft，不得创建平行状态架构",
);
assert.doesNotMatch(
  html,
  /iframe|id="visualDedupApp"|id="visual-dedup-app"/,
  "视觉去重不得创建 iframe 或独立前端应用容器",
);
assert.match(
  app,
  /workflow_type === "generic"[\s\S]*id="simpleNamingEnabled"[\s\S]*id="simpleNamingProduct"[\s\S]*id="simpleNamingBenefit"[\s\S]*id="simpleNamingPreview"/,
  "通用项目简单模式必须提供动态命名开关、产品、利益点和文件名预览",
);
assert.match(
  app,
  /sequence_start: 1[\s\S]*template: "\{product\}-\{benefit\}-\{talents\}-\{restriction_date\}-\{sequence\}\.mp4"[\s\S]*sequence: String\(naming\.sequence_start \|\| 1\)/,
  "动态命名预览必须使用配置的序号起点并默认从 1 开始",
);
assert.match(
  app,
  /id="simpleNamingSequenceStart"[^>]*min="1"[^>]*max="999999"[\s\S]*sequence_start = value/,
  "05 命名设置必须允许用户填写成片序号起点",
);
assert.match(
  app,
  /simple-card-number">05<\/span><div><h3>命名设置<\/h3>[\s\S]*\$\{namingCard\}[\s\S]*simple-card-number">\$\{generic \? "06" : "05"\}<\/span><div><h3>输出设置<\/h3>/,
  "通用项目必须将命名设置独立为 05 卡片，并把输出设置顺延为 06",
);
assert.match(
  app,
  /id="simpleFeishuEnabled"[\s\S]*id="simpleFeishuAppId"[\s\S]*id="simpleFeishuAppSecret"[\s\S]*id="simpleFeishuUrl"[\s\S]*id="simpleFeishuTable"[\s\S]*id="simpleTestFeishuBtn"/,
  "简单模式必须提供飞书同步开关、全局凭证、多维表格链接、数据表选择和连接测试",
);
assert.match(
  app,
  /function renderFeishuSyncPanel\(job\)[\s\S]*立即同步 \/ 重试[\s\S]*function loadJobFeishuSync\(job\)/,
  "成片任务详情必须显示飞书同步状态并支持失败后重试",
);
assert.match(
  app,
  /function feishuConnectionErrorMarkup\(\)[\s\S]*console_url[\s\S]*打开飞书权限配置/,
  "飞书权限不足时必须在配置卡片中提供开放平台权限入口",
);
assert.match(
  app,
  /data-config-section="output-naming"[\s\S]*output\.naming\.source_metadata\.pattern[\s\S]*testNamingPatternBtn/,
  "高级模式必须提供素材文件名解析规则和测试入口",
);
assert.match(
  css,
  /\.simple-naming-card-body\s*\{[^}]*display:\s*grid;[^}]*gap:\s*12px/,
  "独立命名卡片的内容区必须沿用简单模式的网格间距",
);
assert.match(
  app,
  /\["landscape", "横屏（未完成）", "1280 × 720", true\][\s\S]*\["square", "方形（未完成）", "1080 × 1080", true\]/,
  "未完成的横屏和方形输出必须明确标注并禁用",
);
assert.match(
  css,
  /\.simple-choice:disabled\s*\{[^}]*cursor:\s*not-allowed;/,
  "禁用的输出方向必须显示为不可点击的灰色状态",
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
  /function switchView\(view\)[\s\S]*view === "timeline"[\s\S]*refreshTimelineConfig\(\)[\s\S]*async function refreshTimelineConfig\(\)[\s\S]*refreshed\.content_hash === state\.configHash[\s\S]*timelineCategoryOptions\(\)/,
  "进入时间线时必须按配置哈希刷新切片类型，避免长期打开的页面遗漏新增视频库",
);
assert.match(
  app,
  /window\.addEventListener\("focus"[\s\S]*timelineView[\s\S]*refreshTimelineConfig\(\)/,
  "时间线页面重新获得焦点时必须检查共享配置更新",
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
assert.doesNotMatch(
  app,
  /pointerOverVideo|pointerOverTimeline/,
  "全局空格快捷键不应再依赖播放窗口或时间线的悬停状态",
);
assert.match(
  app,
  /\["Delete", "Backspace"\]\.includes\(event\.key\) && state\.timeline\.selectedFrames\.length[\s\S]*event\.preventDefault\(\);[\s\S]*deleteSelectedBreakpoints\(\);/,
  "时间线选中一个或多个断点后必须支持用 Del 或退格键删除",
);
assert.match(
  html,
  /id="deleteBreakpointBtn"[^>]*title="删除选中断点（Del \/ Backspace）"/,
  "删除断点按钮必须提示 Del 和退格快捷键",
);
assert.match(
  html,
  /id="timelineMarqueeLane"[^>]*title="横向拖拽框选多个断点"[\s\S]*id="timelineTrack"[\s\S]*id="timelineMarqueeBox"/,
  "V1 上方必须提供独立的断点框选区和选框覆盖层",
);
assert.match(
  css,
  /\.timeline-canvas\s*\{[^}]*height:\s*208px[\s\S]*\.timeline-marquee-lane\s*\{[^}]*height:\s*32px[\s\S]*\.timeline-marquee-box\s*\{[^}]*top:\s*42px;[^}]*bottom:\s*0;/,
  "时间线必须为框选区增加空间，并让选框贯穿下方轨道",
);
assert.match(
  app,
  /timelineMarqueeLane[\s\S]*beginTimelineMarquee[\s\S]*breakpointFramesInPixelRange[\s\S]*selectedFrames/,
  "框选区拖拽必须计算并保存多选断点",
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
assert.match(
  html,
  /id="sliceTimelineBtn"[^>]*>加入切片队列</,
  "时间线页必须提供异步提交入口",
);
assert.match(
  html,
  /<\/main>[\s\S]*id="timelineSliceQueue" class="timeline-slice-queue hidden"[\s\S]*id="timelineSliceQueueList"/,
  "切片队列必须挂载在主内容之外，避免跟随页面滚动",
);
assert.match(
  app,
  /api\("\/timeline\/slice-jobs"[\s\S]*client_request_id: requestId[\s\S]*connectSliceJobEvents/,
  "切片提交必须使用稳定请求 ID 并订阅后台任务进度",
);
assert.match(
  css,
  /\.timeline-slice-queue\s*\{[^}]*position:\s*fixed;[^}]*right:[^}]*bottom:[^}]*max-height:/s,
  "切片队列必须是固定在右下角的小抽屉",
);
assert.match(
  css,
  /\.slice-queue-row\.is-terminal\s*\{[^}]*display:\s*none;[^}]*\}[\s\S]*\.timeline-slice-queue:hover \.slice-queue-row\.is-terminal[^}]*display:\s*grid;/s,
  "收起时必须隐藏终态任务，鼠标移入后再显示",
);
assert.doesNotMatch(
  css,
  /\.timeline-slice-queue:hover[^}]*\{[^}]*width:/s,
  "切片抽屉悬停时必须锁定宽度，只向上展开",
);
assert.match(
  css,
  /\.slice-queue-row\s*\{[^}]*grid-template-columns:\s*minmax\(0,1fr\) auto;[^}]*grid-template-areas:\s*"source source" "progress status";/s,
  "窄抽屉中的任务信息必须改为上下两层排版",
);
assert.match(
  css,
  /\.slice-queue-list\s*\{[^}]*overflow-x:\s*hidden;[^}]*overflow-y:\s*auto;/s,
  "切片队列只能纵向滚动，不能横向截断信息",
);
assert.match(
  css,
  /\.timeline-current-source > i\s*\{[^}]*width:\s*18px;[^}]*height:\s*18px;[^}]*display:\s*inline-grid;[^}]*place-items:\s*center;[^}]*transform-origin:\s*50% 50%;/s,
  "视频选择箭头必须围绕固定图标盒的中心旋转",
);
assert.match(
  css,
  /\.timeline-slice-queue > header > span i\s*\{[^}]*width:\s*18px;[^}]*height:\s*18px;[^}]*display:\s*inline-grid;[^}]*place-items:\s*center;[^}]*transform-origin:\s*50% 50%;/s,
  "切片抽屉箭头必须围绕固定图标盒的中心旋转",
);
assert.match(
  css,
  /\.timeline-current-source > i::before, \.timeline-slice-queue > header > span i::before\s*\{[^}]*clip-path:\s*polygon\(/s,
  "两个展开箭头必须使用几何对称图形，不能继续使用字体字符",
);
assert.doesNotMatch(
  html,
  /[⌄⌃]/,
  "展开箭头中不能残留上下留白不对称的字体字符",
);
assert.match(
  app,
  /const activeJobs = state\.sliceJobs\.filter[\s\S]*const terminalJobs = state\.sliceJobs\.filter[\s\S]*\[\.\.\.activeJobs, \.\.\.terminalJobs\]/,
  "悬浮队列必须优先展示活动任务",
);
assert.match(
  app,
  /#timelineSliceQueue"\)\.classList\.toggle\("hidden", view !== "timeline"\)/,
  "窗口级切片抽屉只能在时间线页面显示",
);
assert.match(
  app,
  /window\.addEventListener\("keydown",[\s\S]*event\.code === "Space"[\s\S]*event\.preventDefault\(\)[\s\S]*toggleTimelinePlayback\(\)[\s\S]*capture: true/,
  "切片页空格快捷键必须以捕获阶段全局处理并阻止页面滚动",
);
assert.match(
  app,
  /function stepTimelineFrame\(delta\)[\s\S]*stepFrameFromPlayhead\([\s\S]*state\.timeline\.playheadFrame[\s\S]*seekTimelineFrame\(next, "frame_step"\)/,
  "逐帧操作必须基于当前播放头并只移动播放头",
);
assert.match(
  app,
  /function selectTimelineSegment\(segmentId, \{ scrollIntoView = false, toggleMembership = false \} = \{\}\)[\s\S]*shouldRemoveSelectedSegment\([\s\S]*removeSliceSegment\(segment\.id\);[\s\S]*return;/,
  "再次点击上方时间轴中的已选片段必须将它从待切片列表移除",
);
assert.doesNotMatch(
  app,
  /selectTimelineSegment\(unit\.segmentIds\[0\], \{ toggleMembership: true \}\)|selectTimelineSegment\(event\.currentTarget\.dataset\.selectMember, \{ toggleMembership: true \}\)/,
  "下方片段列表只能选中，不得通过重复点击删除",
);
assert.ok(selectTimelineSegmentSource, "必须能定位片段选中逻辑");
assert.doesNotMatch(
  selectTimelineSegmentSource,
  /seekTimelineFrame|timelineVideo"\)\.pause|segment_select/,
  "选中片段只能更新选中状态，不得暂停视频或移动播放头",
);
assert.match(
  app,
  /function removeSliceSegment\(segmentId\)[\s\S]*unit\.segmentIds\.length === 1[\s\S]*removeSliceUnit\(unit\.id, \{ recordHistory: false \}\)[\s\S]*unit\.segmentIds = unit\.segmentIds\.filter/,
  "移除组合中的片段时必须保留其他组合成员",
);
assert.match(
  app,
  /isTimelineHistoryShortcut = \(event\.metaKey \|\| event\.ctrlKey\)[\s\S]*event\.key\.toLowerCase\(\) === "z"[\s\S]*if \(event\.shiftKey\) redoTimelineEdit\(\);[\s\S]*else undoTimelineEdit\(\);/,
  "切片页必须支持 Command+Z 撤销和 Command+Shift+Z 重做",
);
assert.match(
  app,
  /function timelineEditSnapshot\(\)[\s\S]*breakpoints:[\s\S]*sliceUnits:[\s\S]*function recordTimelineEdit\(\)[\s\S]*redoStack = \[\][\s\S]*function restoreTimelineEditSnapshot/,
  "撤销历史必须同时覆盖断点和待切片编辑状态",
);
assert.match(
  html,
  /⌘Z 撤销 · ⌘⇧Z 重做/,
  "时间线手势提示必须展示撤销和重做快捷键",
);

console.log("frontend timeline layout contract ok");
