import test from 'node:test';
import assert from 'node:assert/strict';
import { STALE_TASK_READ, TaskEpoch, totalElapsedSeconds } from './lab_state.js';

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

test('a late idle GET cannot clear a newly confirmed run or stop its next poll', async () => {
  const epoch = new TaskEpoch();
  const oldGet = deferred();
  const newPost = deferred();
  const state = { activeId: null, scheduledFor: null };
  const captured = epoch.capture();
  const refreshing = (async () => {
    try {
      const snapshot = await epoch.read(captured, oldGet.promise);
      if (snapshot === STALE_TASK_READ) return;
      state.activeId = snapshot.active_run_id;
    } finally {
      state.scheduledFor = state.activeId;
    }
  })();
  epoch.invalidate(); // Submission starts after the idle GET was sent.
  const submitting = newPost.promise.then((job) => {
    epoch.invalidate(); // The POST acknowledgement confirms this run.
    state.activeId = job.id;
  });
  newPost.resolve({ id: 'new-run' });
  await submitting;
  oldGet.resolve({ runs: [], active_run_id: null });
  await refreshing;
  assert.equal(state.activeId, 'new-run');
  assert.equal(state.scheduledFor, 'new-run');
});

test('cancellation invalidates a running detail response even after the list was read', async () => {
  const epoch = new TaskEpoch();
  const captured = epoch.capture();
  assert.deepEqual(await epoch.read(captured, Promise.resolve({ active_run_id: 'run-1' })), { active_run_id: 'run-1' });
  const oldDetail = deferred();
  const reading = epoch.read(captured, oldDetail.promise);
  epoch.invalidate();
  oldDetail.resolve({ id: 'run-1', status: 'running' });
  assert.equal(await reading, STALE_TASK_READ);
});

test('current read failures are reported, obsolete read failures are ignored', async () => {
  const epoch = new TaskEpoch();
  await assert.rejects(epoch.read(epoch.capture(), Promise.reject(new Error('offline'))), /offline/);
  const oldRead = deferred();
  const reading = epoch.read(epoch.capture(), oldRead.promise);
  epoch.invalidate();
  oldRead.reject(new Error('obsolete failure'));
  assert.equal(await reading, STALE_TASK_READ);
});

test('total time includes startup and never substitutes computation-only report time', () => {
  assert.equal(totalElapsedSeconds({ elapsed_seconds: 22.259, report: { elapsed_seconds: 20 } }), 22.259);
  assert.equal(totalElapsedSeconds({
    started_at: '2026-10-19T00:00:00.000Z', finished_at: '2026-10-19T00:00:22.259Z', report: { elapsed_seconds: 20 },
  }), 22.259);
  assert.equal(totalElapsedSeconds({ report: { elapsed_seconds: 20 } }), null);
  assert.equal(totalElapsedSeconds({ started_at: '2026-10-19T00:00:00.000Z' }, Date.parse('2026-10-19T00:00:30.000Z')), 30);
});
