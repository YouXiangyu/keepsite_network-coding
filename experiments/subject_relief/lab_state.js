export const STALE_TASK_READ = Symbol('stale task read');

export class TaskEpoch {
  constructor() { this.value = 0; }
  capture() { return this.value; }
  invalidate() { this.value += 1; return this.value; }
  isCurrent(epoch) { return epoch === this.value; }

  async read(epoch, request) {
    try {
      const value = await request;
      return this.isCurrent(epoch) ? value : STALE_TASK_READ;
    } catch (error) {
      if (!this.isCurrent(epoch)) return STALE_TASK_READ;
      throw error;
    }
  }
}

const sameValue = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// Snapshots are { image_id, image_name?, mask_image_id, parameters, boxes }; a missing image_id skips input comparison.
export function snapshotChanges(base, next, fields) {
  const changes = [];
  if (!base || !next) return changes;
  for (const field of fields) {
    const before = base.parameters?.[field.key];
    const after = next.parameters?.[field.key];
    if (before === undefined || after === undefined || sameValue(before, after)) continue;
    changes.push({ key: field.key, field, before, after });
  }
  if (base.image_id && next.image_id) {
    if (base.image_id !== next.image_id) {
      changes.push({ key: 'image', before: base.image_name || base.image_id, after: next.image_name || next.image_id });
    }
    if ((base.mask_image_id || null) !== (next.mask_image_id || null)) {
      changes.push({ key: 'mask', before: base.mask_image_id || null, after: next.mask_image_id || null });
    }
    if (!sameValue(base.boxes || [], next.boxes || [])) {
      changes.push({ key: 'boxes', before: (base.boxes || []).length, after: (next.boxes || []).length });
    }
  }
  return changes;
}

export function flagValue(field, value) {
  if (value === null || value === undefined) return 'none';
  if (typeof value === 'boolean') return value ? 'on' : 'off';
  if (Array.isArray(value)) return value.join(',');
  if (typeof value === 'number' && Number.isFinite(value)) {
    const shown = Number((value * (field?.display_scale || 1)).toFixed(6));
    return field?.unit === '%' ? `${shown}%` : String(shown);
  }
  return String(value);
}

export function totalElapsedSeconds(job, now = Date.now()) {
  if (typeof job?.elapsed_seconds === 'number' && Number.isFinite(job.elapsed_seconds)) {
    return Math.max(0, job.elapsed_seconds);
  }
  const start = Date.parse(job?.started_at || job?.created_at || '');
  const finish = job?.finished_at ? Date.parse(job.finished_at) : now;
  return Number.isFinite(start) && Number.isFinite(finish) ? Math.max(0, (finish - start) / 1000) : null;
}
