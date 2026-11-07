package io.github.youxiangyu.keepsite.core

import org.json.JSONObject
import java.util.concurrent.CancellationException

enum class SocketMessage { UNKNOWN, SYNC_READY, SYNC_HINT, DEVICE_REGISTERED, DEVICE_ALIVE, DEVICE_PEERS, PEER_SIGNAL, PEER_CONTROL }
data class SocketNetworkState(val status: MonitorStatus = MonitorStatus.UNKNOWN, val opened: Boolean = false,
    val aligned: Boolean = false, val connectedSince: Long? = null, val lastMessageAt: Long? = null,
    val lastMessage: SocketMessage = SocketMessage.UNKNOWN, val intervalMs: Long? = null,
    val heartbeatMs: Long? = null, val diagnosticsVersion: Int? = null, val watchdogMs: Long? = null,
    val retryDelayMs: Long? = null, val closeCode: Int? = null, val error: MonitorError = MonitorError.NONE)
/** Item counts are snapshot rows and delta operations, separately; data-change time means nonempty data applied, not a content diff. */
data class SyncNetworkState(val paused: Boolean = true, val running: Boolean = false, val localCursor: Long? = null,
    val observedCursor: Long? = null, val lastSnapshotAt: Long? = null, val lastDeltaAt: Long? = null,
    val lastDataChangeAt: Long? = null, val lastSnapshotItems: Long? = null, val lastDeltaItems: Long? = null, val durationMs: Long? = null,
    val error: MonitorError = MonitorError.NONE) {
    val pending: Boolean? get() = if (running) true else localCursor?.let { local -> observedCursor?.let { it > local } }
}
data class SocketCapabilities(val heartbeatMs: Long? = null, val diagnosticsVersion: Int? = null) {
    companion object {
        fun read(value: JSONObject): SocketCapabilities = diagnostic(SocketCapabilities()) {
            val heartbeat = (value.opt("heartbeatMs") as? Number)?.toDouble()?.takeIf {
                it.isFinite() && it >= 1 && it <= 9_007_199_254_740_991.0 && it % 1.0 == 0.0 }?.toLong()
            val version = (value.opt("diagnosticsVersion") as? Number)?.toDouble()?.takeIf { it == 1.0 }?.toInt()
            SocketCapabilities(heartbeat, version)
        }
    }
}

class SocketObservation internal constructor(private val owner: NetworkDiagnostics, private val connection: NetworkMonitor.Connection) {
    private val operation = owner.begin(connection, MonitorProtocol.WS, MonitorEvent.CONNECTION, MonitorTemplate.EVENTS)
    private var state = SocketNetworkState(status = MonitorStatus.STARTED, watchdogMs = 8000)
    private var messageNanos: Long? = null
    init { owner.socketState(connection, state) }
    private fun publish(event: MonitorEvent = MonitorEvent.CONNECTION, bytes: Long? = null) {
        owner.observe(operation, MonitorProtocol.WS, event, state.status,
            MonitorValues(bytes = bytes, heartbeatMs = state.heartbeatMs, retryDelayMs = state.retryDelayMs, closeCode = state.closeCode, error = state.error))
        owner.socketState(connection, state)
    }
    fun opened() = diagnostic(Unit) {
        state = state.copy(status = MonitorStatus.OPEN, opened = true, connectedSince = System.currentTimeMillis()); publish()
    }
    fun aligned(aligned: Boolean) = diagnostic(Unit) {
        if (state.aligned != aligned) { state = state.copy(aligned = aligned); publish() }
    }
    fun received(type: String, bytes: Int, capabilities: SocketCapabilities? = null, server: String? = null, cursor: Long? = null) = diagnostic(Unit) {
        if (!owner.current(connection)) return@diagnostic
        val now = System.nanoTime()
        val message = when (type) {
            "sync.ready" -> SocketMessage.SYNC_READY; "sync.hint" -> SocketMessage.SYNC_HINT
            "device.registered" -> SocketMessage.DEVICE_REGISTERED; "device.alive" -> SocketMessage.DEVICE_ALIVE
            "device.peers" -> SocketMessage.DEVICE_PEERS; "peer.signal" -> SocketMessage.PEER_SIGNAL
            in setOf("device.unregistered", "cache.ack", "peer.sources", "peer.pending", "peer.request", "peer.authorized", "peer.signalled", "peer.lease", "peer.cancelled", "peer.error") -> SocketMessage.PEER_CONTROL
            else -> SocketMessage.UNKNOWN
        }
        state = state.copy(lastMessageAt = System.currentTimeMillis(), lastMessage = message,
            intervalMs = messageNanos?.let { ((now - it) / 1_000_000).coerceAtLeast(0) },
            heartbeatMs = capabilities?.heartbeatMs ?: if (capabilities == null) state.heartbeatMs else null,
            diagnosticsVersion = capabilities?.diagnosticsVersion ?: if (capabilities == null) state.diagnosticsVersion else null,
            watchdogMs = if (capabilities != null) 45000 else state.watchdogMs)
        messageNanos = now
        val messageOperation = owner.begin(connection, MonitorProtocol.WS, MonitorEvent.MESSAGE, MonitorTemplate.EVENTS)
        owner.observe(messageOperation, MonitorProtocol.WS, MonitorEvent.MESSAGE, MonitorStatus.COMPLETED,
            MonitorValues(bytes = bytes.toLong(), heartbeatMs = state.heartbeatMs, observedCursor = cursor))
        owner.abandon(messageOperation); owner.socketState(connection, state)
        if (server != null && cursor != null) owner.watermark(connection, server, cursor)
    }
    fun closed(code: Int? = null, retryMs: Long? = null, error: MonitorError = MonitorError.NONE) = diagnostic(Unit) {
        state = state.copy(status = if (retryMs != null) MonitorStatus.RETRYING else MonitorStatus.CLOSED,
            opened = false, aligned = false, heartbeatMs = null, diagnosticsVersion = null, watchdogMs = null,
            retryDelayMs = retryMs, closeCode = code?.takeIf { it in 1000..4999 }, error = error)
        publish(); owner.abandon(operation); owner.disconnect(connection)
    }
}

class SyncObservation internal constructor(private val owner: NetworkDiagnostics, private val connection: NetworkMonitor.Connection,
                                           internal val operation: NetworkMonitor.Operation) {
    private val started = System.nanoTime()
    private var ended = false
    init { owner.syncStarted(connection) }
    fun watermark(server: String, cursor: Long) = diagnostic(Unit) { if (!ended) owner.watermark(connection, server, cursor) }
    fun space(server: String) = diagnostic(Unit) { if (!ended) owner.syncSpace(connection, server) }
    fun rebuilding() = diagnostic(Unit) { if (!ended) owner.syncRebuilding(connection) }
    fun retry() { if (!ended) owner.observe(operation, MonitorProtocol.SYNC, MonitorEvent.DELTA, MonitorStatus.RETRYING, MonitorValues(error = MonitorError.PROTOCOL)) }
    fun applied(value: Synced, snapshotItems: Long?, changes: Long) = diagnostic(Unit) {
        if (ended) return@diagnostic
        ended = true
        owner.observe(operation, MonitorProtocol.SYNC, if (snapshotItems != null) MonitorEvent.SNAPSHOT else MonitorEvent.DELTA,
            MonitorStatus.COMPLETED, MonitorValues(localCursor = value.cursor, items = snapshotItems ?: changes))
        owner.syncApplied(connection, value, snapshotItems, changes, ((System.nanoTime() - started) / 1_000_000).coerceAtLeast(0))
    }
    fun failed(failure: Throwable) = diagnostic(Unit) {
        if (ended) return@diagnostic
        ended = true
        val error = if (failure is CancellationException) MonitorError.CANCELLED else if (failure is ApiFailure && failure.status == 401) MonitorError.AUTH_REQUIRED else MonitorError.PROTOCOL
        owner.observe(operation, MonitorProtocol.SYNC, MonitorEvent.DELTA,
            if (failure is CancellationException) MonitorStatus.CANCELLED else MonitorStatus.FAILED, MonitorValues(error = error))
        owner.syncFailed(connection, error)
    }
    fun close() { ended = true; owner.abandon(operation) }
}
