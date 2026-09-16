const assert = require("node:assert/strict");
const {
  choosePointerSnap,
  chooseRulerStep,
  frameFromPlaybackTime,
  frameFromPresentedTime,
  frameFromTimelineX,
  mediaLayoutOrientation,
  mergeSliceUnits,
  previewTimeForFrame,
  shouldTogglePlaybackFromSpace,
} = require("../frontend/timeline-math.js");

assert.equal(frameFromTimelineX(400, 40, 30, 0, 999), 300);
assert.equal(frameFromTimelineX(-20, 40, 30, 0, 999), 0);
assert.equal(frameFromTimelineX(99999, 40, 30, 0, 500), 500);

assert.equal(frameFromPlaybackTime(0.99, 30, 0, 59), 29);
assert.equal(frameFromPlaybackTime(0.966666, 30, 0, 59), 29);
assert.equal(frameFromPlaybackTime(1, 30, 0, 59), 30);
assert.equal(frameFromPlaybackTime(999, 30, 0, 59), 59);
assert.equal(frameFromPresentedTime(0.999999, 30, 0, 59), 30);
assert.equal(previewTimeForFrame(30, 30, 2), 30.5 / 30);
assert.equal(previewTimeForFrame(1487, 60, 62.016667), 1487.5 / 60);
assert.equal(previewTimeForFrame(59, 30, 1.98), 1.98 - 1e-6);

const candidates = [
  { frame: 299, priority: 2 },
  { frame: 301, priority: 3 },
];
assert.deepEqual(
  choosePointerSnap({
    rawFrame: 300,
    pointerTimelineX: 300,
    pixelsPerSecond: 40,
    fps: 40,
    candidates,
  }),
  { frame: 300, snapTargetFrame: null },
);
assert.deepEqual(
  choosePointerSnap({
    rawFrame: 300,
    pointerTimelineX: 300,
    pixelsPerSecond: 40,
    fps: 40,
    candidates,
    enabled: true,
  }),
  { frame: 301, snapTargetFrame: 301 },
);
assert.deepEqual(
  choosePointerSnap({
    rawFrame: 304,
    pointerTimelineX: 304,
    pixelsPerSecond: 40,
    fps: 40,
    candidates,
    lockedFrame: 301,
    enabled: true,
  }),
  { frame: 301, snapTargetFrame: 301 },
);
assert.deepEqual(
  choosePointerSnap({
    rawFrame: 304,
    pointerTimelineX: 304,
    pixelsPerSecond: 40,
    fps: 40,
    candidates,
    lockedFrame: 301,
    enabled: false,
  }),
  { frame: 304, snapTargetFrame: null },
);

assert.deepEqual(chooseRulerStep(10), { majorSeconds: 10, minorSeconds: 1 });
assert.deepEqual(chooseRulerStep(120), { majorSeconds: 1, minorSeconds: 0.1 });

const merged = mergeSliceUnits({
  units: [
    { id: "one", segmentIds: ["late"], category: "benefit_1" },
    { id: "middle", segmentIds: ["middle"], category: "hook" },
    { id: "three", segmentIds: ["early"], category: "benefit_1" },
  ],
  selectedUnitIds: ["one", "three"],
  segmentStartFrames: { early: 0, middle: 25, late: 50 },
  mergedUnitId: "merged",
});
assert.deepEqual(merged.units, [
  { id: "merged", segmentIds: ["early", "late"], category: "benefit_1" },
  { id: "middle", segmentIds: ["middle"], category: "hook" },
]);
assert.equal(merged.mergedUnit.category, "benefit_1");
assert.equal(mergeSliceUnits({
  units: [
    { id: "one", segmentIds: ["early"], category: "hook" },
    { id: "two", segmentIds: ["late"], category: "ending" },
  ],
  selectedUnitIds: ["one", "two"],
  segmentStartFrames: { early: 0, late: 50 },
  mergedUnitId: "mixed",
}).mergedUnit.category, "");

assert.equal(shouldTogglePlaybackFromSpace({
  code: "Space",
  pointerOverVideo: true,
  hasAnalysis: true,
}), true);
assert.equal(shouldTogglePlaybackFromSpace({
  code: "Space",
  pointerOverTimeline: true,
  hasAnalysis: true,
}), true);
assert.equal(shouldTogglePlaybackFromSpace({
  code: "Space",
  pointerOverVideo: true,
  hasAnalysis: true,
  isEditing: true,
}), false);
assert.equal(shouldTogglePlaybackFromSpace({
  code: "Enter",
  pointerOverVideo: true,
  hasAnalysis: true,
}), false);
assert.equal(shouldTogglePlaybackFromSpace({
  code: "Space",
  pointerOverVideo: true,
  hasAnalysis: false,
}), false);

assert.equal(mediaLayoutOrientation(720, 1280), "portrait");
assert.equal(mediaLayoutOrientation(1080, 1920), "portrait");
assert.equal(mediaLayoutOrientation(1920, 1080), "landscape");
assert.equal(mediaLayoutOrientation(1080, 1080), "landscape");
assert.equal(mediaLayoutOrientation(0, 0), "landscape");

console.log("timeline interaction math ok");
