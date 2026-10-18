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

export function totalElapsedSeconds(job, now = Date.now()) {
  if (typeof job?.elapsed_seconds === 'number' && Number.isFinite(job.elapsed_seconds)) {
    return Math.max(0, job.elapsed_seconds);
  }
  const start = Date.parse(job?.started_at || job?.created_at || '');
  const finish = job?.finished_at ? Date.parse(job.finished_at) : now;
  return Number.isFinite(start) && Number.isFinite(finish) ? Math.max(0, (finish - start) / 1000) : null;
}
