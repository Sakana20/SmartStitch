const assert = require("node:assert/strict");
const {
  choosePointerSnap,
  chooseRulerStep,
  frameFromTimelineX,
} = require("../frontend/timeline-math.js");

assert.equal(frameFromTimelineX(400, 40, 30, 0, 999), 300);
assert.equal(frameFromTimelineX(-20, 40, 30, 0, 999), 0);
assert.equal(frameFromTimelineX(99999, 40, 30, 0, 500), 500);

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

console.log("timeline interaction math ok");
