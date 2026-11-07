package io.github.youxiangyu.keepsite.core

import com.sun.net.httpserver.HttpServer
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.io.File
import java.net.InetAddress
import java.net.InetSocketAddress
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

class NetworkSocketMeasurementTest {
    @Test fun actualSocketHttpAndOriginalSyncEngineHaveEquivalentOffOnResultsWithRecovery() {
        val capabilities = JSONObject().put("processing", false).put("cloudStorage", false).put("deviceSync", true).put("processingUnavailableReason", "fixture")
        fun snapshot() = JSONObject().put("serverId", "fixture-server").put("userId", "fixture-user").put("snapshotId", "fixture-snapshot")
            .put("cursor", 0).put("total", 0).put("offset", 0).put("expiresAt", 600000).put("tickets", JSONArray()).put("nextPageToken", JSONObject.NULL).put("capabilities", capabilities)
        val ref = JSONObject().put("assetId", "a".repeat(64)).put("sha256", "a".repeat(64)).put("size", 5).put("mimeType", "image/png")
            .put("width", 1).put("height", 1).put("cloudStatus", "not_configured").put("url", "/api/media/" + "a".repeat(64))
        val ticket = JSONObject().put("id", "fixture-ticket").put("fields", Wire.fieldsJson(TicketFields(title = "fixture")))
            .put("fieldSources", JSONObject(FIELD_NAMES.associateWith { "empty" })).put("createdAt", 1).put("updatedAt", 1).put("version", 1)
            .put("inputVersion", 1).put("deletedAt", JSONObject.NULL).put("operationId", "fixture-operation")
            .put("media", JSONObject().put("original", ref).put("poster", JSONObject.NULL)).put("processing", JSONObject().put("status", "not_started").put("error", JSONObject.NULL))
        fun delta(from: Int, changed: Boolean = false) = JSONObject().put("serverId", "fixture-server").put("userId", "fixture-user").put("fromCursor", from)
            .put("nextCursor", if (changed) 1 else from).put("headCursor", if (changed) 1 else from).put("hasMore", false).put("capabilities", capabilities)
            .put("changes", if (changed) JSONArray().put(JSONObject().put("cursor", 1).put("type", "upsert").put("ticket", ticket)) else JSONArray())
        val plan = listOf(200 to snapshot(), 200 to delta(0), 200 to delta(0), 410 to JSONObject().put("code", "cursor_expired"),
            200 to snapshot(), 200 to delta(0, true), 200 to delta(1)).map { (status, body) -> status to body.toString().toByteArray(Charsets.UTF_8) }
        val executor = Executors.newSingleThreadExecutor(); val index = AtomicInteger(); val detail = AtomicBoolean()
        val serverErrors = java.util.Collections.synchronizedList(mutableListOf<String>())
        val server = HttpServer.create(InetSocketAddress(InetAddress.getByName("127.0.0.1"), 0), 0).apply {
            this.executor = executor
            createContext("/api/sync/") { exchange ->
                try {
                    val sequence = index.getAndIncrement()
                    if (exchange.requestMethod != "GET" || sequence !in plan.indices) throw IllegalStateException("unexpected request")
                    val expectedPath = if (sequence in setOf(0, 4)) "/api/sync/snapshot" else "/api/sync/changes"
                    if (exchange.requestURI.path != expectedPath) throw IllegalStateException("unexpected route order")
                    for (header in listOf("X-Keepsite-Diagnostics", "X-Keepsite-Trace-Id")) {
                        val value = exchange.requestHeaders.getFirst(header)
                        if (detail.get()) { if (value?.matches(Regex("[a-f0-9]{32}")) != true) throw IllegalStateException("missing opt-in trace") }
                        else if (value != null) throw IllegalStateException("off mode emitted trace")
                    }
                    val (status, bytes) = plan[sequence]
                    exchange.responseHeaders.set("Content-Type", "application/json; charset=utf-8")
                    exchange.sendResponseHeaders(status, bytes.size.toLong()); exchange.responseBody.use { it.write(bytes) }
                } catch (_: Exception) { serverErrors += "fixture_request_failed"; runCatching { exchange.sendResponseHeaders(500, -1) } }
                finally { exchange.close() }
            }
            start()
        }
        val http = JSONArray(); val sync = JSONArray(); val finalStates = mutableListOf<Synced>()
        try {
            for (enabled in listOf(false, true)) {
                detail.set(enabled); index.set(0)
                val d = NetworkDiagnostics(); val account = d.openAccount(1)!!; d.setDetailed(enabled)
                val origin = "http://127.0.0.1:${server.address.port}".toHttpUrl()
                val client = OkHttpClient.Builder().retryOnConnectionFailure(false).callTimeout(5, TimeUnit.SECONDS).build()
                var sample = 0
                val engine = SyncEngine("fixture-user") { path ->
                    val start = System.nanoTime(); var status: Int? = null; var bytesRead = 0; var failed = true
                    try {
                        d.http(1, origin, Request.Builder().url(origin.resolve(path)!!).build(), true) { request, observation ->
                            client.newCall(request).execute().use { response ->
                                status = response.code; observation!!.response(response.code)
                                val body = ByteArrayOutputStream()
                                response.body.byteStream().use { stream ->
                                    val buffer = ByteArray(8192)
                                    while (true) { val count = stream.read(buffer); if (count < 0) break; body.write(buffer, 0, count); bytesRead += count; observation.read(count); check(bytesRead <= 256 * 1024) }
                                }
                                if (!response.isSuccessful) throw ApiFailure(response.code, "cursor_expired", "isolated transport response")
                                val decoded = Charsets.UTF_8.newDecoder().onMalformedInput(java.nio.charset.CodingErrorAction.REPORT).decode(java.nio.ByteBuffer.wrap(body.toByteArray())).toString()
                                JSONObject(decoded).also { failed = false }
                            }
                        }
                    } finally {
                        val end = System.nanoTime()
                        http.put(JSONObject().put("mode", if (enabled) "on" else "off").put("sample", sample++).put("template", if (path.startsWith("/api/sync/snapshot")) "snapshot" else "delta")
                            .put("startNs", start).put("endNs", end).put("elapsedNs", end - start).put("status", status ?: JSONObject.NULL).put("actualReadBytes", bytesRead).put("failed", failed))
                    }
                }
                try {
                    repeat(4) { call ->
                        val firstHttp = sample; val start = System.nanoTime(); val applied = engine.sync(observation = d.sync(account, 1)); val end = System.nanoTime()
                        assertSame(applied, engine.current)
                        val state = d.snapshot()!!.sync; assertEquals(applied.cursor, state.localCursor)
                        sync.put(JSONObject().put("mode", if (enabled) "on" else "off").put("sample", call).put("startNs", start).put("endNs", end).put("elapsedNs", end - start)
                            .put("httpRequests", sample - firstHttp).put("localCursor", state.localCursor).put("observedCursor", state.observedCursor ?: JSONObject.NULL)
                            .put("snapshotRows", state.lastSnapshotItems ?: JSONObject.NULL).put("deltaItems", state.lastDeltaItems ?: JSONObject.NULL).put("tickets", applied.tickets.size).put("returnedApplied", true))
                    }
                    assertEquals(7, index.get()); assertEquals(1L, engine.current!!.cursor); assertEquals(1, engine.current!!.tickets.size)
                    assertTrue(serverErrors.isEmpty()); finalStates += engine.current!!
                } finally { client.connectionPool.evictAll(); client.dispatcher.executorService.shutdownNow() }
            }
            assertEquals(finalStates[0], finalStates[1])
            assertEquals(14, http.length()); assertEquals(8, sync.length())
        } finally { server.stop(0); executor.shutdownNow(); assertTrue(executor.awaitTermination(2, TimeUnit.SECONDS)) }
        val evidence = JSONObject().put("scope", "Windows JVM real loopback HTTP socket + actual NetworkDiagnostics.http + original SyncEngine atomic apply then return; synthetic responses/authenticated flag")
            .put("clock", "System.nanoTime").put("httpSamples", http).put("syncSamples", sync).put("sameFinalSynced", true).put("requestsPerMode", 7)
            .put("successfulHttpPerMode", 6).put("recoveredHttp410PerMode", 1).put("syncReturnedSamplesPerMode", 4).put("serverAndClientClosed", true)
            .put("limits", "one identical small sequence per mode; off then on order; no causal monitor overhead, Repository/UI commit callback, WS heartbeat, whole HttpApi auth, Android/RTC/production claim; HTTP and sync scopes overlap")
        System.getenv("KEEPSITE_N4_SOCKET_OUTPUT")?.let { File(it, "socket-sync-measurements.json").writeText(evidence.toString(2) + "\n") }
    }
}
