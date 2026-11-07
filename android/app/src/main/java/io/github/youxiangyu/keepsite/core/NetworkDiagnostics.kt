package io.github.youxiangyu.keepsite.core

import okhttp3.HttpUrl
import okhttp3.Request
import java.util.concurrent.CancellationException

data class ApiNetworkState(val paused: Boolean = true, val active: Int = 0, val lastSuccessAt: Long? = null,
                           val lastFailureAt: Long? = null, val lastDurationMs: Long? = null, val lastStatus: Int? = null,
                           val lastError: MonitorError = MonitorError.NONE)
data class NativeNetworkState(val observations: MonitorSnapshot, val api: ApiNetworkState,
                              val socket: SocketNetworkState, val sync: SyncNetworkState, val serverRead: ServerReadState = ServerReadState(),
                              val media: MediaNetworkState = MediaNetworkState(), val peer: PeerNetworkState = PeerNetworkState(),
                              val peerTransfers: List<PeerTransferState> = emptyList())
data class ServerReadState(val status: MonitorStatus = MonitorStatus.UNKNOWN, val error: MonitorError = MonitorError.NONE, val httpStatus: Int? = null)

/** Only diagnostic actions enter this failure boundary; business execution stays outside it. */
internal fun <T> diagnostic(fallback: T, action: () -> T): T = try { action() } catch (_: Exception) { fallback }

internal fun diagnosticIoError(failure: java.io.IOException): MonitorError = when (failure) {
    is java.net.SocketTimeoutException -> MonitorError.TIMEOUT
    is java.nio.charset.CharacterCodingException -> MonitorError.PROTOCOL
    else -> MonitorError.NETWORK
}

class NetworkDiagnostics internal constructor(internal val monitor: NetworkMonitor = NetworkMonitor()) {
    private data class HttpOwner(val account: NetworkMonitor.Account, val connection: NetworkMonitor.Connection, val epoch: Long)
    private var account: NetworkMonitor.Account? = null
    private val lanes = mutableMapOf<MonitorLane, NetworkMonitor.Connection>()
    private var http: HttpOwner? = null
    private var api = ApiNetworkState()
    private var socket = SocketNetworkState()
    private var sync = SyncNetworkState()
    private var media = MediaNetworkState()
    private var peer = PeerNetworkState()
    private val activePeerTransfers = linkedMapOf<PeerTransferLive, PeerTransferState>()
    private val recentPeerTransfers = ArrayDeque<PeerTransferState>()
    private var syncSpace: String? = null
    private var socketSpace: String? = null
    private var socketGeneration: Long? = null
    private var ignoredWatermarkSocket: NetworkMonitor.Connection? = null
    private var serverAction: ServerAction? = null
    private var serverRead = ServerReadState()

    class ServerAction internal constructor(private val owner: NetworkDiagnostics, internal val read: NetworkMonitor.ServerRead,
        val epoch: Long, val captureId: String, val clearing: Boolean) {
        internal var cancel: (() -> Unit)? = null
        internal var result: MonitorError? = null
        internal fun current() = owner.serverActionCurrent(this)
        internal fun attach(cancel: () -> Unit) = owner.attachServerCall(this, cancel)
        internal fun received(batch: ServerTraceBatch) = owner.finishServerRead(this, batch)
        internal fun finished(error: MonitorError, code: Int? = null) = owner.finishServerAction(this, error, code)
    }
    private fun invalidateServerAction() {
        val old = serverAction; serverAction = null; serverRead = ServerReadState()
        val cancel = old?.cancel; old?.cancel = null; diagnostic(Unit) { cancel?.invoke() }
    }
    @Synchronized internal fun prepareServerAction(expected: Long, clearing: Boolean): ServerAction? = diagnostic(null) {
        if (serverAction?.let { it.clearing && serverActionCurrent(it) } == true) return@diagnostic null
        val binding = http?.takeIf { it.epoch == expected } ?: return@diagnostic null
        val original = monitor.prepareServerRead(binding.connection) ?: return@diagnostic null
        invalidateServerAction()
        if (clearing) { recentPeerTransfers.clear(); monitor.clearHistory(binding.account) } // DELETE targets only the captured old ID.
        val guard = monitor.prepareServerRead(binding.connection) ?: return@diagnostic null
        ServerAction(this, guard, expected, original.captureId, clearing).also {
            serverAction = it; serverRead = ServerReadState(MonitorStatus.STARTED)
        }
    }
    @Synchronized internal fun serverActionCurrent(action: ServerAction) = serverAction === action && http?.epoch == action.epoch && monitor.serverReadCurrent(action.read)
    @Synchronized private fun attachServerCall(action: ServerAction, cancel: () -> Unit): Boolean {
        if (!serverActionCurrent(action) || action.cancel != null) { diagnostic(Unit) { cancel() }; return false }
        action.cancel = cancel; return true
    }
    @Synchronized private fun finishServerRead(action: ServerAction, batch: ServerTraceBatch): Boolean = diagnostic(false) {
        if (action.clearing || !serverActionCurrent(action)) return@diagnostic false
        val installed = monitor.installServer(action.read, batch)
        if (installed != MonitorError.NONE) { finishServerAction(action, installed, 200); return@diagnostic false }
        val available = batch.domain.expiresAt != null
        finishServerAction(action, if (available) MonitorError.NONE else MonitorError.UNAVAILABLE, 200)
        available
    }
    @Synchronized private fun finishServerAction(action: ServerAction, error: MonitorError, code: Int?): Boolean {
        if (!serverActionCurrent(action)) return false
        serverAction = null; action.cancel = null; action.result = error
        serverRead = ServerReadState(if (error == MonitorError.NONE) MonitorStatus.COMPLETED else if (error == MonitorError.CANCELLED) MonitorStatus.CANCELLED else MonitorStatus.FAILED,
            error, code?.takeIf { it in 100..599 })
        return true
    }

    @Synchronized fun openAccount(epoch: Long): NetworkMonitor.Account? = diagnostic(null) {
        invalidateServerAction()
        activePeerTransfers.clear(); recentPeerTransfers.clear()
        account?.let(monitor::closeAccount); account = null; lanes.clear(); http = null; api = ApiNetworkState()
        socket = SocketNetworkState(); sync = SyncNetworkState(); media = MediaNetworkState(); peer = PeerNetworkState(); syncSpace = null; socketSpace = null; ignoredWatermarkSocket = null
        account = monitor.openAccount()
        account?.also { resume(it, epoch) }
    }
    @Synchronized fun closeAccount(owner: NetworkMonitor.Account?) = diagnostic(Unit) {
        if (owner != null && owner === account) {
            invalidateServerAction()
            activePeerTransfers.clear(); recentPeerTransfers.clear()
            monitor.closeAccount(owner); account = null; lanes.clear(); http = null; api = ApiNetworkState()
            socket = SocketNetworkState(); sync = SyncNetworkState(); media = MediaNetworkState(); peer = PeerNetworkState(); syncSpace = null; socketSpace = null; ignoredWatermarkSocket = null
        }
    }
    @Synchronized fun resume(owner: NetworkMonitor.Account, epoch: Long) = diagnostic(Unit) {
        if (account !== owner) return@diagnostic
        if (http?.epoch != null && http?.epoch != epoch) { activePeerTransfers.clear(); recentPeerTransfers.clear(); peer = PeerNetworkState() }
        val connection = connect(owner, MonitorLane.HTTP) ?: return@diagnostic
        http = HttpOwner(owner, connection, epoch); api = api.copy(paused = false, active = 0)
    }
    @Synchronized fun suspend(owner: NetworkMonitor.Account?) = diagnostic(Unit) {
        if (owner == null || account !== owner) return@diagnostic
        invalidateServerAction()
        activePeerTransfers.clear(); recentPeerTransfers.clear()
        lanes.values.toList().forEach(monitor::disconnect); lanes.clear(); http = null
        api = api.copy(paused = true, active = 0)
        socket = socket.copy(status = MonitorStatus.PAUSED, opened = false, aligned = false, heartbeatMs = null, diagnosticsVersion = null)
        sync = sync.copy(paused = true, running = false)
        media = media.copy(active = 0, status = MonitorStatus.PAUSED)
        peer = peer.copy(registered = false, active = 0, status = MonitorStatus.PAUSED)
    }
    @Synchronized fun connect(owner: NetworkMonitor.Account, lane: MonitorLane): NetworkMonitor.Connection? = diagnostic(null) {
        if (account === owner && lane == MonitorLane.HTTP) invalidateServerAction()
        if (account === owner && lane == MonitorLane.PEER) { activePeerTransfers.clear(); recentPeerTransfers.clear() }
        if (account !== owner) null else monitor.connect(owner, lane)?.also { lanes[lane] = it }
    }
    @Synchronized fun disconnect(connection: NetworkMonitor.Connection?) = diagnostic(Unit) {
        if (connection != null && lanes[connection.lane] === connection) {
            if (connection.lane == MonitorLane.HTTP) invalidateServerAction()
            if (connection.lane == MonitorLane.PEER) activePeerTransfers.clear()
            monitor.disconnect(connection); lanes.remove(connection.lane)
        }
    }
    @Synchronized internal fun current(connection: NetworkMonitor.Connection) = account === connection.account && lanes[connection.lane] === connection
    @Synchronized fun setDetailed(enabled: Boolean) = diagnostic(Unit) {
        if (monitor.snapshot().detailed != enabled) invalidateServerAction()
        account?.let { monitor.setDetailed(it, enabled) }
    }
    @Synchronized fun pauseHistory(paused: Boolean) = diagnostic(Unit) {
        if (monitor.snapshot().historyPaused != paused) invalidateServerAction()
        account?.let { monitor.pauseHistory(it, paused) }
    }
    @Synchronized fun clearHistory() = diagnostic(Unit) { invalidateServerAction(); recentPeerTransfers.clear(); account?.let(monitor::clearHistory) }
    @Synchronized fun snapshot(): NativeNetworkState? = diagnostic(null) {
        NativeNetworkState(monitor.snapshot(), api, socket, sync, serverRead, media, peer, activePeerTransfers.values.toList() + recentPeerTransfers.toList())
    }
    @Synchronized internal fun currentPeer(connection: NetworkMonitor.Connection, expected: Long) = current(connection) && http?.epoch == expected
    @Synchronized internal fun beginPeerTransfer(connection: NetworkMonitor.Connection, alias: String, expected: Long, outgoing: Boolean, operationId: String?, epoch: Long): PeerTransferLive? = diagnostic(null) {
        if (!currentPeer(connection, epoch) || activePeerTransfers.size >= 2) return@diagnostic null
        val id = operationId ?: java.util.UUID.randomUUID().toString().replace("-", "")
        val ticket = PeerTransferLive(this, connection, id, alias, outgoing, expected, epoch)
        activePeerTransfers[ticket] = PeerTransferState(id, alias, outgoing, MonitorStatus.STARTED,
            MonitorValues(bytes = 0, expectedBytes = expected, outgoing = outgoing).safe(), 0, System.currentTimeMillis(), null)
        ticket
    }
    @Synchronized internal fun peerTransferState(ticket: PeerTransferLive, state: PeerTransferState, terminal: Boolean) = diagnostic(Unit) {
        if (!currentPeer(ticket.connection, ticket.epoch) || !activePeerTransfers.containsKey(ticket)) return@diagnostic
        if (terminal) {
            activePeerTransfers.remove(ticket); recentPeerTransfers.addFirst(state)
            while (recentPeerTransfers.size > 50) recentPeerTransfers.removeLast()
        } else activePeerTransfers[ticket] = state
    }
    @Synchronized fun peer(owner: NetworkMonitor.Account?, expected: Long): PeerObservation? = diagnostic(null) {
        if (owner == null || owner !== account || http?.epoch != expected) null
        else connect(owner, MonitorLane.PEER)?.let { PeerObservation(this, it, owner, expected) }
    }
    @Synchronized internal fun socketCurrent(generation: Long) = lanes[MonitorLane.WS] != null && socket.opened && socket.aligned && socketGeneration == generation
    @Synchronized internal fun peerContext(connection: NetworkMonitor.Connection, operation: NetworkMonitor.Operation, expected: Long, generation: Long): MonitorTraceContext? = diagnostic(null) {
        if (!current(connection) || http?.epoch != expected || !socketCurrent(generation) || socket.diagnosticsVersion != 1) null
        else monitor.traceContext(operation, MonitorDestination.AUTHENTICATED_API)
    }
    @Synchronized internal fun peerState(connection: NetworkMonitor.Connection, state: PeerNetworkState, expected: Long) = diagnostic(Unit) { if (currentPeer(connection, expected)) peer = state }
    @Synchronized fun media(owner: NetworkMonitor.Account?, expected: Long, expectedBytes: Long): MediaObservation? = diagnostic(null) {
        if (owner == null || owner !== account || http?.epoch != expected) return@diagnostic null
        val connection = lanes[MonitorLane.MEDIA] ?: connect(owner, MonitorLane.MEDIA) ?: return@diagnostic null
        begin(connection, MonitorProtocol.MEDIA, MonitorEvent.MEDIA_READ, MonitorTemplate.MEDIA)?.let {
            MediaObservation(this, connection, it, expectedBytes).also { media = media.copy(active = media.active + 1, status = MonitorStatus.STARTED) }
        }
    }
    @Synchronized internal fun mediaFinished(connection: NetworkMonitor.Connection, status: MonitorStatus, route: MonitorRoute, bytes: Long?, expected: Long,
                                           verified: Boolean, duration: Long, error: MonitorError) = diagnostic(Unit) {
        if (current(connection)) media = MediaNetworkState((media.active - 1).coerceAtLeast(0), status, route, bytes, expected, verified,
            if (status == MonitorStatus.COMPLETED) System.currentTimeMillis() else media.lastSuccessAt, duration, error)
    }
    @Synchronized fun socket(owner: NetworkMonitor.Account?, expected: Long, generation: Long? = null): SocketObservation? = diagnostic(null) {
        if (http?.epoch != expected) null else owner?.let { connect(it, MonitorLane.WS) }?.let {
            socketSpace = null; socketGeneration = generation; ignoredWatermarkSocket = null; SocketObservation(this, it)
        }
    }
    @Synchronized fun sync(owner: NetworkMonitor.Account?, expected: Long): SyncObservation? = diagnostic(null) {
        if (http?.epoch != expected) null else owner?.let { connect(it, MonitorLane.SYNC) }?.let { connection ->
            begin(connection, MonitorProtocol.SYNC, MonitorEvent.DELTA)?.let { SyncObservation(this, connection, it) }
        }
    }
    @Synchronized internal fun socketState(connection: NetworkMonitor.Connection, state: SocketNetworkState) = diagnostic(Unit) {
        if (current(connection)) socket = state
    }
    @Synchronized internal fun syncStarted(connection: NetworkMonitor.Connection) = diagnostic(Unit) {
        if (current(connection)) sync = sync.copy(paused = false, running = true)
    }
    @Synchronized internal fun watermark(connection: NetworkMonitor.Connection, server: String, cursor: Long) = diagnostic(Unit) {
        if (!current(connection)) return@diagnostic
        if (connection.lane == MonitorLane.WS) {
            socketSpace = server
            if (connection === ignoredWatermarkSocket) return@diagnostic
        }
        if (syncSpace != server) { syncSpace = server; sync = SyncNetworkState(paused = false, running = sync.running, error = sync.error) }
        sync = sync.copy(observedCursor = maxOf(sync.observedCursor ?: 0, cursor))
    }
    @Synchronized internal fun syncRebuilding(connection: NetworkMonitor.Connection) = diagnostic(Unit) {
        if (!current(connection)) return@diagnostic
        syncSpace = null; sync = SyncNetworkState(paused = false, running = true, error = sync.error)
        ignoredWatermarkSocket = lanes[MonitorLane.WS]
    }
    @Synchronized internal fun syncSpace(connection: NetworkMonitor.Connection, server: String) = diagnostic(Unit) {
        if (!current(connection)) return@diagnostic
        if (syncSpace != server) { syncSpace = server; sync = SyncNetworkState(paused = false, running = true, error = sync.error) }
        if (ignoredWatermarkSocket === lanes[MonitorLane.WS] && socketSpace == server) ignoredWatermarkSocket = null
    }
    @Synchronized internal fun syncApplied(connection: NetworkMonitor.Connection, value: Synced, snapshotItems: Long?, deltaItems: Long, duration: Long) = diagnostic(Unit) {
        if (!current(connection)) return@diagnostic
        watermark(connection, value.serverId, value.cursor)
        val now = System.currentTimeMillis()
        sync = sync.copy(paused = false, running = false, localCursor = value.cursor, lastDeltaAt = now,
            lastSnapshotAt = if (snapshotItems != null) now else sync.lastSnapshotAt,
            lastDataChangeAt = if ((snapshotItems ?: 0) > 0 || deltaItems > 0) now else sync.lastDataChangeAt,
            lastSnapshotItems = snapshotItems ?: sync.lastSnapshotItems, lastDeltaItems = deltaItems, durationMs = duration, error = MonitorError.NONE)
    }
    @Synchronized internal fun syncFailed(connection: NetworkMonitor.Connection, error: MonitorError) = diagnostic(Unit) {
        if (current(connection)) sync = sync.copy(running = false, error = error)
    }

    fun begin(connection: NetworkMonitor.Connection?, protocol: MonitorProtocol, event: MonitorEvent,
              template: MonitorTemplate = MonitorTemplate.UNKNOWN, parent: NetworkMonitor.Operation? = null, device: NetworkMonitor.DeviceAlias? = null): NetworkMonitor.Operation? = diagnostic(null) {
        connection?.let { monitor.begin(it, protocol, event, template, device, parent) }
    }
    fun observe(operation: NetworkMonitor.Operation?, protocol: MonitorProtocol, event: MonitorEvent,
                status: MonitorStatus, values: MonitorValues = MonitorValues()) = diagnostic(false) {
        operation != null && monitor.observe(operation, protocol, event, status, values)
    }
    fun abandon(operation: NetworkMonitor.Operation?) = diagnostic(Unit) { operation?.let(monitor::abandon) }

    private fun template(request: Request, origin: HttpUrl): MonitorTemplate {
        if (!sameOrigin(request.url, origin)) return MonitorTemplate.EXTERNAL
        val path = request.url.encodedPath
        return when {
            path == "/api/auth/session" -> MonitorTemplate.SESSION
            path.startsWith("/api/auth/") -> MonitorTemplate.AUTH
            path == "/api/diagnostics/events" -> MonitorTemplate.DIAGNOSTICS
            path == "/api/sync/snapshot" -> MonitorTemplate.SNAPSHOT
            path == "/api/sync/changes" -> MonitorTemplate.DELTA
            path == "/api/media" -> MonitorTemplate.UPLOAD
            Regex("/api/media/[a-f0-9]{64}/download").matches(path) -> MonitorTemplate.DOWNLOAD
            Regex("/api/media/[a-f0-9]{64}").matches(path) -> MonitorTemplate.MEDIA
            path == "/api/tickets" || Regex("/api/tickets/[A-Za-z0-9_-]{1,128}(?:/process)?").matches(path) -> MonitorTemplate.TICKET
            else -> MonitorTemplate.UNKNOWN
        }
    }
    @Synchronized private fun startHttp(expected: Long, origin: HttpUrl, request: Request, parent: NetworkMonitor.Operation?): HttpObservation? = diagnostic(null) {
        val binding = http?.takeIf { it.epoch == expected && current(it.connection) } ?: return@diagnostic null
        val template = template(request, origin)
        if (template == MonitorTemplate.DIAGNOSTICS) return@diagnostic null
        val operation = monitor.begin(binding.connection, MonitorProtocol.HTTP, MonitorEvent.REQUEST, template, parent = parent) ?: return@diagnostic null
        HttpObservation(binding.connection, operation, template).also { api = api.copy(active = api.active + 1) }
    }

    fun <T> http(expected: Long, origin: HttpUrl, request: Request, authenticated: Boolean, parent: NetworkMonitor.Operation? = null,
                 business: (Request, HttpObservation?) -> T): T {
        val observation = startHttp(expected, origin, request, parent)
        val clean = request.newBuilder().removeHeader("X-Keepsite-Diagnostics").removeHeader("X-Keepsite-Trace-Id").build()
        val actual = diagnostic(clean) {
            val eligible = authenticated && sameOrigin(request.url, origin) && observation?.template !in setOf(
                MonitorTemplate.AUTH, MonitorTemplate.SESSION, MonitorTemplate.EXTERNAL, MonitorTemplate.DIAGNOSTICS, MonitorTemplate.UNKNOWN)
            val context = if (eligible && observation != null) monitor.traceContext(observation.operation, MonitorDestination.AUTHENTICATED_API) else null
            if (context == null) clean else clean.newBuilder().header("X-Keepsite-Diagnostics", context.captureId)
                .header("X-Keepsite-Trace-Id", context.traceId).build()
        }
        try {
            val result = business(actual, observation)
            observation?.finish(MonitorStatus.COMPLETED, MonitorError.NONE)
            return result
        } catch (failure: Throwable) {
            val error = when (failure) {
                is CancellationException -> MonitorError.CANCELLED
                is java.io.IOException -> diagnosticIoError(failure)
                is ApiFailure -> when (failure.status) { 401 -> MonitorError.AUTH_REQUIRED; 403 -> MonitorError.AUTH_DENIED; else -> MonitorError.PROTOCOL }
                else -> MonitorError.UNKNOWN
            }
            observation?.finish(if (failure is CancellationException) MonitorStatus.CANCELLED else MonitorStatus.FAILED, error)
            throw failure
        } finally { abandon(observation?.operation) }
    }
    inner class HttpObservation internal constructor(internal val connection: NetworkMonitor.Connection,
                                                    internal val operation: NetworkMonitor.Operation, internal val template: MonitorTemplate) {
        private val started = System.nanoTime()
        private var status: Int? = null
        private var bytes = 0L
        private var failureCategory: MonitorError? = null
        fun failedAs(error: MonitorError) { failureCategory = error }
        fun response(code: Int) { status = code; observe(operation, MonitorProtocol.HTTP, MonitorEvent.REQUEST,
            MonitorStatus.TRANSFERRING, MonitorValues(httpStatus = code)) }
        fun read(count: Int) { if (count > 0) bytes = if (Long.MAX_VALUE - bytes < count) Long.MAX_VALUE else bytes + count }
        internal fun finish(terminal: MonitorStatus, error: MonitorError) = diagnostic(Unit) {
            val actualError = if (error == MonitorError.CANCELLED) error else failureCategory ?: error
            observe(operation, MonitorProtocol.HTTP, MonitorEvent.REQUEST, terminal,
                MonitorValues(bytes = bytes, httpStatus = status, error = actualError))
            synchronized(this@NetworkDiagnostics) {
                if (!current(connection)) return@synchronized
                val now = System.currentTimeMillis()
                api = api.copy(active = (api.active - 1).coerceAtLeast(0), lastDurationMs = ((System.nanoTime() - started) / 1_000_000).coerceAtLeast(0),
                    lastSuccessAt = if (terminal == MonitorStatus.COMPLETED) now else api.lastSuccessAt,
                    lastFailureAt = if (terminal == MonitorStatus.FAILED) now else api.lastFailureAt,
                    lastStatus = status, lastError = actualError)
            }
        }
    }
    companion object {
        internal fun sameOrigin(url: HttpUrl, origin: HttpUrl) = url.scheme == origin.scheme && url.host == origin.host && url.port == origin.port
    }
}
