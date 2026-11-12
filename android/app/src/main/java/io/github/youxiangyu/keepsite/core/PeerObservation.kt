package io.github.youxiangyu.keepsite.core

import java.io.Closeable

data class PeerNetworkState(val enabled: Boolean? = null, val registered: Boolean = false, val discovered: Int? = null,
    val active: Int = 0, val status: MonitorStatus = MonitorStatus.UNKNOWN)

class PeerObservation internal constructor(private val owner: NetworkDiagnostics, internal val connection: NetworkMonitor.Connection,
    private val account: NetworkMonitor.Account, private val epoch: Long) : Closeable {
    internal fun command(kind: String, generation: Long, parent: NetworkMonitor.Operation? = null, ice: Boolean = false): PeerCommand? = diagnostic(null) {
        if (!owner.currentPeer(connection, epoch)) return@diagnostic null
        val event = when (kind) {
            "device.register" -> MonitorEvent.REGISTRATION
            "device.heartbeat", "device.list", "device.unregister", "cache.replace", "cache.patch" -> MonitorEvent.MESSAGE
            "peer.find" -> MonitorEvent.DISCOVERY
            "peer.open", "peer.accept", "peer.renew", "peer.cancel" -> MonitorEvent.SIGNAL
            "peer.signal" -> if (ice) MonitorEvent.ICE else MonitorEvent.SIGNAL
            else -> return@diagnostic null
        }
        owner.begin(connection, MonitorProtocol.P2P, event, MonitorTemplate.EVENTS, parent)?.let { PeerCommand(owner, connection, it, epoch, generation, event) }
    }
    internal fun transfer(parent: MediaAttempt?, peerKey: String, expectedBytes: Long, outgoing: Boolean): MediaAttempt? = diagnostic(null) {
        if (!owner.currentPeer(connection, epoch)) return@diagnostic null
        val alias = owner.monitor.alias(account, peerKey) ?: return@diagnostic null
        owner.begin(connection, MonitorProtocol.P2P, MonitorEvent.MEDIA_ATTEMPT, MonitorTemplate.MEDIA, parent?.operation, alias)?.let {
            MediaAttempt(owner, connection, it, MonitorRoute.P2P_UNKNOWN, MonitorProtocol.P2P, MonitorEvent.MEDIA_ATTEMPT, expectedBytes, outgoing)
        }
    }
    internal fun live(peerKey: String, expectedBytes: Long, outgoing: Boolean, operationId: String?): PeerTransferLive? = diagnostic(null) {
        if (!owner.currentPeer(connection, epoch)) return@diagnostic null
        val alias = owner.monitor.alias(account, peerKey) ?: return@diagnostic null
        owner.beginPeerTransfer(connection, alias.value, expectedBytes, outgoing, operationId, epoch)
    }
    internal fun event(event: MonitorEvent, status: MonitorStatus, values: MonitorValues = MonitorValues()) = diagnostic(Unit) {
        if (!owner.currentPeer(connection, epoch)) return@diagnostic
        val operation = owner.begin(connection, MonitorProtocol.P2P, event, MonitorTemplate.EVENTS) ?: return@diagnostic
        try { owner.observe(operation, MonitorProtocol.P2P, event, status, values) } finally { owner.abandon(operation) }
    }
    internal fun state(enabled: Boolean, registered: Boolean, discovered: Int, active: Int) = diagnostic(Unit) {
        owner.peerState(connection, PeerNetworkState(enabled, registered, discovered.coerceIn(0, 8), active.coerceIn(0, 2),
            if (registered) MonitorStatus.OPEN else if (enabled) MonitorStatus.STARTED else MonitorStatus.PAUSED), epoch)
    }
    override fun close() = diagnostic(Unit) {
        owner.peerState(connection, PeerNetworkState(status = MonitorStatus.CLOSED), epoch); owner.disconnect(connection)
    }
}
