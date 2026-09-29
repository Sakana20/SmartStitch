/* Shared estimates use existing progress observations; the clock never requests data. */
const RenderEta = (() => {
  const records = new Map();
  const terminal = new Set(['completed', 'succeeded', 'partial_failed', 'failed', 'cancelled', 'interrupted', 'skipped']);
  const fraction = value => Math.max(0, Math.min(1, Number(value) || 0));
  const number = value => Math.max(0, Number(value) || 0);

  function progress(job, type = job.job_type) {
    if (type === 'worker') return fraction(job.progress);
    if (type === 'video_upscale') {
      const retired = (job.items || []).filter(item => ['failed', 'cancelled', 'skipped'].includes(item.status))
        .reduce((sum, item) => sum + Math.max(0, number(item.frames) - number(item.processed_frames)), 0);
      return job.total_frames ? fraction((number(job.processed_frames) + retired) / job.total_frames) : 0;
    }
    if (type === 'timeline_slice') return fraction(job.progress);
    if (['landscape_to_portrait', 'portrait_to_landscape', 'prores_alpha'].includes(type)) {
      const active = job.mode === 'cluster'
        ? (job.items || []).filter(item => item.status === 'running').reduce((sum, item) => sum + fraction(item.progress), 0)
        : fraction(job.current_progress);
      return job.total ? fraction((number(job.completed) + active) / job.total) : 0;
    }
    if (job.items?.length) {
      return job.count ? fraction(job.items.reduce((sum, item) => sum + (terminal.has(item.status) ? 1 : fraction(item.progress)), 0) / job.count) : 0;
    }
    return job.progress != null ? fraction(job.progress) : job.count ? fraction((number(job.success_count) + number(job.failure_count)) / job.count) : 0;
  }

  function duration(seconds) {
    seconds = Math.max(1, Math.ceil(seconds));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor(seconds % 3600 / 60);
    const rest = seconds % 60;
    return `${hours ? `${hours} 小时 ` : ''}${minutes ? `${minutes} 分 ` : ''}${rest} 秒`;
  }

  function observe(job, type = job.job_type || 'render', now = Date.now()) {
    const key = encodeURIComponent(`${type}:${job.id || job.attempt_id}`);
    if (terminal.has(job.status) || job.status === 'draft') { records.delete(key); return null; }
    let message = '';
    let deadline = null;
    if (job.status === 'scheduled' || job.phase === 'waiting_start') {
      const start = typeof job.scheduled_at === 'number' ? job.scheduled_at * 1000 : Date.parse(job.scheduled_at);
      if (Number.isFinite(start) && start > now) deadline = start;
      message = '等待预约开始';
    } else if (job.status === 'cancelling') message = '正在取消';
    else if (job.status !== 'running' || ['waiting_workers', 'queued'].includes(job.phase) || job.current_phase === 'waiting_workers') message = '等待处理';
    else if (job.phase === 'prechecking' || job.current_phase === 'inspecting') message = '正在检查视频，暂无法估算';
    else if (['joining', 'audio', 'committing', 'verifying'].includes(job.phase)) message = '正在完成最后处理';
    const p = progress(job, type);
    // Reset for retries, resumes, phase changes and a changing batch/frame denominator.
    const signature = `${job.status}:${message}:${job.started_at || ''}:${job.total_frames || job.total || job.output_unit_count || job.count || ''}`;
    let record = records.get(key);
    if (!record || record.signature !== signature || p < record.progress - 0.0001) {
      record = { signature, progress: p, baseline: p, baselineAt: now, changedAt: now, rate: null, endAt: null };
      records.set(key, record);
    } else if (p > record.progress) {
      if (now - record.changedAt > 30000) {
        record.baseline = p;
        record.baselineAt = now;
        record.rate = null;
        record.endAt = null;
      }
      const elapsed = (now - record.baselineAt) / 1000;
      if (elapsed >= 5) {
        const measured = (p - record.baseline) / elapsed;
        record.rate = record.rate == null ? measured : record.rate * 0.7 + measured * 0.3;
        record.endAt = now + (1 - p) / record.rate * 1000;
        record.baseline = p;
        record.baselineAt = now;
      }
      record.progress = p;
      record.changedAt = now;
    }
    Object.assign(record, { message, deadline, seenAt: now });
    if (records.size > 2000) records.delete(records.keys().next().value);
    return key;
  }

  function label(key, now = Date.now()) {
    const record = records.get(key);
    if (!record) return '';
    if (record.deadline) return record.deadline > now ? `距开始 ${duration((record.deadline - now) / 1000)}` : '等待开始';
    if (record.message) return record.message;
    if (now - record.changedAt > 30000) return '进度暂未更新，正在重新估算';
    if (record.progress >= 0.999) return '正在完成最后处理';
    if (!record.rate || !Number.isFinite(record.endAt)) return '正在估算剩余时间';
    return record.endAt > now ? `预计剩余约 ${duration((record.endAt - now) / 1000)}` : '正在处理，等待进度更新';
  }

  function html(job, type) {
    const key = observe(job, type);
    return key ? `<span class="render-eta" data-render-eta="${key}">${label(key)}</span>` : '';
  }

  function tick(root = document, now = Date.now()) {
    root.querySelectorAll('[data-render-eta]').forEach(element => {
      const text = label(element.dataset.renderEta, now);
      if (element.textContent !== text) element.textContent = text;
    });
    for (const [key, record] of records) if (now - record.seenAt > 3600000) records.delete(key);
  }
  return { progress, observe, label, html, tick };
})();

if (typeof document !== 'undefined') setInterval(() => {
  if (!document.hidden) RenderEta.tick();
}, 1000);
if (typeof module !== 'undefined') module.exports = RenderEta;
