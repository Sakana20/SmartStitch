const assert = require('node:assert/strict');
const eta = require('../frontend/render-eta.js');
const job = { id: 'batch', job_type: 'render', status: 'running', count: 4, progress: 0 };
let key = eta.observe(job, undefined, 1000);
assert.equal(eta.label(key, 1000), '正在估算剩余时间');
job.progress = .25;
eta.observe(job, undefined, 11000);
assert.equal(eta.label(key, 11000), '预计剩余约 30 秒');
assert.equal(eta.label(key, 12000), '预计剩余约 29 秒');
// Rerendering identical snapshots must not restart the countdown.
eta.observe(job, undefined, 12000);
assert.equal(eta.label(key, 13000), '预计剩余约 28 秒');
assert.equal(eta.label(key, 41000), '正在处理，等待进度更新');
assert.equal(eta.label(key, 42000), '进度暂未更新，正在重新估算');
job.progress = .3;
eta.observe(job, undefined, 50000);
assert.equal(eta.label(key, 50000), '正在估算剩余时间');
job.progress = .5;
eta.observe(job, undefined, 60000);
assert.equal(eta.label(key, 60000), '预计剩余约 25 秒');
// Retry with lower progress discards the previous speed.
job.progress = .1;
eta.observe(job, undefined, 61000);
assert.equal(eta.label(key, 61000), '正在估算剩余时间');
job.status = 'cancelling';
eta.observe(job, undefined, 62000);
assert.equal(eta.label(key, 62000), '正在取消');
job.status = 'completed';
assert.equal(eta.observe(job, undefined, 63000), null);
assert.equal(eta.label(key, 64000), '');
assert.equal(eta.html(job), '');

const scheduled = { id: 'scheduled', status: 'scheduled', scheduled_at: '2026-09-29T00:01:00Z' };
key = eta.observe(scheduled, 'render', Date.parse('2026-09-29T00:00:00Z'));
assert.equal(eta.label(key, Date.parse('2026-09-29T00:00:00Z')), '距开始 1 分 0 秒');
assert.equal(eta.label(key, Date.parse('2026-09-29T00:01:01Z')), '等待开始');
scheduled.scheduled_at = 'invalid';
eta.observe(scheduled, 'render', 1000);
assert.equal(eta.label(key, 1000), '等待预约开始');

const upscale = { id: 'upscale', status: 'running', total_frames: 100, processed_frames: 0, phase: 'prechecking' };
key = eta.observe(upscale, 'video_upscale', 1000);
upscale.processed_frames = 20;
eta.observe(upscale, 'video_upscale', 11000);
assert.equal(eta.label(key, 11000), '正在检查视频，暂无法估算');
upscale.phase = 'upscaling';
eta.observe(upscale, 'video_upscale', 12000);
assert.equal(eta.label(key, 12000), '正在估算剩余时间');
upscale.processed_frames = 40;
eta.observe(upscale, 'video_upscale', 22000);
assert.equal(eta.label(key, 22000), '预计剩余约 30 秒');
upscale.phase = 'joining';
eta.observe(upscale, 'video_upscale', 23000);
assert.equal(eta.label(key, 23000), '正在完成最后处理');
upscale.phase = 'upscaling';
upscale.total_frames = 200;
eta.observe(upscale, 'video_upscale', 24000);
assert.equal(eta.label(key, 24000), '正在估算剩余时间');

// Normalize every renderer, including failed work, concurrent workers and single-file conversions.
assert.equal(eta.progress({ count: 4, items: [{ status: 'failed', progress: .1 }, { status: 'succeeded' }, { status: 'running', progress: .5 }, { status: 'pending' }] }), .625);
for (const type of ['render', 'cluster', 'batch_dedup', 'folder_concat']) {
  assert.equal(eta.progress({ count: 4, progress: .625 }, type), .625);
}
assert.equal(eta.progress({ total: 4, completed: 1, current_progress: .5 }, 'landscape_to_portrait'), .375);
assert.equal(eta.progress({ total: 4, completed: 1, mode: 'cluster', items: [{ status: 'running', progress: .5 }, { status: 'running', progress: .5 }, { status: 'completed', progress: 1 }] }, 'portrait_to_landscape'), .5);
assert.equal(eta.progress({ total: 1, completed: 0, current_progress: .5 }, 'prores_alpha'), .5);
assert.equal(eta.progress({ progress: .5 }, 'timeline_slice'), .5);
assert.equal(eta.progress({ progress: .5 }, 'worker'), .5);
assert.equal(eta.progress({ total_frames: 100, processed_frames: 20, items: [{ status: 'failed', frames: 40, processed_frames: 10 }] }, 'video_upscale'), .5);
assert.equal(eta.progress({ total_frames: 0 }, 'video_upscale'), 0);
assert.equal(eta.progress({ count: 0, items: [{}] }), 0);

// The shared clock updates text only, preserves DOM nodes and needs no server calls.
key = eta.observe({ id: 'clock', status: 'scheduled', scheduled_at: 60 }, 'render', 1000);
const element = { dataset: { renderEta: key }, textContent: '' };
eta.tick({ querySelectorAll: () => [element] }, 2000);
assert.equal(element.textContent, '距开始 58 秒');
eta.tick({ querySelectorAll: () => [element] }, 3000);
assert.equal(element.textContent, '距开始 57 秒');
console.log('render ETA: countdown, stages, retry, stalls, scheduling, renderer progress and shared clock ok');
