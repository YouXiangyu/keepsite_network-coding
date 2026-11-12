package io.github.youxiangyu.keepsite.core

import java.io.Closeable
import java.util.concurrent.CancellationException

internal fun mediaFailure(failure: Throwable): MonitorError = when (failure) {
    is CancellationException -> MonitorError.CANCELLED
    is java.io.IOException -> diagnosticIoError(failure)
    is ApiFailure -> when (failure.status) { 401 -> MonitorError.AUTH_REQUIRED; 403 -> MonitorError.AUTH_DENIED; else -> MonitorError.PROTOCOL }
    else -> MonitorError.UNKNOWN
}

data class MediaNetworkState(val active: Int = 0, val status: MonitorStatus = MonitorStatus.UNKNOWN,
    val route: MonitorRoute = MonitorRoute.UNKNOWN, val bytes: Long? = null, val expectedBytes: Long? = null,
    val verified: Boolean? = null, val lastSuccessAt: Long? = null, val durationMs: Long? = null, val error: MonitorError = MonitorError.NONE)

/** One loadMedia call. Observer work is isolated; the actual validation block preserves its result/Throwable. */
class MediaObservation internal constructor(private val owner: NetworkDiagnostics, internal val connection: NetworkMonitor.Connection,
    internal val operation: NetworkMonitor.Operation, private val expectedBytes: Long) : Closeable {
    private val started = System.nanoTime()
    private var ended = false
    private var validated = false
    @Synchronized fun attempt(route: MonitorRoute, protocol: MonitorProtocol, event: MonitorEvent = MonitorEvent.MEDIA_ATTEMPT): MediaAttempt? = diagnostic(null) {
        if (ended) null else owner.begin(connection, protocol, event, MonitorTemplate.MEDIA, operation)?.let {
            MediaAttempt(owner, connection, it, route, protocol, event, expectedBytes)
        }
    }
    fun <T> validating(block: () -> T): T {
        val span = attempt(MonitorRoute.UNKNOWN, MonitorProtocol.MEDIA, MonitorEvent.VERIFICATION)
        try {
            val value = block()
            span?.complete(verified = true)
            diagnostic(Unit) { synchronized(this) { if (!ended) validated = true } }
            return value
        } catch (failure: Throwable) {
            val error = if (failure is CancellationException) MonitorError.CANCELLED else MonitorError.INTEGRITY
            span?.failed(error); failed(error)
            throw failure
        } finally { span?.close() }
    }
    @Synchronized fun cacheValidated() = diagnostic(Unit) { if (!ended) validated = true }
    @Synchronized fun complete(bytes: Long?, route: MonitorRoute) = diagnostic(Unit) {
        finish(if (validated) MonitorStatus.COMPLETED else MonitorStatus.FAILED, if (validated) MonitorError.NONE else MonitorError.INTEGRITY, bytes, route)
    }
    @Synchronized fun failed(error: MonitorError) = diagnostic(Unit) {
        finish(if (error == MonitorError.CANCELLED) MonitorStatus.CANCELLED else MonitorStatus.FAILED, error, null, MonitorRoute.UNKNOWN)
    }
    private fun finish(status: MonitorStatus, error: MonitorError, bytes: Long?, route: MonitorRoute) {
        if (ended) return
        ended = true
        val protocol = when (route) { MonitorRoute.CACHE -> MonitorProtocol.CACHE
            MonitorRoute.P2P_DIRECT, MonitorRoute.P2P_RELAY, MonitorRoute.P2P_UNKNOWN, MonitorRoute.MIXED -> MonitorProtocol.P2P
            MonitorRoute.DIRECT_S3 -> MonitorProtocol.S3; MonitorRoute.SERVER_PROXY -> MonitorProtocol.HTTP; else -> MonitorProtocol.MEDIA }
        try { owner.observe(operation, protocol, MonitorEvent.MEDIA_READ, status,
            MonitorValues(bytes = bytes, expectedBytes = expectedBytes, route = route, verified = validated, error = error)) }
        finally {
            owner.abandon(operation)
            owner.mediaFinished(connection, status, route, bytes, expectedBytes, validated, ((System.nanoTime() - started) / 1_000_000).coerceAtLeast(0), error)
        }
    }
    @Synchronized override fun close() { if (!ended) failed(MonitorError.CANCELLED); owner.abandon(operation) }
}

/** Cumulative application payload/control/discarded counts for one attempt, never declared ref.size. */
class MediaAttempt internal constructor(private val owner: NetworkDiagnostics, internal val connection: NetworkMonitor.Connection,
    internal val operation: NetworkMonitor.Operation, private var route: MonitorRoute, private val protocol: MonitorProtocol,
    private val event: MonitorEvent, private val expectedBytes: Long, private val outgoing: Boolean? = null) : Closeable {
    private var ended = false
    private var bytes: Long? = null
    private var control: Long? = null
    private var discarded: Long? = null
    private var verified: Boolean? = null
    private var resumed: Long? = null
    private var rtc = MonitorValues()
    private var rateNanos: Long? = null
    private var rateBytes: Long? = null
    private var bytesPerSecond: Double? = null
    @Synchronized fun read(count: Int) = diagnostic(Unit) { if (!ended && count >= 0) bytes = add(bytes, count) }
    @Synchronized fun control(count: Int) = diagnostic(Unit) { if (!ended && count >= 0) control = add(control, count) }
    @Synchronized fun discarded(count: Int) = diagnostic(Unit) { if (!ended && count >= 0) discarded = add(discarded, count) }
    @Synchronized fun verified() = diagnostic(Unit) { if (!ended) verified = true }
    @Synchronized internal fun payloadBytes(): Long? = bytes
    @Synchronized internal fun state(event: MonitorEvent, ice: MonitorRtcState? = null, pc: MonitorRtcState? = null, channelOpen: Boolean? = null) = diagnostic(Unit) {
        if (!ended) {
            rtc = rtc.copy(iceState = ice ?: rtc.iceState, pcState = pc ?: rtc.pcState, channelOpen = channelOpen ?: rtc.channelOpen)
            owner.observe(operation, protocol, event, if (channelOpen == true) MonitorStatus.OPEN else MonitorStatus.TRANSFERRING, values())
        }
    }
    @Synchronized internal fun sample(value: MonitorValues) = diagnostic(Unit) {
        if (!ended) {
            rtc = value.safe().copy(iceState = rtc.iceState, pcState = rtc.pcState, channelOpen = rtc.channelOpen)
            route = MonitorValues.mergeRoute(route, rtc.route)
            owner.observe(operation, protocol, MonitorEvent.STATS, MonitorStatus.TRANSFERRING, values())
        }
    }
    @Synchronized internal fun assembledRoute(value: MonitorRoute) = diagnostic(Unit) {
        if (!ended && value in setOf(MonitorRoute.P2P_DIRECT, MonitorRoute.P2P_RELAY, MonitorRoute.MIXED, MonitorRoute.P2P_UNKNOWN)) route = value
    }
    internal fun traceEligible() = diagnostic(false) { owner.monitor.traceContext(operation, MonitorDestination.AUTHENTICATED_API) != null }
    @Synchronized fun progress(buffered: Long? = null) = diagnostic(Unit) {
        if (!ended) {
            val now = System.nanoTime(); val current = bytes; val previous = rateBytes; val start = rateNanos
            bytesPerSecond = if (current != null && previous != null && start != null && current >= previous && now > start)
                ((current - previous).toDouble() * 1_000_000_000 / (now - start)).takeIf { it.isFinite() && it >= 0 } else null
            rateBytes = current; rateNanos = now
            owner.observe(operation, protocol, event, MonitorStatus.TRANSFERRING, values().copy(bufferedBytes = buffered))
        }
    }
    @Synchronized fun complete(verified: Boolean? = this.verified, actualIfUnknown: Long? = null, resumedBytes: Long? = null) = diagnostic(Unit) {
        if (ended) return@diagnostic
        if (bytes == null) bytes = actualIfUnknown
        this.verified = verified; resumed = resumedBytes
        finish(MonitorStatus.COMPLETED, MonitorError.NONE)
    }
    @Synchronized fun failed(error: MonitorError) = diagnostic(Unit) { if (!ended) finish(if (error == MonitorError.CANCELLED) MonitorStatus.CANCELLED else MonitorStatus.FAILED, error) }
    internal fun current() = diagnostic(false) { owner.monitor.operationCurrent(operation) }
    private fun values() = rtc.copy(bytes = bytes, expectedBytes = expectedBytes, controlBytes = control, discardedBytes = discarded,
        resumedBytes = resumed, verified = verified, route = route, outgoing = outgoing, bytesPerSecond = bytesPerSecond)
    private fun finish(status: MonitorStatus, error: MonitorError) {
        ended = true
        try { owner.observe(operation, protocol, event, status, values().copy(error = error)) }
        finally { owner.abandon(operation) }
    }
    @Synchronized override fun close() { if (!ended) failed(MonitorError.CANCELLED); owner.abandon(operation) }
    private fun add(previous: Long?, count: Int) = ((previous ?: 0).coerceAtMost(9_007_199_254_740_991L - count) + count)
}
