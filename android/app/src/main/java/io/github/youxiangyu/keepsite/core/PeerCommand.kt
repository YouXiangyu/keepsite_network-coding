package io.github.youxiangyu.keepsite.core

import io.github.youxiangyu.keepsite.peer.EventState
import java.io.Closeable

/** A real command's explicit owner, never the account's most recent trace. No message/body is retained. */
class PeerCommand internal constructor(private val owner: NetworkDiagnostics, internal val connection: NetworkMonitor.Connection,
    internal val operation: NetworkMonitor.Operation, internal val epoch: Long, internal val generation: Long, private val event: MonitorEvent) : Closeable {
    private var ended = false
    private var controlBytes: Long? = null
    @Synchronized internal fun matchesSender(diagnostics: NetworkDiagnostics?, state: EventState) = diagnostic(false) {
        !ended && owner === diagnostics && state.connected && state.generation == generation && owner.currentPeer(connection, epoch) && owner.socketCurrent(generation)
    }
    @Synchronized internal fun context(diagnostics: NetworkDiagnostics?, state: EventState): MonitorTraceContext? = diagnostic(null) {
        if (ended || owner !== diagnostics || !state.connected || state.diagnosticsVersion != 1 || state.generation != generation) null
        else owner.peerContext(connection, operation, epoch, generation)
    }
    @Synchronized internal fun sent(bytes: Int, accepted: Boolean) = diagnostic(Unit) {
        if (!ended) {
            if (!owner.currentPeer(connection, epoch)) { ended = true; owner.abandon(operation); return@diagnostic }
            controlBytes = if (accepted) bytes.toLong() else 0
            if (accepted) owner.observe(operation, MonitorProtocol.P2P, event, MonitorStatus.TRANSFERRING, MonitorValues(controlBytes = controlBytes))
            else finish(MonitorStatus.FAILED, MonitorError.UNAVAILABLE)
        }
    }
    @Synchronized internal fun complete() = diagnostic(Unit) { if (!ended) finish(MonitorStatus.COMPLETED, MonitorError.NONE) }
    @Synchronized internal fun failed(error: MonitorError) = diagnostic(Unit) { if (!ended) finish(if (error == MonitorError.CANCELLED) MonitorStatus.CANCELLED else MonitorStatus.FAILED, error) }
    private fun finish(status: MonitorStatus, error: MonitorError) {
        ended = true
        try { if (owner.currentPeer(connection, epoch)) owner.observe(operation, MonitorProtocol.P2P, event, status, MonitorValues(controlBytes = controlBytes, error = error)) }
        finally { owner.abandon(operation) }
    }
    @Synchronized override fun close() { if (!ended) failed(MonitorError.CANCELLED); owner.abandon(operation) }
}
