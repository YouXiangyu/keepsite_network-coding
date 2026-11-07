package io.github.youxiangyu.keepsite.core

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class NetworkMonitorTest {
    private class Clock : MonitorClock {
        override val id = "c".repeat(32)
        var wall = 1_800_000_000_000L
        var nano = 0L
        override fun wallMillis() = wall
        override fun nanos() = nano
        fun advance(ms: Long) { nano += ms * 1_000_000; wall += ms }
    }
    private val clock = Clock()
    private val monitor = NetworkMonitor(clock)
    private val account = monitor.openAccount()
    private val http = monitor.connect(account, MonitorLane.HTTP)!!
    private fun request(connection: NetworkMonitor.Connection = http) = monitor.begin(connection,
        MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorTemplate.MEDIA)!!
    private fun export() = JSONObject(monitor.exportJson(monitor.prepareExport(account)!!)!!)

    @Test fun defaultOffKeepsActualObservationWithoutClaimingConnectionOrCompletedTransfer() {
        val operation = request()
        clock.advance(31)
        monitor.observe(operation, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorStatus.FAILED,
            MonitorValues(httpStatus = 401, error = MonitorError.AUTH_REQUIRED))
        val state = monitor.snapshot()
        assertFalse(state.detailed); assertTrue(state.events.isEmpty()); assertEquals(0, state.active)
        val last = state.latest.getValue(MonitorProtocol.HTTP)
        assertEquals(31L, last.durationMs); assertEquals(401, last.values.httpStatus)
        assertEquals(MonitorStatus.FAILED, last.status); assertNull(last.values.verified)
        assertEquals(MonitorRoute.UNKNOWN, last.values.route)
        assertNull(monitor.traceContext(operation, MonitorDestination.AUTHENTICATED_API))
        val peer = monitor.connect(account, MonitorLane.PEER)!!
        val channel = monitor.begin(peer, MonitorProtocol.P2P, MonitorEvent.CHANNEL)!!
        monitor.observe(channel, MonitorProtocol.P2P, MonitorEvent.CHANNEL, MonitorStatus.OPEN)
        assertEquals(MonitorStatus.OPEN, monitor.snapshot().latest.getValue(MonitorProtocol.P2P).status)
        assertNull(monitor.snapshot().latest.getValue(MonitorProtocol.P2P).values.verified)
    }

    @Test fun traceHeadersAreOptInCurrentAccountAndAuthenticatedSameOriginOnly() {
        monitor.setDetailed(account, true)
        val operation = request()
        val context = monitor.traceContext(operation, MonitorDestination.AUTHENTICATED_API)!!
        assertTrue(context.captureId.matches(Regex("[0-9a-f]{32}")))
        assertTrue(context.traceId.matches(Regex("[0-9a-f]{32}")))
        for (destination in listOf(MonitorDestination.EXTERNAL, MonitorDestination.ANONYMOUS_API, MonitorDestination.DIAGNOSTICS))
            assertNull(monitor.traceContext(operation, destination))
        val diagnostic = monitor.begin(http, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorTemplate.DIAGNOSTICS)!!
        assertNull(monitor.traceContext(diagnostic, MonitorDestination.AUTHENTICATED_API))
        monitor.observe(operation, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorStatus.COMPLETED)
        assertTrue(monitor.knowsTrace(account, context.captureId, context.traceId))
        monitor.setDetailed(account, false)
        assertFalse(monitor.knowsTrace(account, context.captureId, context.traceId))
        monitor.setDetailed(account, true)
        assertNotEquals(context.captureId, monitor.traceContext(request(), MonitorDestination.AUTHENTICATED_API)!!.captureId)
    }

    @Test fun privatePeerIdentifiersAndInvalidNumbersCannotLeakThroughExport() {
        monitor.setDetailed(account, true)
        val canary = "private@example.test?token=SECRET&sdp=10.2.3.4"
        assertNull(monitor.begin(http, MonitorProtocol.HTTP, MonitorEvent.REQUEST,
            device = NetworkMonitor.DeviceAlias(account, canary)))
        val alias = monitor.alias(account, canary)!!
        val operation = monitor.begin(http, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorTemplate.DOWNLOAD, alias)!!
        monitor.observe(operation, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorStatus.COMPLETED,
            MonitorValues(bytes = -1, expectedBytes = Long.MAX_VALUE, httpStatus = 999, closeCode = 42,
                localCursor = -1, rttMs = Double.NaN, bytesPerSecond = Double.POSITIVE_INFINITY))
        val json = export()
        val text = json.toString()
        for (secret in listOf(canary, "SECRET", "private@", "10.2.3.4", "token=", "sdp=")) assertFalse(text.contains(secret))
        val values = json.getJSONArray("events").getJSONObject(1).getJSONObject("values")
        for (key in listOf("bytes", "expectedBytes", "httpStatus", "closeCode", "localCursor", "rttMs", "bytesPerSecond"))
            assertTrue("$key must be unknown", values.isNull(key))
        assertEquals("/api/media/:asset/download", json.getJSONArray("events").getJSONObject(1).getString("routeTemplate"))
        assertEquals(alias.value, monitor.alias(account, canary)!!.value)
        assertNull(monitor.alias(account, "x".repeat(129)))
    }

    @Test fun clearDoesNotLetOldCallbacksOrChildWorkRecreateClearedHistory() {
        monitor.setDetailed(account, true)
        val old = request()
        val oldContext = monitor.traceContext(old, MonitorDestination.AUTHENTICATED_API)!!
        val pendingExport = monitor.prepareExport(account)!!
        monitor.clearHistory(account)
        assertNull(monitor.exportJson(pendingExport))
        assertTrue(monitor.observe(old, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorStatus.COMPLETED, MonitorValues(bytes = 77)))
        assertEquals(77L, monitor.snapshot().latest.getValue(MonitorProtocol.HTTP).values.bytes)
        assertTrue(monitor.snapshot().events.isEmpty()); assertEquals(0, monitor.snapshot().completedTraces)
        assertFalse(monitor.knowsTrace(account, oldContext.captureId, oldContext.traceId))
        assertNull(monitor.begin(http, MonitorProtocol.HTTP, MonitorEvent.REQUEST, parent = old))
        val fresh = request()
        assertNotEquals(oldContext.captureId, monitor.traceContext(fresh, MonitorDestination.AUTHENTICATED_API)!!.captureId)
        assertEquals(1, monitor.snapshot().events.size)
    }

    @Test fun accountAndConnectionOwnersAreIndependentAndLateCallbacksCannotAffectNewOwners() {
        monitor.setDetailed(account, true)
        val peerConnection = monitor.connect(account, MonitorLane.PEER)!!
        val ice = monitor.begin(peerConnection, MonitorProtocol.P2P, MonitorEvent.ICE)!!
        val pendingHttp = request()
        val peerReplacement = monitor.connect(account, MonitorLane.PEER)!!
        assertFalse(monitor.observe(ice, MonitorProtocol.P2P, MonitorEvent.ICE, MonitorStatus.OPEN))
        assertTrue(monitor.observe(pendingHttp, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorStatus.COMPLETED))
        monitor.disconnect(peerConnection)
        assertNotNull(monitor.begin(peerReplacement, MonitorProtocol.P2P, MonitorEvent.ICE))
        val pendingExport = monitor.prepareExport(account)!!
        val peerAlias = monitor.alias(account, "peer-old")!!
        val replacement = monitor.openAccount()
        monitor.closeAccount(account)
        assertEquals(replacement.sessionId, monitor.snapshot().sessionId)
        assertTrue(monitor.snapshot().events.isEmpty()); assertTrue(monitor.snapshot().latest.isEmpty())
        assertEquals(0, monitor.snapshot().aliases); assertFalse(monitor.snapshot().detailed)
        assertNull(monitor.exportJson(pendingExport)); assertNull(monitor.connect(account, MonitorLane.HTTP))
        val newHttp = monitor.connect(replacement, MonitorLane.HTTP)!!
        assertNull(monitor.begin(newHttp, MonitorProtocol.HTTP, MonitorEvent.REQUEST, device = peerAlias))
    }

    @Test fun pauseFreezesHistoryAndHeadersWhileLatestObservationsContinue() {
        monitor.setDetailed(account, true)
        val operation = request()
        val size = monitor.snapshot().events.size
        monitor.pauseHistory(account, true)
        clock.advance(8)
        monitor.observe(operation, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorStatus.TRANSFERRING, MonitorValues(bytes = 5))
        assertEquals(size, monitor.snapshot().events.size)
        assertEquals(5L, monitor.snapshot().latest.getValue(MonitorProtocol.HTTP).values.bytes)
        assertNull(monitor.traceContext(operation, MonitorDestination.AUTHENTICATED_API))
        monitor.pauseHistory(account, false)
        monitor.observe(operation, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorStatus.COMPLETED)
        assertEquals(size, monitor.snapshot().events.size)
        request()
        assertEquals(size + 1, monitor.snapshot().events.size)
    }

    @Test fun activityHistoryAliasesAndCompletedIndexesAllHaveHardBounds() {
        monitor.setDetailed(account, true)
        repeat(100) { monitor.alias(account, "peer-$it") }
        assertEquals(NetworkMonitor.MAX_SLOTS, monitor.snapshot().aliases)
        val active = (1..NetworkMonitor.MAX_SLOTS).map { request() }
        assertNull(monitor.begin(http, MonitorProtocol.HTTP, MonitorEvent.REQUEST))
        assertEquals(1L, monitor.snapshot().droppedOperations)
        active.forEach { monitor.observe(it, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorStatus.COMPLETED) }
        repeat(300) {
            val operation = request()
            monitor.observe(operation, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorStatus.COMPLETED,
                MonitorValues(bytes = 1234, expectedBytes = 1234, verified = true))
        }
        val state = monitor.snapshot()
        assertTrue(state.events.size <= NetworkMonitor.MAX_EVENTS)
        assertTrue(state.historyBytes <= NetworkMonitor.MAX_HISTORY_BYTES)
        assertTrue(state.completedTraces <= NetworkMonitor.MAX_SLOTS); assertEquals(0, state.active)
        assertTrue(state.dropped > 0); assertEquals(664L, state.recorded)
        val events = export().getJSONArray("events")
        for (index in 0 until events.length()) assertTrue(events.getJSONObject(index).toString().toByteArray().size <= NetworkMonitor.MAX_EVENT_BYTES)
    }

    @Test fun sharedMediaTraceDoesNotGuessByAssetAndExpiresWithCapture() {
        monitor.setDetailed(account, true)
        val mediaConnection = monitor.connect(account, MonitorLane.MEDIA)!!
        val media = monitor.begin(mediaConnection, MonitorProtocol.CACHE, MonitorEvent.MEDIA_READ)!!
        val child = monitor.begin(http, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorTemplate.DOWNLOAD, parent = media)!!
        val context = monitor.traceContext(child, MonitorDestination.AUTHENTICATED_API)!!
        assertEquals(media.traceId, context.traceId); assertNotEquals(media.id, child.id)
        monitor.observe(child, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorStatus.COMPLETED)
        assertTrue(monitor.knowsTrace(account, context.captureId, context.traceId))
        monitor.clearHistory(account)
        assertNull(monitor.begin(http, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorTemplate.MEDIA, parent = media))
        assertFalse(monitor.knowsTrace(account, context.captureId, context.traceId))
    }

    @Test fun selectedPairRequiresBothValidatedCandidateTypesAndPreservesObservedChanges() {
        assertEquals(MonitorRoute.P2P_UNKNOWN, MonitorValues.selectedRoute(null, "relay"))
        assertEquals(MonitorRoute.P2P_UNKNOWN, MonitorValues.selectedRoute("10.0.0.1", "host"))
        assertEquals(MonitorRoute.P2P_DIRECT, MonitorValues.selectedRoute("host", "srflx"))
        assertEquals(MonitorRoute.P2P_RELAY, MonitorValues.selectedRoute("srflx", "relay"))
        assertEquals(MonitorRoute.MIXED, MonitorValues.mergeRoute(MonitorRoute.P2P_DIRECT, MonitorRoute.P2P_RELAY))
        assertEquals(MonitorRoute.P2P_DIRECT, MonitorValues.mergeRoute(MonitorRoute.P2P_DIRECT, MonitorRoute.P2P_UNKNOWN))
    }

    @Test fun manualServerReadNeedsCurrentHttpInstanceAndRejectsClearOffOrAccountChanges() {
        assertNull(monitor.prepareServerRead(http))
        monitor.setDetailed(account, true)
        val old = monitor.prepareServerRead(http)!!
        val replacement = monitor.connect(account, MonitorLane.HTTP)!!
        assertFalse(monitor.serverReadCurrent(old))
        val current = monitor.prepareServerRead(replacement)!!
        assertTrue(monitor.serverReadCurrent(current))
        monitor.clearHistory(account)
        assertFalse(monitor.serverReadCurrent(current))
        assertNotEquals(current.captureId, monitor.prepareServerRead(replacement)!!.captureId)
        val peer = monitor.connect(account, MonitorLane.PEER)!!
        assertNull(monitor.prepareServerRead(peer))
        val beforePause = monitor.prepareServerRead(replacement)!!
        monitor.pauseHistory(account, true)
        assertFalse(monitor.serverReadCurrent(beforePause)); assertNull(monitor.prepareServerRead(replacement))
        monitor.pauseHistory(account, false)
        val beforeOff = monitor.prepareServerRead(replacement)!!
        monitor.setDetailed(account, false)
        assertFalse(monitor.serverReadCurrent(beforeOff)); assertNull(monitor.prepareServerRead(replacement))
        monitor.openAccount()
        assertFalse(monitor.serverReadCurrent(beforeOff))
    }

    @Test fun onlyCollectorPreparedExportsCanSerializeState() {
        monitor.setDetailed(account, true)
        request()
        val actual = monitor.prepareExport(account)!!
        val forged = NetworkMonitor.Export(Any(), account, actual.history,
            monitor.snapshot().copy(sessionId = "private-token", clockId = "private@example.test"))
        assertNull(actual.contents(Any()))
        assertFalse(monitor.exportCurrent(forged)); assertNull(monitor.exportJson(forged))
        assertTrue(monitor.exportCurrent(actual)); assertNotNull(monitor.exportJson(actual))
    }
}
