package io.github.youxiangyu.keepsite.core

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

/** Shared 44-event isolated R3 TestClient/SQL/FakeRedis/worker/FakeS3 fixture, never product data. */
class ServerTraceTest {
    private fun fixture() = javaClass.getResourceAsStream("/diagnostics/server-r3-isolated-trace.json")!!.use { JSONObject(it.readBytes().toString(Charsets.UTF_8)) }
    private fun parse(value: JSONObject) = ServerTraceBatch.parse(value.toString().toByteArray(), value.getString("captureId"))
    private fun batch(capture: String, count: Int = 44, clock: String? = null): ServerTraceBatch {
        val root = fixture().put("captureId", capture)
        if (clock != null) root.put("clockId", clock)
        if (count != 44) {
            val row = root.getJSONArray("events").getJSONObject(0).toString()
            root.put("events", JSONArray((1..count).map { JSONObject(row).put("sequence", it) })).put("next", count)
            root.getJSONObject("metrics").put("recorded", count)
        }
        return parse(root)!!
    }
    private fun ready(): Triple<NetworkMonitor, NetworkMonitor.Account, NetworkMonitor.Connection> {
        val monitor = NetworkMonitor(); val account = monitor.openAccount(); monitor.setDetailed(account, true)
        return Triple(monitor, account, monitor.connect(account, MonitorLane.HTTP)!!)
    }

    @Test fun sharedR3FixturePreservesFractionalTimesAndDistinguishesInstalledFromObserved() {
        val result = parse(fixture())!!.domain
        assertEquals(44, result.events.size); assertEquals(5, result.events.map { it.type.protocol }.distinct().size)
        assertEquals(7, result.instrumented.size); assertTrue(MonitorProtocol.P2P in result.instrumented)
        assertFalse(result.events.any { it.type.protocol == MonitorProtocol.P2P || it.type.protocol == MonitorProtocol.WS })
        assertEquals(44, result.events.count { it.offsetMs % 1.0 != 0.0 })
        assertEquals(41, result.events.count { it.startOffsetMs?.let { start -> start % 1.0 != 0.0 } == true })
        assertEquals(24, result.events.count { it.durationMs % 1.0 != 0.0 })
        assertEquals(1.239, result.metrics.captureCostMs, 0.000001)
        assertTrue(result.events.any { it.type == ServerTraceEventType.S3_HEAD })
        assertTrue(result.events.any { it.type == ServerTraceEventType.WORKER_PREPARE })
        assertNotNull(ServerTraceBatch.exportJson(result))
    }

    @Test fun explicitCoalescedAndOptionalUnknownByteCasesRemainUnattributed() {
        val root = fixture(); val row = root.getJSONArray("events").getJSONObject(0)
        row.put("coalesced", true).put("traceId", JSONObject.NULL).put("parentSpanId", JSONObject.NULL).remove("bytes")
        val event = parse(root)!!.domain.events.first()
        assertNull(event.traceId); assertNull(event.parentSpanId); assertNull(event.bytes); assertTrue(event.coalesced!!)
        row.put("traceId", "a".repeat(32)); assertNull(parse(root))
    }

    @Test fun strictWhitelistTypesIdsNumbersAndCursorsRejectUnsafeInput() {
        val changes: List<(JSONObject) -> Unit> = listOf(
            { it.put("free", "private@example.test signed URL SECRET") },
            { it.put("v", "1") }, { it.put("captureId", "0".repeat(32)) }, { it.put("clockId", "x".repeat(32)) },
            { it.put("capacity", 201) }, { it.put("next", 1) }, { it.put("next", 9_007_199_254_740_992L) },
            { it.getJSONObject("metrics").put("dropped", 1) },
            { it.getJSONArray("instrumented").put("private@example.test") },
            { it.getJSONArray("events").getJSONObject(0).put("origin", "client") },
            { it.getJSONArray("events").getJSONObject(0).put("protocol", "s3") },
            { it.getJSONArray("events").getJSONObject(0).put("event", "s3.verify") },
            { it.getJSONArray("events").getJSONObject(0).put("status", "unknown") },
            { it.getJSONArray("events").getJSONObject(0).put("spanId", JSONObject.NULL) },
            { it.getJSONArray("events").getJSONObject(0).put("offsetMs", "1.2") },
            { it.getJSONArray("events").getJSONObject(0).put("durationMs", -1) },
            { it.getJSONArray("events").getJSONObject(0).put("startOffsetMs", 9_007_199_254_740_991L) },
            { it.getJSONArray("events").getJSONObject(0).put("startOffsetMs", 10).put("offsetMs", 20).put("durationMs", 1000) },
            { it.getJSONArray("events").getJSONObject(0).put("timestamp", 1.5) },
            { it.getJSONArray("events").getJSONObject(0).put("attempt", 1_000_001) },
            { it.getJSONArray("events").getJSONObject(0).put("bytes", true) },
            { it.getJSONArray("events").getJSONObject(0).put("errorCode", "SECRET raw failure") },
            { it.getJSONArray("events").getJSONObject(0).put("free", JSONObject().put("signedUrl", "SECRET")) },
            { it.getJSONArray("events").getJSONObject(1).put("sequence", 1) }
        )
        for ((index, change) in changes.withIndex()) { val root = fixture(); change(root); assertNull("invalid case $index", parse(root)) }
        val numberCase = fixture()
        numberCase.getJSONArray("events").getJSONObject(0).put("durationMs", 123)
        val raw = numberCase.toString().replaceFirst("\"durationMs\":123", "\"durationMs\":1e999")
        assertNotEquals(numberCase.toString(), raw)
        assertNull(ServerTraceBatch.parse(raw.toByteArray(), numberCase.getString("captureId")))
        val rounding = fixture()
        rounding.getJSONArray("events").getJSONObject(0).put("startOffsetMs", 10).put("offsetMs", 20).put("durationMs", 10.001)
        assertNotNull(parse(rounding))
        rounding.getJSONArray("events").getJSONObject(0).remove("startOffsetMs")
        rounding.getJSONArray("events").getJSONObject(0).put("durationMs", 1000)
        assertNotNull(parse(rounding)) // No measured start means the interval cannot be inferred here.
    }

    @Test fun depthBodyUtf8AndLenientJsonCannotReachRecursiveParser() {
        val capture = fixture().getString("captureId")
        val deep = "[".repeat(20_000) + "0" + "]".repeat(20_000)
        assertNull(ServerTraceBatch.parse(deep.toByteArray(), capture))
        assertNull(ServerTraceBatch.parse(ByteArray(ServerTraceBatch.MAX_BODY + 1) { 32 }, capture))
        assertNotNull(ServerTraceBatch.parse(fixture().toString().padEnd(ServerTraceBatch.MAX_BODY, ' ').toByteArray(), capture))
        assertNull(ServerTraceBatch.parse(byteArrayOf(0xc3.toByte(), 0x28), capture))
        assertNull(ServerTraceBatch.parse((fixture().toString() + "{}").toByteArray(), capture))
        assertNull(ServerTraceBatch.parse(("/* \" */" + deep + "/* \" */").toByteArray(), capture))
        assertNull(ServerTraceBatch.parse("{'quote':'\"','bad':$deep,'end':'\"'}".toByteArray(), capture))
        // Strings with brackets/escaped quotes must not alter the structural-depth count (then the free field is rejected).
        val free = fixture().put("free", "[[[\\\" }]] SECRET")
        assertNull(parse(free))
        val root = fixture(); val encoded = root.toString().replace("http.request", "http\\u002erequest")
        assertNotNull(ServerTraceBatch.parse(encoded.toByteArray(), capture))
        assertNull(ServerTraceBatch.parse(encoded.toByteArray(), "f".repeat(32)))
    }

    @Test fun nullExpiryIsOnlyUnavailableAndNotClientWallTtl() {
        val root = fixture().put("events", JSONArray()).put("next", 0).put("expiresAt", JSONObject.NULL)
        root.put("metrics", JSONObject().put("recorded", 0).put("dropped", 0).put("captureCostMs", 0))
        assertNull(parse(root)!!.domain.expiresAt)
        root.put("events", fixture().getJSONArray("events")); assertNull(parse(root))
        assertNotNull(parse(fixture().put("expiresAt", 0))) // Server wall can be far behind this client; no local TTL comparison.
    }

    @Test fun sharedBudgetDeduplicatesRefreshAndSeparatesBoundedClockDomains() {
        val (monitor, _, http) = ready(); val read = monitor.prepareServerRead(http)!!
        val server = batch(read.captureId, 200)
        assertEquals(MonitorError.NONE, monitor.installServer(read, server)); assertEquals(MonitorError.NONE, monitor.installServer(read, server))
        assertEquals(MonitorError.PROTOCOL, monitor.installServer(read, batch(read.captureId, 1, server.domain.clockId)))
        assertEquals(200L, monitor.snapshot().importedServerEvents)
        repeat(70) {
            val operation = monitor.begin(http, MonitorProtocol.HTTP, MonitorEvent.REQUEST)!!
            monitor.observe(operation, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorStatus.COMPLETED)
        }
        assertEquals(MonitorError.NONE, monitor.installServer(read, server))
        var state = monitor.snapshot()
        assertEquals(200L, state.importedServerEvents)
        assertEquals(200, state.events.size + state.serverDomains.sumOf { it.events.size })
        assertTrue(state.historyBytes <= 128 * 1024); assertTrue(state.dropped > 0)
        for (i in 1..3) assertEquals(MonitorError.NONE, monitor.installServer(read, batch(read.captureId, 1, i.toString(16).padStart(32, '0'))))
        val beforeCapacity = monitor.snapshot()
        for (i in 4..6) assertEquals(MonitorError.CAPACITY, monitor.installServer(read, batch(read.captureId, 1, i.toString(16).padStart(32, '0'))))
        state = monitor.snapshot()
        assertEquals(beforeCapacity, state)
        assertEquals(4, state.serverDomains.size)
        assertEquals(200L, state.serverDomains.first().next)
        assertTrue(state.serverDomains.drop(1).all { it.next == 1L }) // No cross-clock comparison with earlier next=200.
        assertEquals(203L, state.importedServerEvents)
        assertTrue(state.events.size + state.serverDomains.sumOf { it.events.size } <= 200)
    }

    @Test fun safeExportAndCapturedReadCannotSurviveClearOffOrAccountReplacement() {
        val (monitor, account, http) = ready(); val read = monitor.prepareServerRead(http)!!
        assertEquals(MonitorError.NONE, monitor.installServer(read, batch(read.captureId)))
        val export = monitor.prepareExport(account)!!
        val safe = JSONObject(monitor.exportJson(export)!!)
        assertEquals(44, safe.getJSONArray("serverDomains").getJSONObject(0).getJSONArray("events").length())
        assertTrue(monitor.exportText(export)!!.contains("separate"))
        monitor.clearHistory(account)
        assertEquals(MonitorError.CANCELLED, monitor.installServer(read, batch(read.captureId))); assertNull(monitor.exportJson(export)); assertNull(monitor.exportText(export))
        val fresh = monitor.prepareServerRead(http)!!
        monitor.setDetailed(account, false); assertEquals(MonitorError.CANCELLED, monitor.installServer(fresh, batch(fresh.captureId)))
        monitor.openAccount(); assertTrue(monitor.snapshot().serverDomains.isEmpty())
        assertEquals(0, monitor.snapshot().historyBytes)
    }
}
