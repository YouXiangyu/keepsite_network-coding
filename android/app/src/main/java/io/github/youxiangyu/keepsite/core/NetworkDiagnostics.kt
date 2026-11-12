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
                              val peerTransfers: List<PeerTransferState> = emptyList(), val httpActivities: List<HttpActivityState> = emptyList(),
                              val httpOmitted: Int = 0)
data class HttpActivityState(val id: String, val template: MonitorTemplate, val status: MonitorStatus, val startedAt: Long,
    val elapsedMs: Long, val httpStatus: Int? = null)
data class ServerReadState(val status: MonitorStatus = MonitorStatus.UNKNOWN, val error: MonitorError = MonitorError.NONE, val httpStatus: Int? = null, val clearing: Boolean = false)

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
    private var socketStartedNanos: Long? = null
    internal class HttpActivityOwner(val connection: NetworkMonitor.Connection, val startedNanos: Long, var ended: Boolean = false)
    private val httpActivities = linkedMapOf<HttpActivityOwner, HttpActivityState>()
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
    private val pageSeal = Any()
    private val reportSeal = Any()
    @Synchronized fun openPage(): NetworkPageOwner? = account?.let { NetworkPageOwner(this, it, pageSeal) }
    @Synchronized fun pageCurrent(page: NetworkPageOwner) = page.matches(pageSeal) && account === page.account
    @Synchronized fun pageSnapshot(page: NetworkPageOwner): NativeNetworkState? = if (pageCurrent(page)) snapshot() else null
    @Synchronized fun pageDetailed(page: NetworkPageOwner, enabled: Boolean) { if (pageCurrent(page)) setDetailed(enabled) }
    @Synchronized fun pagePause(page: NetworkPageOwner, paused: Boolean) { if (pageCurrent(page)) pauseHistory(paused) }
    @Synchronized fun pageClear(page: NetworkPageOwner) { if (pageCurrent(page)) clearHistory() }
    @Synchronized internal fun pageServerAction(page: NetworkPageOwner, expected: Long, clearing: Boolean): ServerAction? =
        if (pageCurrent(page)) prepareServerAction(expected, clearing) else null
    @Synchronized fun prepareReport(page: NetworkPageOwner, environment: NetworkReportEnvironment): NetworkReport? = diagnostic(null) {
        if (!pageCurrent(page)) return@diagnostic null
        val prepared = monitor.prepareExportState(page.account) ?: return@diagnostic null
        val state = snapshot()?.copy(observations = prepared.second) ?: return@diagnostic null
        NetworkReport(this, page.account, reportSeal, prepared.first, state, environment, System.currentTimeMillis())
    }
    @Synchronized fun reportCurrent(report: NetworkReport) = account === report.account && report.contents(reportSeal) != null && monitor.exportCurrent(report.history)
    internal fun renderReport(report: NetworkReport, format: NetworkReportFormat): String? = diagnostic(null) {
        if (!reportCurrent(report)) return@diagnostic null
        val (state, environment, capturedAt) = report.contents(reportSeal) ?: return@diagnostic null
        val history = monitor.exportJson(report.history) ?: return@diagnostic null
        val value = NetworkReports.format(NetworkReports.build(history, state, environment, capturedAt), state, format)
        value.takeIf { reportCurrent(report) }
    }

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
            serverAction = it; serverRead = ServerReadState(MonitorStatus.STARTED, clearing = clearing)
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
        httpActivities.clear(); socketStartedNanos = null
        account?.let(monitor::closeAccount); account = null; lanes.clear(); http = null; api = ApiNetworkState()
        socket = SocketNetworkState(); sync = SyncNetworkState(); media = MediaNetworkState(); peer = PeerNetworkState(); syncSpace = null; socketSpace = null; ignoredWatermarkSocket = null
        account = monitor.openAccount()
        account?.also { resume(it, epoch) }
    }
    @Synchronized fun closeAccount(owner: NetworkMonitor.Account?) = diagnostic(Unit) {
        if (owner != null && owner === account) {
            invalidateServerAction()
            activePeerTransfers.clear(); recentPeerTransfers.clear()
            httpActivities.clear(); socketStartedNanos = null
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
        httpActivities.clear()
        lanes.values.toList().forEach(monitor::disconnect); lanes.clear(); http = null
        api = api.copy(paused = true, active = 0)
        socket = socket.copy(status = MonitorStatus.PAUSED, opened = false, aligned = false, heartbeatMs = null, diagnosticsVersion = null, durationMs = socketElapsed())
        socketStartedNanos = null
        sync = sync.copy(paused = true, running = false)
        media = media.copy(active = 0, status = MonitorStatus.PAUSED)
        peer = peer.copy(registered = false, active = 0, status = MonitorStatus.PAUSED)
    }
    @Synchronized fun connect(owner: NetworkMonitor.Account, lane: MonitorLane): NetworkMonitor.Connection? = diagnostic(null) {
        if (account === owner && lane == MonitorLane.HTTP) { invalidateServerAction(); httpActivities.clear(); api = api.copy(active = 0) }
        if (account === owner && lane == MonitorLane.PEER) { activePeerTransfers.clear(); recentPeerTransfers.clear() }
        if (account !== owner) null else monitor.connect(owner, lane)?.also { lanes[lane] = it }
    }
    @Synchronized fun disconnect(connection: NetworkMonitor.Connection?) = diagnostic(Unit) {
        if (connection != null && lanes[connection.lane] === connection) {
            if (connection.lane == MonitorLane.HTTP) { invalidateServerAction(); httpActivities.clear(); api = api.copy(active = 0) }
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
        val now = System.nanoTime()
        val activities = httpActivities.map { (owner, state) -> state.copy(elapsedMs = ((now - owner.startedNanos) / 1_000_000).coerceAtLeast(0)) }
        NativeNetworkState(monitor.snapshot(), api, socket.copy(durationMs = socketElapsed()), sync, serverRead, media, peer,
            activePeerTransfers.values.toList() + recentPeerTransfers.toList(), activities, (api.active - activities.size).coerceAtLeast(0))
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
    private fun socketElapsed() = if (socket.opened) socketStartedNanos?.let { ((System.nanoTime() - it) / 1_000_000).coerceAtLeast(0) } else socket.durationMs
    @Synchronized internal fun socketState(connection: NetworkMonitor.Connection, state: SocketNetworkState, startedNanos: Long? = null) = diagnostic(Unit) {
        if (current(connection)) { socket = state; socketStartedNanos = if (state.opened) startedNanos else null }
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
    @Synchronized private fun startActivity(expected: Long, origin: HttpUrl, request: Request): HttpActivityOwner? = diagnostic(null) {
        val binding = http?.takeIf { it.epoch == expected && current(it.connection) } ?: return@diagnostic null
        val template = template(request, origin)
        if (template == MonitorTemplate.DIAGNOSTICS) return@diagnostic null
        val activity = HttpActivityOwner(binding.connection, System.nanoTime())
        api = api.copy(active = api.active + 1)
        if (httpActivities.size < 16) httpActivities[activity] = HttpActivityState(java.util.UUID.randomUUID().toString().replace("-", ""),
            template, MonitorStatus.STARTED, System.currentTimeMillis(), 0)
        activity
    }
    @Synchronized private fun finishActivity(activity: HttpActivityOwner?) = diagnostic(Unit) {
        if (activity != null && !activity.ended) {
            activity.ended = true; httpActivities.remove(activity)
            if (current(activity.connection)) api = api.copy(active = (api.active - 1).coerceAtLeast(0))
        }
    }
    @Synchronized private fun activityResponse(activity: HttpActivityOwner?, code: Int) = diagnostic(Unit) {
        if (activity != null && current(activity.connection)) httpActivities[activity]?.let {
            httpActivities[activity] = it.copy(status = MonitorStatus.TRANSFERRING, httpStatus = code.takeIf { value -> value in 100..599 })
        }
    }
    @Synchronized private fun startHttp(expected: Long, origin: HttpUrl, request: Request, parent: NetworkMonitor.Operation?, activity: HttpActivityOwner?): HttpObservation? = diagnostic(null) {
        val binding = http?.takeIf { it.epoch == expected && current(it.connection) } ?: return@diagnostic null
        val template = template(request, origin)
        if (template == MonitorTemplate.DIAGNOSTICS) return@diagnostic null
        val operation = monitor.begin(binding.connection, MonitorProtocol.HTTP, MonitorEvent.REQUEST, template, parent = parent) ?: return@diagnostic null
        activity?.let { httpActivities[it]?.let { state -> httpActivities[it] = state.copy(id = operation.id) } }
        HttpObservation(binding.connection, operation, template, activity)
    }

    fun <T> http(expected: Long, origin: HttpUrl, request: Request, authenticated: Boolean, parent: NetworkMonitor.Operation? = null,
                 business: (Request, HttpObservation?) -> T): T {
        val activity = startActivity(expected, origin, request)
        val observation = startHttp(expected, origin, request, parent, activity)
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
        } finally { abandon(observation?.operation); finishActivity(activity) }
    }
    inner class HttpObservation internal constructor(internal val connection: NetworkMonitor.Connection,
                                                    internal val operation: NetworkMonitor.Operation, internal val template: MonitorTemplate,
                                                    private val activity: HttpActivityOwner?) {
        private val started = System.nanoTime()
        private var status: Int? = null
        private var bytes = 0L
        private var failureCategory: MonitorError? = null
        fun failedAs(error: MonitorError) { failureCategory = error }
        fun response(code: Int) { status = code; activityResponse(activity, code); observe(operation, MonitorProtocol.HTTP, MonitorEvent.REQUEST,
            MonitorStatus.TRANSFERRING, MonitorValues(httpStatus = code)) }
        fun read(count: Int) { if (count > 0) bytes = if (Long.MAX_VALUE - bytes < count) Long.MAX_VALUE else bytes + count }
        internal fun finish(terminal: MonitorStatus, error: MonitorError) = diagnostic(Unit) {
            val actualError = if (error == MonitorError.CANCELLED) error else failureCategory ?: error
            observe(operation, MonitorProtocol.HTTP, MonitorEvent.REQUEST, terminal,
                MonitorValues(bytes = bytes, httpStatus = status, error = actualError))
            synchronized(this@NetworkDiagnostics) {
                if (!current(connection)) return@synchronized
                val now = System.currentTimeMillis()
                api = api.copy(lastDurationMs = ((System.nanoTime() - started) / 1_000_000).coerceAtLeast(0),
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
