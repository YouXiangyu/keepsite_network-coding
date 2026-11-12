package io.github.youxiangyu.keepsite.ui

import io.github.youxiangyu.keepsite.core.*
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class NetworkPresentationTest {
    private fun row(key: String, clock: String = "client", trace: String? = "trace", protocol: String = "http", operation: String = "op", device: String? = null) =
        NetworkEventRow(key, clock, if (clock == "client") "client" else "server", "capture", 1, operation, trace, null, protocol,
            "request", "completed", device, 1, 20.0, 10.0, 5.0, 0, null, true)
    @Test fun filtersOnlyChooseTheListAndExpansionUsesTheCompleteTraceAcrossClocks() {
        val a = row("a", operation = "local", device = "device")
        val b = row("b", "server", protocol = "sql", operation = "server-span")
        val other = row("c", trace = "other")
        val all = listOf(a, b, other)
        val selected = NetworkPresentation.filtered(all, NetworkFilter(protocol = "http", device = "device", operation = "local"))
        assertEquals(listOf(a), selected)
        assertEquals(listOf(a, b), NetworkPresentation.expanded(all, selected.single()))
        assertEquals(2, NetworkPresentation.lanes(NetworkPresentation.expanded(all, a)).size)
    }
    @Test fun coalescedOrMissingTraceNeverGuessesParents() {
        val a = row("a", trace = null).copy(coalesced = true, linked = false)
        val b = row("b", trace = null)
        assertEquals(listOf(a), NetworkPresentation.expanded(listOf(a, b), a))
        assertNull(NetworkPresentation.interval(a.copy(start = null)))
        assertNull(NetworkPresentation.interval(a.copy(duration = 1000.0)))
        assertEquals(10.0 to 15.0, NetworkPresentation.interval(a))
    }
    @Test fun timeWindowIsRelativeToEachIndependentClockRatherThanCrossClockOffsets() {
        val local = row("local").copy(offset = 100000.0)
        val old = row("old").copy(offset = 1.0)
        val server = row("server", "server").copy(offset = 5.0)
        assertEquals(listOf(local, server), NetworkPresentation.filtered(listOf(local, old, server), NetworkFilter(seconds = 60)))
    }
    @Test fun sameClockAndSequenceAcrossCapturesKeepUnrelatedCoalescedRowsSeparate() {
        val d = NetworkDiagnostics(); d.openAccount(3)
        val event = ServerTraceEvent(1, 1, 10.0, null, 0.0, ServerTraceEventType.REDIS_PUBSUB_RECEIVE,
            ServerTraceStatus.COMPLETED, null, "1".repeat(32), null, null, null, null, true)
        fun domain(capture: String) = ServerTraceDomain(capture, "shared-clock", 1, 0, 1000,
            setOf(MonitorProtocol.REDIS), ServerTraceMetrics(1, 0, 0.0), listOf(event))
        val state = d.snapshot()!!
        val rows = NetworkPresentation.rows(state.copy(observations = state.observations.copy(serverDomains = listOf(domain("first"), domain("second")))))
        assertEquals(2, rows.map { it.key }.distinct().size)
        assertEquals(2, rows.map { it.group }.distinct().size)
        assertEquals(listOf(rows.first()), NetworkPresentation.expanded(rows, rows.first()))
    }
    @Test fun zeroUnknownMixedAndLogicalMediaLabelsRemainDistinct() {
        assertEquals("0 B", NetworkPresentation.bytes(0)); assertEquals("未知", NetworkPresentation.bytes(null))
        assertTrue(NetworkPresentation.millis(0).contains("0.0")); assertEquals("未知", NetworkPresentation.millis(null))
        assertEquals("媒体操作", NetworkPresentation.protocol("media")); assertEquals("混合路线", NetworkPresentation.route(MonitorRoute.MIXED))
        assertTrue(NetworkPresentation.route(MonitorRoute.P2P_UNKNOWN).contains("未知"))
    }
    @Test fun reportStatisticsUseUniqueVerifiedParentTerminalsNotAttemptsOrProgress() {
        val d = NetworkDiagnostics(); val account = d.openAccount(3)!!; d.setDetailed(true)
        val page = d.openPage()!!
        val first = d.media(account, 3, 4)!!; val child = first.attempt(MonitorRoute.CACHE, MonitorProtocol.CACHE)!!
        child.read(4); child.complete(true); first.validating { Unit }; first.complete(4, MonitorRoute.CACHE)
        val second = d.media(account, 3, 8)!!; second.failed(MonitorError.INTEGRITY)
        val report = JSONObject(d.prepareReport(page, NetworkReportEnvironment("https://keepsite.space", "1", 35))!!.render(NetworkReportFormat.JSON)!!)
        val stats = report.getJSONObject("statistics")
        assertEquals(1, stats.getInt("verifiedMediaSamples")); assertEquals(4, stats.getInt("verifiedLogicalBytes")); assertEquals(1, stats.getInt("durationSamples"))
        assertFalse(stats.isNull("p50MediaDurationMs"))
        assertEquals("aggregation", report.getJSONObject("scope").keys().asSequence().first { it == "aggregation" })
        assertNull(d.prepareReport(page, NetworkReportEnvironment("https://keepsite.space/?secret=PRIVATE", "1", 35))!!.render(NetworkReportFormat.JSON))
    }
}
