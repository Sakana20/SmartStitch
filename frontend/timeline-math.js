(function exposeTimelineMath(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.TimelineMath = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createTimelineMath() {
  function frameFromTimelineX(timelineX, pixelsPerSecond, fps, minimum, maximum) {
    const frame = Math.round(Math.max(0, timelineX) / pixelsPerSecond * fps);
    return Math.max(minimum, Math.min(maximum, frame));
  }

  function frameFromPlaybackTime(mediaTime, fps, minimum, maximum) {
    const frame = Math.floor(Math.max(0, mediaTime) * fps + 1e-4);
    return Math.max(minimum, Math.min(maximum, frame));
  }

  function frameFromPresentedTime(mediaTime, fps, minimum, maximum) {
    const frame = Math.round(Math.max(0, mediaTime) * fps);
    return Math.max(minimum, Math.min(maximum, frame));
  }

  function previewTimeForFrame(frame, fps, duration = Number.POSITIVE_INFINITY) {
    const midpoint = (Math.max(0, frame) + 0.5) / fps;
    return Math.min(midpoint, Math.max(0, duration - 1e-6));
  }

  function waveformFrameRange({
    scrollLeft,
    viewportWidth,
    pixelsPerSecond,
    fps,
    frameCount,
  }) {
    const maximumStart = Math.max(0, frameCount - 1);
    const startFrame = Math.max(0, Math.min(
      maximumStart,
      Math.floor(Math.max(0, scrollLeft) / pixelsPerSecond * fps),
    ));
    const endFrame = Math.max(startFrame + 1, Math.min(
      frameCount,
      Math.ceil(
        (Math.max(0, scrollLeft) + Math.max(1, viewportWidth))
        / pixelsPerSecond * fps,
      ),
    ));
    return { startFrame, endFrame };
  }

  function waveformViewportGeometry({
    scrollLeft,
    viewportWidth,
    pixelsPerSecond,
    duration,
  }) {
    const timelineWidth = Math.max(1, Math.max(0, duration) * pixelsPerSecond);
    const left = Math.max(0, Math.min(Math.max(0, scrollLeft), timelineWidth - 1));
    const right = Math.max(left + 1, Math.min(
      timelineWidth,
      left + Math.max(1, viewportWidth),
    ));
    return { left, width: right - left };
  }

  function isAudioOnlyBreakpoint(point) {
    const reasons = Array.isArray(point?.reasons) ? point.reasons : [];
    return reasons.length > 0 && reasons.every(reason => (
      reason === "speech_pause" || reason === "silence_end"
    ));
  }

  function activeTimelineBreakpoints(breakpoints, audioLocked = false) {
    return audioLocked
      ? breakpoints.filter(point => !isAudioOnlyBreakpoint(point))
      : [...breakpoints];
  }

  function breakpointFramesInPixelRange({
    breakpoints,
    startX,
    endX,
    pixelsPerSecond,
    fps,
    markerHalfWidth = 8,
  }) {
    const left = Math.min(startX, endX);
    const right = Math.max(startX, endX);
    const frames = breakpoints
      .map(point => Number(point?.frame_index))
      .filter(Number.isFinite)
      .filter(frame => {
        const x = frame / fps * pixelsPerSecond;
        return x + markerHalfWidth >= left && x - markerHalfWidth <= right;
      });
    return [...new Set(frames)].sort((first, second) => first - second);
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

  function mergeSliceUnits({ units, selectedUnitIds, segmentStartFrames, mergedUnitId }) {
    const selectedIds = new Set(selectedUnitIds);
    const selectedUnits = units.filter(unit => selectedIds.has(unit.id));
    if (selectedUnits.length < 2) throw new Error("至少需要两个输出单元");
    const segmentIds = [...new Set(selectedUnits.flatMap(unit => unit.segmentIds))]
      .sort((left, right) => (
        (segmentStartFrames[left] ?? 0) - (segmentStartFrames[right] ?? 0)
      ));
    const categories = new Set(selectedUnits.map(unit => unit.category));
    const category = categories.size === 1 && !categories.has("")
      ? selectedUnits[0].category
      : "";
    const mergedUnit = { id: mergedUnitId, segmentIds, category };
    const mergedUnits = [];
    let inserted = false;
    units.forEach(unit => {
      if (!selectedIds.has(unit.id)) {
        mergedUnits.push(unit);
      } else if (!inserted) {
        mergedUnits.push(mergedUnit);
        inserted = true;
      }
    });
    return { units: mergedUnits, mergedUnit };
  }

  function shouldTogglePlaybackFromSpace({
    code,
    hasAnalysis = false,
    isEditing = false,
  }) {
    return code === "Space"
      && hasAnalysis
      && !isEditing;
  }

  function stepFrameFromPlayhead(playheadFrame, delta, frameCount) {
    const maximum = Math.max(0, Math.round(frameCount) - 1);
    const current = Math.max(0, Math.min(maximum, Math.round(playheadFrame)));
    return Math.max(0, Math.min(maximum, current + Math.trunc(delta)));
  }

  function shouldRemoveSelectedSegment(currentSegmentId, targetSegmentId, toggleMembership = false) {
    return toggleMembership && currentSegmentId === targetSegmentId;
  }

  function mediaLayoutOrientation(width, height) {
    const normalizedWidth = Number(width);
    const normalizedHeight = Number(height);
    if (!(normalizedWidth > 0) || !(normalizedHeight > 0)) return "landscape";
    return normalizedHeight > normalizedWidth ? "portrait" : "landscape";
  }

  return {
    activeTimelineBreakpoints,
    breakpointFramesInPixelRange,
    choosePointerSnap,
    chooseRulerStep,
    frameFromPlaybackTime,
    frameFromPresentedTime,
    frameFromTimelineX,
    isAudioOnlyBreakpoint,
    mediaLayoutOrientation,
    mergeSliceUnits,
    previewTimeForFrame,
    shouldRemoveSelectedSegment,
    shouldTogglePlaybackFromSpace,
    stepFrameFromPlayhead,
    waveformFrameRange,
    waveformViewportGeometry,
  };
}));
