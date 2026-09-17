const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const app = fs.readFileSync(path.join(root, "frontend/app.js"), "utf8");
const context = { document: { addEventListener() {} } };
vm.createContext(context);
vm.runInContext(`${app}
globalThis.__normalizePathInput = normalizePathInput;
globalThis.__libraryTargetPath = libraryTargetPath;
globalThis.__timelineState = state.timeline;
globalThis.__filteredTimelineSourceIndexes = filteredTimelineSourceIndexes;`, context);

const normalize = context.__normalizePathInput;
const libraryTarget = context.__libraryTargetPath;
const pathname = "/Volumes/Elements SE/陈鼎琦/原始视频/26 室友要喝我的奶茶 #剧情演绎.mp4";

assert.equal(normalize(`'${pathname}'`), pathname);
assert.equal(normalize(`\"${pathname}\"`), pathname);
assert.equal(normalize(`“${pathname}”`), pathname);
assert.equal(normalize(pathname), pathname);
assert.equal(normalize("  '/tmp/有 空格.mp4'\n"), "/tmp/有 空格.mp4");
assert.equal(normalize("/tmp/文件'名.mp4"), "/tmp/文件'名.mp4");
assert.equal(
  libraryTarget("/Volumes/home/红果短剧一口价二剪", "红果短剧一口价二剪"),
  "/Volumes/home/红果短剧一口价二剪",
);
assert.equal(
  libraryTarget("/Volumes/home", "红果短剧一口价二剪"),
  "/Volumes/home/红果短剧一口价二剪",
);
assert.equal(
  libraryTarget("C:\\Video\\商品库", "商品库"),
  "C:\\Video\\商品库",
);
assert.equal(libraryTarget("", ""), "");

context.__timelineState.sourceVideos = [
  { name: "第1集-开场.mp4", path: "/tmp/1.mp4" },
  { name: "第2集-厨房.mp4", path: "/tmp/2.mp4" },
  { name: "第20集-开场.mp4", path: "/tmp/20.mp4" },
];
context.__timelineState.sourceFilter = "开场";
assert.deepEqual(Array.from(context.__filteredTimelineSourceIndexes()), [0, 2]);
context.__timelineState.sourceFilter = "厨房";
assert.deepEqual(Array.from(context.__filteredTimelineSourceIndexes()), [1]);
context.__timelineState.sourceFilter = "没有匹配";
assert.deepEqual(Array.from(context.__filteredTimelineSourceIndexes()), []);

console.log("path input normalization ok");
