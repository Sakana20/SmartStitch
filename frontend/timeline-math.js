(function exposeTimelineMath(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.TimelineMath = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createTimelineMath() {
  function frameFromTimelineX(timelineX, pixelsPerSecond, fps, minimum, maximum) {
    const frame = Math.round(Math.max(0, timelineX) / pixelsPerSecond * fps);
    return Math.max(minimum, Math.min(maximum, frame));
  }

  function ranked(matches) {
    return matches.sort((left, right) => (
      left.distance - right.distance
      || right.priority - left.priority
      || left.frame - right.frame
    ));
  }

  function choosePointerSnap({
    rawFrame,
    pointerTimelineX,
    pixelsPerSecond,
    fps,
    candidates,
    lockedFrame = null,
    enabled = false,
    enterPixels = 8,
    exitPixels = 12,
  }) {
    if (!enabled) return { frame: rawFrame, snapTargetFrame: null };
    const withDistance = candidate => ({
      ...candidate,
      distance: Math.abs(pointerTimelineX - candidate.frame / fps * pixelsPerSecond),
    });
    const locked = candidates.find(candidate => candidate.frame === lockedFrame);
    if (locked && withDistance(locked).distance <= exitPixels) {
      return { frame: locked.frame, snapTargetFrame: locked.frame };
    }
    const matches = ranked(candidates.map(withDistance).filter(candidate => (
      candidate.distance <= enterPixels
    )));
    return matches.length
      ? { frame: matches[0].frame, snapTargetFrame: matches[0].frame }
      : { frame: rawFrame, snapTargetFrame: null };
  }

  function chooseRulerStep(pixelsPerSecond, minimumMajorPixels = 80, minimumMinorPixels = 8) {
    const candidates = [
      1 / 60, 1 / 30, 1 / 15, 0.1, 0.2, 0.5,
      1, 2, 5, 10, 15, 30, 60, 120, 300, 600,
    ];
    const majorSeconds = candidates.find(value => value * pixelsPerSecond >= minimumMajorPixels)
      ?? candidates[candidates.length - 1];
    const minorDivisors = [10, 5, 2, 1];
    const divisor = minorDivisors.find(value => (
      majorSeconds / value * pixelsPerSecond >= minimumMinorPixels
    )) ?? 1;
    return { majorSeconds, minorSeconds: majorSeconds / divisor };
  }

  return { choosePointerSnap, chooseRulerStep, frameFromTimelineX };
}));
