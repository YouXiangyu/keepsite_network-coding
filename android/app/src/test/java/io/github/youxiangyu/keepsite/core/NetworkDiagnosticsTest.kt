package io.github.youxiangyu.keepsite.core

import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.IOException
import java.io.InterruptedIOException
import java.net.SocketTimeoutException
import java.util.concurrent.CancellationException

class NetworkDiagnosticsTest {
    private class Clock : MonitorClock {
        override val id = "d".repeat(32)
        var broken = false
        override fun wallMillis(): Long { if (broken) throw IllegalStateException("private clock error"); return 1_800_000_000_000 }
        override fun nanos() = 123_000_000L
    }
    private val origin = "https://keepsite.space".toHttpUrl()
    private fun request(url: String = "https://keepsite.space/api/media/" + "a".repeat(64)) = Request.Builder().url(url)
        .header("X-Keepsite-Diagnostics", "untrusted-old").header("X-Keepsite-Trace-Id", "untrusted-old").build()
    private fun ready(monitor: NetworkMonitor = NetworkMonitor()): Pair<NetworkDiagnostics, NetworkMonitor.Account> {
        val diagnostics = NetworkDiagnostics(monitor)
        val account = diagnostics.openAccount(4)!!
        diagnostics.setDetailed(true)
        return diagnostics to account
    }

    @Test fun clockAndJsonFailuresKeepBusinessSuccessAndReleasePartiallyStartedSlots() {
        val clock = Clock()
        val monitor = NetworkMonitor(clock)
        val (diagnostics, _) = ready(monitor)
        clock.broken = true // begin inserts active, then wallMillis fails during initial observe.
        repeat(40) {
            assertEquals("original-success", diagnostics.http(4, origin, request(), true) { actual, observation ->
                assertNull(observation); assertNull(actual.header("X-Keepsite-Diagnostics")); "original-success"
            })
        }
        clock.broken = false
        assertEquals(0, monitor.snapshot().active)
        diagnostics.http(4, origin, request(), true) { _, observation ->
            observation!!.response(200); observation.read(7); clock.broken = true; "body"
        }
        clock.broken = false
        assertEquals(0, monitor.snapshot().active); assertEquals(0, diagnostics.snapshot()!!.api.active)
        val jsonMonitor = NetworkMonitor(eventSize = { throw JSONException("private body canary") })
        val (jsonDiagnostics, _) = ready(jsonMonitor)
        repeat(40) { assertEquals(123, jsonDiagnostics.http(4, origin, request(), true) { _, _ -> 123 }) }
        assertEquals(0, jsonMonitor.snapshot().active)
        assertTrue(jsonMonitor.snapshot().dropped >= 40)
    }

    @Test fun originalFailureAndCancellationObjectsSurviveObserverFailure() {
        val clock = Clock(); val monitor = NetworkMonitor(clock)
        val (diagnostics, _) = ready(monitor)
        for (original in listOf(IOException("original private failure"), CancellationException("original cancel"))) {
            var caught: Throwable? = null
            try {
                diagnostics.http(4, origin, request(), true) { _, observation ->
                    assertNotNull(observation); clock.broken = true; throw original
                }
            } catch (failure: Throwable) { caught = failure }
            clock.broken = false
            assertSame(original, caught); assertEquals(0, monitor.snapshot().active)
            assertEquals(0, diagnostics.snapshot()!!.api.active)
        }
    }

    @Test fun explicitTimeoutAndOrdinaryIoAndCancellationKeepTheirIdentityAndCategory() {
        val (diagnostics, _) = ready()
        for ((original, expected) in listOf(
            SocketTimeoutException("private timeout") to MonitorError.TIMEOUT,
            InterruptedIOException("not proven timeout") to MonitorError.NETWORK,
            IOException("private IO") to MonitorError.NETWORK,
            java.nio.charset.MalformedInputException(1) to MonitorError.PROTOCOL,
            CancellationException("private cancellation") to MonitorError.CANCELLED)) {
            val caught = assertThrows(original.javaClass) {
                diagnostics.http(4, origin, request(), true) { _, _ -> throw original }
            }
            assertSame(original, caught)
            val state = diagnostics.snapshot()!!
            assertEquals(expected, state.api.lastError)
            assertEquals(expected, state.observations.events.last().values.error)
            assertEquals(0, state.api.active); assertEquals(0, state.observations.active)
        }
        val wrapped = ApiFailure(message = "unchanged business failure")
        assertSame(wrapped, assertThrows(ApiFailure::class.java) {
            diagnostics.http(4, origin, request(), true) { _, observation ->
                observation!!.failedAs(diagnosticIoError(SocketTimeoutException("private timeout")))
                throw wrapped // HttpApi keeps its existing conversion to the business ApiFailure.
            }
        })
        assertEquals(MonitorError.TIMEOUT, diagnostics.snapshot()!!.api.lastError)
        assertEquals(0, diagnostics.snapshot()!!.observations.active)
    }

    @Test fun closedSocketClearsCurrentCapabilitiesBeforeAnyReconnect() {
        val (diagnostics, account) = ready()
        val socket = diagnostics.socket(account, 4)!!
        socket.opened(); socket.received("sync.ready", 80, SocketCapabilities(15000, 1), "space", 0)
        assertEquals(45000L, diagnostics.snapshot()!!.socket.watchdogMs)
        socket.closed(1008, 1000)
        val closed = diagnostics.snapshot()!!.socket
        assertEquals(MonitorStatus.RETRYING, closed.status); assertFalse(closed.opened); assertFalse(closed.aligned)
        assertNull(closed.heartbeatMs); assertNull(closed.diagnosticsVersion); assertNull(closed.watchdogMs)
        assertEquals(1000L, closed.retryDelayMs); assertEquals(MonitorError.NONE, closed.error)
        assertEquals(0, diagnostics.snapshot()!!.observations.active)
    }

    @Test fun nonemptySnapshotCountsSnapshotRowsInsteadOfDeltaRows() {
        val (diagnostics, account) = ready()
        val ref = JSONObject().put("assetId", "a".repeat(64)).put("sha256", "a".repeat(64)).put("size", 5)
            .put("mimeType", "image/png").put("width", 1).put("height", 1).put("cloudStatus", "saved").put("url", "/api/media/" + "a".repeat(64))
        val ticket = JSONObject().put("id", "fixture-ticket").put("fields", Wire.fieldsJson(TicketFields()))
            .put("fieldSources", JSONObject(FIELD_NAMES.associateWith { "empty" })).put("createdAt", 1).put("updatedAt", 1)
            .put("version", 1).put("inputVersion", 1).put("deletedAt", JSONObject.NULL).put("operationId", "fixture-operation")
            .put("media", JSONObject().put("original", ref).put("poster", JSONObject.NULL))
            .put("processing", JSONObject().put("status", "not_started").put("error", JSONObject.NULL))
        val replies = ArrayDeque(listOf(snapshot().put("total", 1).put("tickets", JSONArray().put(ticket)), changes(),
            changes().put("nextCursor", 2).put("headCursor", 2).put("changes", JSONArray()
                .put(JSONObject().put("cursor", 1).put("type", "delete").put("id", "fixture-ticket").put("version", 2).put("deletedAt", 2))
                .put(JSONObject().put("cursor", 2).put("type", "delete").put("id", "fixture-other").put("version", 1).put("deletedAt", 2)))))
        val engine = SyncEngine("fixture-user") { replies.removeFirst() }
        assertEquals(1, engine.sync(observation = diagnostics.sync(account, 4)).tickets.size)
        val first = diagnostics.snapshot()!!
        assertEquals(MonitorEvent.SNAPSHOT, first.observations.events.last().event)
        assertEquals(1L, first.observations.events.last().values.items)
        assertEquals(1L, first.sync.lastSnapshotItems); assertEquals(0L, first.sync.lastDeltaItems)
        assertNotNull(first.sync.lastDataChangeAt)
        assertTrue(engine.sync(observation = diagnostics.sync(account, 4)).tickets.isEmpty())
        val next = diagnostics.snapshot()!!
        assertEquals(MonitorEvent.DELTA, next.observations.events.last().event)
        assertEquals(2L, next.observations.events.last().values.items)
        assertEquals(1L, next.sync.lastSnapshotItems); assertEquals(2L, next.sync.lastDeltaItems)
        assertEquals(0, next.observations.active)

        val (combined, combinedAccount) = ready()
        val combinedReplies = ArrayDeque(listOf(snapshot().put("total", 1).put("tickets", JSONArray().put(ticket)),
            changes().put("nextCursor", 2).put("headCursor", 2).put("changes", JSONArray()
                .put(JSONObject().put("cursor", 1).put("type", "delete").put("id", "fixture-ticket").put("version", 2).put("deletedAt", 2))
                .put(JSONObject().put("cursor", 2).put("type", "delete").put("id", "fixture-other").put("version", 1).put("deletedAt", 2)))))
        assertTrue(SyncEngine("fixture-user") { combinedReplies.removeFirst() }.sync(observation = combined.sync(combinedAccount, 4)).tickets.isEmpty())
        val combinedState = combined.snapshot()!!
        assertEquals(MonitorEvent.SNAPSHOT, combinedState.observations.events.last().event)
        assertEquals(1L, combinedState.observations.events.last().values.items)
        assertEquals(1L, combinedState.sync.lastSnapshotItems); assertEquals(2L, combinedState.sync.lastDeltaItems)
    }

    @Test fun actualUrlAuthenticationEpochAndTemplateControlHeadersWithoutLeakingRequestData() {
        val (diagnostics, _) = ready()
        val safe = request("https://keepsite.space/api/media/" + "a".repeat(64) + "/download?token=SECRET")
        diagnostics.http(4, origin, safe, true) { actual, observation ->
            assertTrue(actual.header("X-Keepsite-Diagnostics")!!.matches(Regex("[0-9a-f]{32}")))
            assertTrue(actual.header("X-Keepsite-Trace-Id")!!.matches(Regex("[0-9a-f]{32}")))
            assertEquals(safe.url, actual.url); observation!!.response(200); observation.read(3)
        }
        for (url in listOf("https://bucket.s3.amazonaws.com/key?signature=SECRET", "https://github.com/x",
            "https://accounts.google.com/oauth", "https://keepsite.space:8443/api/media", "http://keepsite.space/api/media",
            "https://keepsite.space/api/auth/account/methods", "https://keepsite.space/api/diagnostics/events", "https://keepsite.space/api/private-new")) {
            diagnostics.http(4, origin, request(url), true) { actual, _ ->
                assertNull(url, actual.header("X-Keepsite-Diagnostics")); assertNull(actual.header("X-Keepsite-Trace-Id"))
            }
        }
        for (epoch in listOf(3L, 4L)) diagnostics.http(epoch, origin, safe, false) { actual, _ -> assertNull(actual.header("X-Keepsite-Diagnostics")) }
        val state = diagnostics.snapshot()!!
        assertEquals(0, state.api.active); assertEquals(0, state.observations.active)
        assertFalse(state.observations.events.toString().contains("SECRET"))
        assertFalse(state.observations.events.toString().contains("signature="))
    }

    @Test fun oldHttpCompletionCannotTouchNewAccountOrCreateNewCaptureHistory() {
        val (diagnostics, _) = ready()
        diagnostics.http(4, origin, request(), true) { _, observation ->
            observation!!.response(200)
            diagnostics.openAccount(4)
            "old body may finish its original business"
        }
        val fresh = diagnostics.snapshot()!!
        assertNull(fresh.api.lastSuccessAt); assertEquals(0, fresh.api.active)
        assertTrue(fresh.observations.events.isEmpty())
        diagnostics.setDetailed(true)
        diagnostics.http(4, origin, request(), true) { _, observation ->
            diagnostics.clearHistory(); observation!!.response(200)
        }
        assertTrue(diagnostics.snapshot()!!.observations.events.isEmpty())
        assertNotNull(diagnostics.snapshot()!!.api.lastSuccessAt)
    }

    @Test fun websocketCapabilitiesAreOptionalAndEveryInstanceStartsUnknown() {
        val (diagnostics, account) = ready()
        val first = diagnostics.socket(account, 4)!!
        first.opened(); first.received("sync.ready", 80, SocketCapabilities.read(JSONObject().put("heartbeatMs", 15000).put("diagnosticsVersion", 1)), "space", 5)
        first.aligned(true)
        assertEquals(15000L, diagnostics.snapshot()!!.socket.heartbeatMs)
        assertEquals(1, diagnostics.snapshot()!!.socket.diagnosticsVersion)
        val next = diagnostics.socket(account, 4)!!
        assertNull(diagnostics.snapshot()!!.socket.heartbeatMs); assertNull(diagnostics.snapshot()!!.socket.diagnosticsVersion)
        first.received("sync.hint", 50, SocketCapabilities(1, 1), "stale-space", 999)
        first.closed(1008, 1000)
        assertEquals(MonitorStatus.STARTED, diagnostics.snapshot()!!.socket.status)
        next.opened(); next.received("sync.hint", 50, SocketCapabilities.read(JSONObject().put("heartbeatMs", "15000").put("diagnosticsVersion", 2)), "space", 6)
        assertNull(diagnostics.snapshot()!!.socket.heartbeatMs); assertNull(diagnostics.snapshot()!!.socket.diagnosticsVersion)
        next.closed(1008, 1000)
        assertEquals(MonitorError.NONE, diagnostics.snapshot()!!.socket.error) // 1008 alone is not authentication evidence.
        assertEquals(0, diagnostics.snapshot()!!.observations.active)
        diagnostics.suspend(account); assertEquals(MonitorStatus.PAUSED, diagnostics.snapshot()!!.socket.status)
    }

    private fun snapshot() = JSONObject().put("userId", "fixture-user").put("serverId", "space").put("snapshotId", "frozen")
        .put("cursor", 0).put("total", 0).put("offset", 0).put("expiresAt", 600000).put("tickets", JSONArray())
        .put("nextPageToken", JSONObject.NULL).put("capabilities", capabilities())
    private fun capabilities() = JSONObject().put("processing", false).put("cloudStorage", false).put("deviceSync", true)
        .put("processingUnavailableReason", JSONObject.NULL)
    private fun changes() = JSONObject().put("userId", "fixture-user").put("serverId", "space").put("fromCursor", 0)
        .put("nextCursor", 0).put("headCursor", 0).put("hasMore", false).put("changes", JSONArray()).put("capabilities", capabilities())

    @Test fun syncOnlyCompletesAfterActualApplyAndDoesNotEraseNewerObservedWatermark() {
        val (diagnostics, account) = ready()
        diagnostics.socket(account, 4)!!.received("sync.hint", 50, SocketCapabilities(), "space", 5)
        val replies = ArrayDeque(listOf(snapshot(), changes()))
        val engine = SyncEngine("fixture-user") { replies.removeFirst() }
        val observation = diagnostics.sync(account, 4)!!
        val result = engine.sync(observation = observation)
        assertSame(result, engine.current)
        val state = diagnostics.snapshot()!!.sync
        assertEquals(0L, state.localCursor); assertEquals(5L, state.observedCursor); assertEquals(true, state.pending)
        assertNotNull(state.lastSnapshotAt); assertNotNull(state.lastDeltaAt); assertNull(state.lastDataChangeAt)
        observation.watermark("stale-space", 999)
        assertEquals(5L, diagnostics.snapshot()!!.sync.observedCursor)
        assertEquals(1, diagnostics.snapshot()!!.observations.active) // The separate WS lifetime remains active.
    }

    @Test fun syncFailureRetainsOriginalExceptionAndNeverMarksPartialSnapshotApplied() {
        val (diagnostics, account) = ready()
        val original = IOException("private test failure")
        var reads = 0
        val engine = SyncEngine("fixture-user") { if (reads++ == 0) snapshot() else throw original }
        val caught = assertThrows(IOException::class.java) { engine.sync(observation = diagnostics.sync(account, 4)) }
        assertSame(original, caught); assertNull(engine.current)
        val state = diagnostics.snapshot()!!.sync
        assertNull(state.localCursor); assertNull(state.lastDeltaAt); assertNull(state.lastSnapshotAt)
        assertFalse(state.running); assertEquals(0, diagnostics.snapshot()!!.observations.active)
    }

    @Test fun httpServerChangeClearsOldWatermarkBeforeRebuildAndRejectsOldSocketHints() {
        val (diagnostics, account) = ready()
        val oldSocket = diagnostics.socket(account, 4)!!
        var reads = 0
        val engine = SyncEngine("fixture-user") {
            when (reads++) {
                0 -> snapshot().put("serverId", "old").put("cursor", 100)
                1 -> changes().put("serverId", "old").put("fromCursor", 100).put("nextCursor", 100).put("headCursor", 100)
                2 -> throw ApiFailure(410, "server_changed", "fixture")
                3 -> {
                    assertNull(diagnostics.snapshot()!!.sync.localCursor)
                    assertNull(diagnostics.snapshot()!!.sync.observedCursor)
                    oldSocket.received("sync.hint", 80, SocketCapabilities(), "old", 9999)
                    assertNull(diagnostics.snapshot()!!.sync.observedCursor)
                    snapshot().put("serverId", "new").put("cursor", 2)
                }
                else -> {
                    oldSocket.received("sync.hint", 80, SocketCapabilities(), "old", 9999)
                    assertNull(diagnostics.snapshot()!!.sync.observedCursor)
                    changes().put("serverId", "new").put("fromCursor", 2).put("nextCursor", 2).put("headCursor", 2)
                }
            }
        }
        engine.sync(observation = diagnostics.sync(account, 4))
        oldSocket.received("sync.hint", 80, SocketCapabilities(), "old", 150)
        assertEquals(150L, diagnostics.snapshot()!!.sync.observedCursor)
        assertEquals(2L, engine.sync(observation = diagnostics.sync(account, 4)).cursor)
        val reset = diagnostics.snapshot()!!.sync
        assertEquals(2L, reset.localCursor); assertEquals(2L, reset.observedCursor); assertEquals(false, reset.pending)
        diagnostics.socket(account, 4)!!.received("sync.ready", 80, SocketCapabilities(), "new", 3)
        assertEquals(3L, diagnostics.snapshot()!!.sync.observedCursor)
        assertEquals(true, diagnostics.snapshot()!!.sync.pending)
    }

    @Test fun firstOrNewDomainHintDoesNotFinishPullAndHeartbeatDoesNotEraseFailure() {
        val (diagnostics, account) = ready()
        var socket = diagnostics.socket(account, 4)!!
        var reads = 0
        val original = IOException("fixture unavailable")
        val engine = SyncEngine("fixture-user") {
            assertTrue(diagnostics.snapshot()!!.sync.running)
            if (reads++ == 0) {
                socket.received("sync.ready", 80, SocketCapabilities(), "space", 0)
                assertTrue("First watermark must preserve the actual pull", diagnostics.snapshot()!!.sync.running)
                snapshot()
            } else {
                socket = diagnostics.socket(account, 4)!!
                socket.received("sync.ready", 80, SocketCapabilities(), "new-space", 4)
                assertTrue("New WS domain must preserve the actual pull", diagnostics.snapshot()!!.sync.running)
                throw original
            }
        }
        assertSame(original, assertThrows(IOException::class.java) { engine.sync(observation = diagnostics.sync(account, 4)) })
        val failed = diagnostics.snapshot()!!.sync
        assertFalse(failed.running); assertEquals(MonitorError.PROTOCOL, failed.error)
        socket.received("sync.hint", 80, SocketCapabilities(), "new-space", 4)
        assertEquals(MonitorError.PROTOCOL, diagnostics.snapshot()!!.sync.error)
        diagnostics.socket(account, 4)!!.received("sync.ready", 80, SocketCapabilities(), "third-space", 0)
        assertEquals(MonitorError.PROTOCOL, diagnostics.snapshot()!!.sync.error)
        assertNull(diagnostics.snapshot()!!.sync.lastDeltaAt)
    }
}
