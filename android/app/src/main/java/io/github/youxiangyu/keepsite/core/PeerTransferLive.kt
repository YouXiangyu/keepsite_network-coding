package io.github.youxiangyu.keepsite.core

/** Lightweight facts, independent of detailed history. Identifiers are collector-minted session aliases. */
data class PeerTransferState(val id: String, val device: String, val outgoing: Boolean, val status: MonitorStatus,
    val values: MonitorValues, val elapsedMs: Long, val updatedAt: Long, val statsAt: Long?)

class PeerTransferLive internal constructor(private val owner: NetworkDiagnostics, internal val connection: NetworkMonitor.Connection,
    internal val id: String, private val device: String, private val outgoing: Boolean, private val expectedBytes: Long, internal val epoch: Long) {
    private val started = System.nanoTime()
    private var ended = false
    private var rateAt = started
    private var rateBytes = 0L
    private var rate: Double? = null
    @Synchronized internal fun publish(values: MonitorValues, status: MonitorStatus, statsAt: Long? = null, terminal: Boolean = false) = diagnostic(Unit) {
        if (ended) return@diagnostic
        val now = System.nanoTime(); val bytes = values.bytes
        if (bytes != null && now - rateAt >= 100_000_000) {
            rate = if (bytes >= rateBytes) ((bytes - rateBytes).toDouble() * 1_000_000_000 / (now - rateAt)).takeIf { it.isFinite() && it >= 0 } else null
            rateAt = now; rateBytes = bytes
        }
        if (terminal) ended = true
        owner.peerTransferState(this, PeerTransferState(id, device, outgoing, status,
            values.copy(expectedBytes = expectedBytes, outgoing = outgoing, bytesPerSecond = rate).safe(),
            ((now - started) / 1_000_000).coerceAtLeast(0), System.currentTimeMillis(), statsAt), terminal)
    }
}
