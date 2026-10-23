import { ApiError, assertSessionEpoch, errorMessage, getChanges, getSnapshotPage, openSyncEvents, sessionEpoch, type Capabilities, type ChangePage, type SnapshotPage, type SyncHint, type EventConnection } from '../api';
import type { AppData, Ticket } from '../data';
import type { AccountEventPort, ClientPeerMessage, EventConnectionState, PeerMessage } from '../media/peerProtocol';

export interface SyncView { data: AppData; ready: boolean; rebuilding: boolean; error: string; connected: boolean; cursor: number; serverId: string; capabilities?: Capabilities }
export interface SyncTransport {
  snapshot: (token: string | null, signal: AbortSignal) => Promise<SnapshotPage>;
  changes: (serverId: string, after: number, signal: AbortSignal) => Promise<ChangePage>;
  events: (hint: (value: SyncHint | PeerMessage) => void, close: () => void) => EventConnection;
}
const defaultTransport: SyncTransport = { snapshot: getSnapshotPage, changes: getChanges, events: openSyncEvents };
const abortError = () => new DOMException('同步已取消。', 'AbortError');

export class AccountSync implements AccountEventPort {
  private view: SyncView = { data: { tickets: [] }, ready: false, rebuilding: false, error: '', connected: false, cursor: 0, serverId: '' };
  private listeners = new Set<() => void>();
  private generation = 0;
  private scope = 0;
  private running = false;
  private needSnapshot = true;
  private desiredCursor = 0;
  private requested = 0;
  private completed = 0;
  private pull: Promise<void> | null = null;
  private request: AbortController | null = null;
  private socket: { stop: () => void; send: (message: ClientPeerMessage) => boolean; ready: boolean; serverId: string } | null = null;
  private connectionGeneration = 0;
  private connectionListeners = new Set<() => void>();
  private eventListeners = new Set<(message: PeerMessage) => void>();
  eventState = (): EventConnectionState => ({ connected: !!this.socket?.ready && this.view.ready && this.socket.serverId === this.view.serverId, serverId: this.socket?.serverId ?? '', generation: this.connectionGeneration });
  subscribeConnection = (listener: () => void) => { this.connectionListeners.add(listener); return () => { this.connectionListeners.delete(listener); }; };
  subscribeEvents = (listener: (message: PeerMessage) => void) => { this.eventListeners.add(listener); return () => { this.eventListeners.delete(listener); }; };
  sendEvent = (message: ClientPeerMessage) => this.eventState().connected && !!this.socket?.send(message);
  private timer: ReturnType<typeof setTimeout> | undefined;
  private watchdog: ReturnType<typeof setTimeout> | undefined;
  private retryDelay = 1000;
  readonly userId: string;
  private transport: SyncTransport;
  constructor(userId: string, transport: SyncTransport = defaultTransport) { this.userId = userId; this.transport = transport; }
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  snapshot = () => this.view;
  private publish(update: Partial<SyncView>) { this.view = { ...this.view, ...update }; for (const listener of this.listeners) listener(); for (const listener of this.connectionListeners) listener(); }
  private guard(generation = this.generation) { assertSessionEpoch(this.scope); if (!this.running || generation !== this.generation) throw abortError(); }
  start() {
    this.stop(); this.running = true; this.scope = sessionEpoch(); this.retryDelay = 1000;
    this.connect(); this.background(true);
  }
  stop() {
    this.running = false; this.generation++; this.request?.abort(); this.request = null; this.pull = null;
    this.socket?.stop(); this.socket = null; clearTimeout(this.timer); clearTimeout(this.watchdog); this.timer = undefined; this.watchdog = undefined;
    this.desiredCursor = this.view.cursor; this.requested = this.completed = 0;
    this.connectionGeneration++; for (const listener of this.connectionListeners) listener();
  }
  private report(error: unknown) {
    try { this.guard(); } catch { return; }
    if (error instanceof DOMException && error.name === 'AbortError') return;
    this.publish({ error: errorMessage(error), rebuilding: false });
  }
  private background(force: boolean, target = 0) {
    void this.catchUp(target, force).then(() => { if (this.socket?.ready) this.retryDelay = 1000; }).catch(error => { this.report(error); this.scheduleReconnect(); });
  }
  refresh = () => { if (this.running) { this.background(true); if (!this.socket) this.connect(); } };
  private scheduleReconnect() {
    if (!this.running || this.timer) return;
    const generation = this.generation;
    this.timer = setTimeout(() => {
      this.timer = undefined;
      try { this.guard(generation); } catch { return; }
      this.background(true); this.connect();
      this.retryDelay = Math.min(30000, this.retryDelay * 2);
    }, this.retryDelay);
  }
  private connect() {
    if (!this.running || this.socket) return;
    const generation = this.generation, socket = { stop: () => {}, send: (_message: ClientPeerMessage) => false, ready: false, serverId: '' };
    this.connectionGeneration++;
    this.socket = socket;
    const close = () => {
      if (this.socket !== socket || generation !== this.generation || !this.running) return;
      socket.stop(); this.socket = null; clearTimeout(this.watchdog);
      this.publish({ connected: false }); this.scheduleReconnect();
    };
    try {
      const connection = this.transport.events(value => {
        if (this.socket !== socket || generation !== this.generation || !this.running) return;
        try { this.guard(generation); } catch { return; }
        if (value.type !== 'sync.ready' && value.type !== 'sync.hint') {
          if (value.serverId !== socket.serverId || value.userId !== this.userId || !this.eventState().connected) return;
          for (const listener of this.eventListeners) listener(value as PeerMessage);
          return;
        }
        const hint = value as SyncHint;
        if (hint.userId !== this.userId) { close(); return; }
        const wasReady = socket.ready;
        socket.ready = true; socket.serverId = hint.serverId; clearTimeout(this.watchdog); this.watchdog = setTimeout(close, 45000);
        if (!wasReady) { this.retryDelay = 1000; clearTimeout(this.timer); this.timer = undefined; }
        this.publish({ connected: true });
        if (this.view.serverId && this.view.serverId !== hint.serverId) { close(); this.needSnapshot = true; this.desiredCursor = 0; this.request?.abort(); this.background(true); }
        else if (!wasReady || !this.view.ready || this.needSnapshot || this.view.error || hint.cursor > this.view.cursor) this.background(!wasReady || !!this.view.error, hint.cursor);
      }, close);
      socket.stop = connection.stop; socket.send = connection.send;
      if (!socket.ready) this.watchdog = setTimeout(close, 8000);
    } catch { close(); }
  }
  async catchUp(target = 0, force = true, expectedServer?: string): Promise<AppData> {
    this.guard();
    const generation = this.generation;
    if (expectedServer && this.view.serverId && expectedServer !== this.view.serverId) {
      this.needSnapshot = true; this.desiredCursor = 0; this.request?.abort(); force = true;
    }
    this.desiredCursor = Math.max(this.desiredCursor, target);
    const requested = force ? ++this.requested : this.requested;
    do {
      if (!this.pull) {
        const task = this.drain(generation);
        this.pull = task;
        void task.finally(() => { if (this.pull === task) this.pull = null; }).catch(error => {
          if (generation === this.generation && this.running && !(error instanceof DOMException && error.name === 'AbortError')) { this.report(error); this.scheduleReconnect(); }
        });
      }
      let unsubscribe = () => {};
      const barrier = new Promise<void>(resolve => {
        const check = () => { if (this.view.ready && !this.needSnapshot && (!expectedServer || this.view.serverId === expectedServer) && this.completed >= requested && this.view.cursor >= target) resolve(); };
        unsubscribe = this.subscribe(check); check();
      });
      try { await Promise.race([this.pull, barrier]); } finally { unsubscribe(); }
      this.guard(generation);
      if (expectedServer && this.view.serverId !== expectedServer) throw new ApiError('同步服务已变更，请核对操作结果。');
      if (this.view.cursor < target) throw new ApiError('尚未追上已确认操作，请核对后重试。');
    } while (this.completed < requested);
    return this.view.data;
  }
  private async fullSnapshot(signal: AbortSignal, generation: number) {
    let token: string | null = null, first: SnapshotPage | undefined, bytes = 0;
    const tickets = new Map<string, Ticket>(), tokens = new Set<string>();
    do {
      const page = await this.transport.snapshot(token, signal); this.guard(generation);
      if (signal.aborted) throw abortError();
      first ??= page;
      if (page.userId !== this.userId || page.snapshotId !== first.snapshotId || page.serverId !== first.serverId || page.cursor !== first.cursor
        || page.total !== first.total || page.expiresAt !== first.expiresAt || page.offset !== tickets.size) throw new ApiError('快照页面不一致，需要重新读取。', 410, 'snapshot_expired');
      bytes += new TextEncoder().encode(JSON.stringify(page.tickets)).byteLength;
      if (page.total > 10000 || bytes > 16 * 1024 * 1024) throw new ApiError('票夹快照超过容量限制。');
      for (const ticket of page.tickets) { if (tickets.has(ticket.id)) throw new ApiError('快照包含重复票根。'); tickets.set(ticket.id, ticket); }
      token = page.nextPageToken;
      if (token && (!page.tickets.length || tokens.has(token))) throw new ApiError('快照分页没有前进。');
      if (token) tokens.add(token);
      if ((tickets.size < page.total) !== (token !== null)) throw new ApiError('快照总数与分页不一致。');
    } while (token);
    return { tickets, cursor: first!.cursor, serverId: first!.serverId, capabilities: first!.capabilities };
  }
  private async drain(generation: number) {
    let restarts = 0, passes = 0, stagnant = 0;
    do {
      this.guard(generation);
      if (++passes > 256) throw new ApiError('同步积压较多，请稍后继续核对。');
      const serial = this.requested, controller = new AbortController(); this.request = controller;
      try {
        const replacing = this.needSnapshot || !this.view.ready;
        if (replacing) this.publish({ rebuilding: true });
        const baseline = replacing ? await this.fullSnapshot(controller.signal, generation)
          : { tickets: new Map(this.view.data.tickets.map(ticket => [ticket.id, ticket])), cursor: this.view.cursor, serverId: this.view.serverId, capabilities: this.view.capabilities! };
        this.guard(generation); if (controller.signal.aborted) throw abortError();
        let page: ChangePage;
        let calibrationHead: number | null = null;
        do {
          if (++passes > 256) throw new ApiError('同步积压较多，请稍后继续核对。');
          page = await this.transport.changes(baseline.serverId, baseline.cursor, controller.signal);
          this.guard(generation); if (controller.signal.aborted) throw abortError();
          if (page.userId !== this.userId || page.serverId !== baseline.serverId || page.fromCursor !== baseline.cursor) throw new ApiError('同步范围已变化。', 410, 'server_changed');
          calibrationHead ??= page.headCursor;
          for (const change of page.changes) {
            if (change.cursor !== baseline.cursor + 1) throw new ApiError('增量游标不连续。', 410, 'cursor_gap');
            const existing = baseline.tickets.get(change.type === 'upsert' ? change.ticket.id : change.id);
            if (existing && (change.type === 'upsert' ? change.ticket.version : change.version) < existing.version) throw new ApiError('票根版本倒退。', 410, 'cursor_gap');
            if (change.type === 'delete') baseline.tickets.delete(change.id); else baseline.tickets.set(change.ticket.id, change.ticket);
            baseline.cursor = change.cursor;
          }
          if (page.nextCursor !== baseline.cursor || page.hasMore && !page.changes.length) throw new ApiError('增量没有完整前进。', 410, 'cursor_gap');
          baseline.capabilities = page.capabilities;
        } while (page.hasMore && baseline.cursor < calibrationHead);
        this.needSnapshot = false; this.completed = serial;
        this.publish({ data: { tickets: [...baseline.tickets.values()] }, cursor: baseline.cursor, serverId: baseline.serverId, capabilities: baseline.capabilities, ready: true, rebuilding: false, error: '' });
        if (this.socket?.serverId && this.socket.serverId !== baseline.serverId) {
          const old = this.socket; this.socket = null; old.stop(); clearTimeout(this.watchdog);
          this.publish({ connected: false }); this.connect();
        }
        if (this.desiredCursor > page.headCursor && !page.changes.length) { if (++stagnant > 2) throw new ApiError('同步水位尚未追上提示，请稍后重试。'); } else stagnant = 0;
        if (page.hasMore) this.desiredCursor = Math.max(this.desiredCursor, page.headCursor);
      } catch (error) {
        this.guard(generation);
        if (error instanceof ApiError && error.status === 410 && ++restarts <= 3 || controller.signal.aborted && this.needSnapshot && ++restarts <= 3) {
          this.needSnapshot = true; this.desiredCursor = 0; continue;
        }
        throw error;
      } finally { if (this.request === controller) this.request = null; }
    } while (this.needSnapshot || this.completed < this.requested || this.view.cursor < this.desiredCursor);
  }
}
