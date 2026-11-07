import { ApiError, deleteServerTrace, getServerTrace, sessionEpoch } from '../api';
import { NetworkMonitor, type ExportLease, type MonitorScope } from './monitor';
import { networkMonitor, observe } from './runtime';
import { identifier, parseServerTrace, type ServerStatus } from './serverTrace';

interface Transport {
  read(capture: string, signal: AbortSignal): Promise<unknown>;
  clear(capture: string, epoch: number, signal: AbortSignal): Promise<void>;
  epoch(): number;
}
interface Reading {
  lease: ExportLease; scope: MonitorScope; ws: MonitorScope; sync: MonitorScope;
  controller: AbortController; unsubscribe?: () => void;
  owner?: object;
}
/** Explicit actions only: importing this module never opens a request or subscription. */
export class ServerTraceClient {
  private current?: Reading;
  private monitor: NetworkMonitor;
  private transport: Transport;
  constructor(monitor: NetworkMonitor, transport: Transport) { this.monitor = monitor; this.transport = transport; }
  private valid(reading: Reading) {
    return observe(() => this.current === reading && !reading.controller.signal.aborted && this.monitor.validExport(reading.lease)
      && this.monitor.valid(reading.scope) && this.monitor.valid(reading.ws) && this.monitor.valid(reading.sync)) === true;
  }
  private release(reading: Reading) {
    observe(() => reading.unsubscribe?.()); reading.unsubscribe = undefined;
    observe(() => reading.controller.abort());
    if (this.current === reading) this.current = undefined;
  }
  cancel(owner?: object) {
    const reading = this.current; if (!reading || owner && reading.owner !== owner) return;
    observe(() => this.monitor.setServerStatus(reading.lease, reading.scope, 'cancelled'));
    this.release(reading);
  }
  private begin(owner?: object): Reading | ServerStatus {
    this.cancel();
    const lease = this.monitor.exportLease(), state = this.monitor.snapshot();
    if (!lease || !identifier(lease.captureId) || !state.active || !state.detailed || state.paused) return 'unavailable';
    const scope = this.monitor.replaceConnection('server'), ws = this.monitor.scope('ws'), sync = this.monitor.scope('sync');
    if (!scope || !ws || !sync) return 'unavailable';
    const reading: Reading = { lease, scope, ws, sync, controller: new AbortController(), owner }; this.current = reading;
    try {
      reading.unsubscribe = this.monitor.onInvalidated(() => {
        if (!this.valid(reading)) { observe(() => this.monitor.setServerStatus(lease, scope, 'cancelled')); this.release(reading); }
      });
      if (!reading.unsubscribe) {
        observe(() => this.monitor.setServerStatus(lease, scope, 'capacity')); this.release(reading); return 'capacity';
      }
      return reading;
    } catch (error) { this.release(reading); throw error; }
  }
  private failure(reading: Reading, error: unknown): ServerStatus {
    if (!this.valid(reading)) return 'cancelled';
    const state = error instanceof ApiError && error.code === 'diagnostics_invalid' ? 'invalid' : 'unavailable';
    observe(() => this.monitor.setServerStatus(reading.lease, reading.scope, state)); return state;
  }
  async refresh(owner?: object): Promise<ServerStatus> {
    let reading: Reading | undefined;
    try {
      const started = this.begin(owner); if (typeof started === 'string') return started; reading = started;
      this.monitor.setServerStatus(reading.lease, reading.scope, 'loading');
      if (!this.valid(reading)) return 'cancelled';
      const raw = await this.transport.read(reading.lease.captureId!, reading.controller.signal);
      if (!this.valid(reading)) return 'cancelled';
      const batch = parseServerTrace(raw, reading.lease.captureId);
      if (!this.valid(reading)) return 'cancelled';
      if (!batch) { this.monitor.setServerStatus(reading.lease, reading.scope, 'invalid'); return 'invalid'; }
      return this.monitor.importServer(reading.lease, reading.scope, batch);
    } catch (error) { return reading ? this.failure(reading, error) : 'unavailable'; }
    finally { if (reading) this.release(reading); }
  }
  /** Clears local history first; a failed remote cleanup cannot undo that action. */
  async clear(owner?: object): Promise<ServerStatus> {
    let reading: Reading | undefined;
    try {
      const lease = this.monitor.exportLease();
      this.cancel(); this.monitor.clearHistory();
      if (!lease || !identifier(lease.captureId)) return 'ready';
      const epoch = this.transport.epoch();
      const started = this.begin(owner); if (typeof started === 'string') return started; reading = started;
      if (reading.lease.sessionId !== lease.sessionId || !this.valid(reading)) return 'cancelled';
      await this.transport.clear(lease.captureId, epoch, reading.controller.signal);
      return this.valid(reading) ? 'ready' : 'cancelled';
    } catch { return reading && !this.valid(reading) ? 'cancelled' : 'unavailable'; }
    finally { if (reading) this.release(reading); }
  }
}
interface Clearing { lease: ExportLease | null; unsubscribe?: () => void }
/** One page's entry guards and synchronous clear gate; owns only its own diagnostic actions. */
export class MonitorViewActions {
  private disposed = false; private clearing?: Clearing;
  private monitor: NetworkMonitor; private client: ServerTraceClient; private sessionId: string | null; private busy: (value: boolean) => void;
  constructor(monitor: NetworkMonitor, client: ServerTraceClient, sessionId: string | null, busy: (value: boolean) => void = () => {}) {
    this.monitor = monitor; this.client = client; this.sessionId = sessionId; this.busy = busy;
  }
  valid = () => !this.disposed && !!this.sessionId && this.monitor.snapshot().sessionId === this.sessionId;
  canRun = () => this.valid() && !this.clearing;
  activate() {
    if (this.disposed && this.sessionId && this.monitor.snapshot().sessionId === this.sessionId) { this.disposed = false; observe(() => this.busy(false)); }
  }
  setDetailed(value: boolean) { if (this.valid()) observe(() => this.monitor.setDetailed(value)); }
  togglePaused() { if (this.valid()) observe(() => this.monitor.setPaused(!this.monitor.snapshot().paused)); }
  private release(clearing: Clearing) {
    observe(() => clearing.unsubscribe?.()); clearing.unsubscribe = undefined;
    if (this.clearing !== clearing) return;
    this.clearing = undefined; if (!this.disposed) observe(() => this.busy(false));
  }
  async run(kind: 'refresh' | 'clear'): Promise<ServerStatus | null> {
    if (!this.canRun()) return null;
    const clearing: Clearing | undefined = kind === 'clear' ? { lease: null } : undefined;
    if (clearing) {
      this.clearing = clearing;
      clearing.unsubscribe = observe(() => this.monitor.onInvalidated(() => {
        if (!this.valid() || clearing.lease && !this.monitor.validExport(clearing.lease)) this.release(clearing);
      }));
      if (!clearing.unsubscribe) { this.release(clearing); return null; }
      observe(() => this.busy(true));
    }
    try {
      if (!this.valid()) return null;
      const pending = this.client[kind](this), lease = this.monitor.exportLease();
      if (clearing) clearing.lease = lease;
      const result = await pending;
      return this.valid() && this.monitor.validExport(lease) ? result : null;
    } catch { return null; }
    finally { if (clearing) this.release(clearing); }
  }
  dispose = () => {
    this.disposed = true;
    if (this.clearing) this.release(this.clearing);
    observe(() => this.client.cancel(this));
  };
}
export const serverTraceClient = new ServerTraceClient(networkMonitor, { read: getServerTrace, clear: deleteServerTrace, epoch: sessionEpoch });
