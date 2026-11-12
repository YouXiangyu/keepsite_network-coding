package io.github.youxiangyu.keepsite.core

import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.Buffer
import okio.BufferedSource
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

/** OkHttp application interceptors return isolated responses without opening a socket. */
class ServerDiagnosticsHttpTest {
    private val origin = "https://keepsite.space".toHttpUrl()
    private fun ready(): Pair<NetworkDiagnostics, NetworkMonitor.Account> {
        val diagnostics = NetworkDiagnostics(); val account = diagnostics.openAccount(7)!!
        diagnostics.setDetailed(true); return diagnostics to account
    }
    private fun fixture(capture: String) = javaClass.getResourceAsStream("/diagnostics/server-r3-isolated-trace.json")!!.use {
        JSONObject(it.readBytes().toString(Charsets.UTF_8)).put("captureId", capture).toString()
    }
    private fun client(code: Int = 200, body: (Request) -> String, seen: (Request) -> Unit = {}): OkHttpClient =
        OkHttpClient.Builder().addInterceptor { chain ->
            seen(chain.request())
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(code).message("not retained")
                .body(body(chain.request()).toResponseBody()).build()
        }.build()
    private fun execute(action: NetworkDiagnostics.ServerAction, client: OkHttpClient, authenticated: () -> Boolean = { true },
                        csrf: () -> String? = { "private-csrf" }, builder: (String, Long) -> Request.Builder = { path, _ ->
                            Request.Builder().url(origin.resolve(path)!!).header("Cookie", "private-cookie")
                                .header("X-Keepsite-Trace-Id", "old-trace")
                        }): Boolean = ServerDiagnosticsHttp.execute(origin, client, action, authenticated, csrf, builder) { assertEquals(7L, it) }

    @Test fun manualGetUsesOnlyCapturedHeaderAndDoesNotObserveItselfOrPoll() {
        val (diagnostics, account) = ready(); val action = diagnostics.prepareServerAction(7, false)!!
        var calls = 0
        val client = client(body = { fixture(action.captureId) }, seen = {
            calls++; assertEquals("GET", it.method); assertEquals("https://keepsite.space/api/diagnostics/events?after=0", it.url.toString())
            assertEquals(action.captureId, it.header("X-Keepsite-Diagnostics")); assertNull(it.header("X-Keepsite-Trace-Id"))
            assertEquals("private-cookie", it.header("Cookie"))
        })
        assertEquals(0, calls); assertTrue(execute(action, client)); assertEquals(1, calls)
        val state = diagnostics.snapshot()!!
        assertEquals(0L, state.observations.recorded); assertTrue(state.observations.events.isEmpty())
        assertEquals(44L, state.observations.importedServerEvents); assertNull(state.api.lastSuccessAt)
        assertFalse(state.toString().contains("private-cookie")); assertFalse(state.toString().contains("private-csrf"))
        val exported = diagnostics.monitor.exportJson(diagnostics.monitor.prepareExport(account)!!)!!
        assertFalse(exported.contains("private-cookie")); assertFalse(exported.contains("private-csrf")); assertFalse(exported.contains("?after="))
        assertFalse(action.current()); assertNull(action.cancel)
    }

    @Test fun unavailableAndInvalidResponsesDoNotRotateRetryOrInstallFreeFields() {
        val (diagnostics, _) = ready()
        for ((code, payload) in listOf(404 to "PRIVATE missing", 503 to "PRIVATE unavailable", 401 to "PRIVATE auth", 403 to "PRIVATE denied",
            200 to "{\"free\":\"PRIVATE signed URL\"}", 200 to "[".repeat(10000))) {
            val action = diagnostics.prepareServerAction(7, false)!!; var calls = 0
            assertFalse(execute(action, client(code, { payload }, { calls++ })))
            assertEquals(1, calls); assertFalse(action.current()); assertNull(action.cancel)
            val next = diagnostics.prepareServerAction(7, false)!!
            assertEquals(action.captureId, next.captureId)
            val state = diagnostics.snapshot()!!
            assertTrue(state.observations.serverDomains.isEmpty()); assertFalse(state.toString().contains("PRIVATE"))
        }
        diagnostics.setDetailed(false); assertNull(diagnostics.prepareServerAction(7, false))
    }

    @Test fun allScopeInvalidationsCancelTheOwnedCallAndRejectItsResponse() {
        val mutations: List<(NetworkDiagnostics, NetworkMonitor.Account) -> Unit> = listOf(
            { d, _ -> d.clearHistory() }, { d, _ -> d.setDetailed(false) }, { d, _ -> d.pauseHistory(true) },
            { d, a -> d.suspend(a) }, { d, a -> d.resume(a, 8) }, { d, _ -> d.openAccount(7) }, { d, a -> d.closeAccount(a) }
        )
        for (mutate in mutations) {
            val (diagnostics, account) = ready(); val action = diagnostics.prepareServerAction(7, false)!!
            var cancelled = 0; assertTrue(action.attach { cancelled++ })
            mutate(diagnostics, account)
            assertEquals(1, cancelled); assertFalse(action.current()); assertNull(action.cancel)
            assertFalse(action.received(ServerTraceBatch.parse(fixture(action.captureId).toByteArray(), action.captureId)!!))
            assertTrue(diagnostics.snapshot()!!.observations.serverDomains.isEmpty())
        }
    }

    @Test fun clearDuringActualCallCancelsAndCannotPublishIntoNewHistory() {
        val (diagnostics, _) = ready(); val action = diagnostics.prepareServerAction(7, false)!!
        var cancelled = false
        val client = OkHttpClient.Builder().addInterceptor { chain ->
            diagnostics.clearHistory(); cancelled = chain.call().isCanceled()
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(200).message("ignored")
                .body(fixture(action.captureId).toResponseBody()).build()
        }.build()
        assertFalse(execute(action, client)); assertTrue(cancelled)
        assertTrue(diagnostics.snapshot()!!.observations.serverDomains.isEmpty())
        assertEquals(ServerReadState(), diagnostics.snapshot()!!.serverRead)
    }

    @Test fun explicitDeleteInvalidatesOldExportImmediatelyAndTargetsOnlyOldCaptureWithCsrf() {
        val (diagnostics, _) = ready()
        val get = diagnostics.prepareServerAction(7, false)!!
        assertTrue(execute(get, client(body = { fixture(get.captureId) })))
        val monitor = diagnostics.monitor
        val account = get.read.connection.account
        val export = monitor.prepareExport(account)!!
        val deletion = diagnostics.prepareServerAction(7, true)!!
        assertEquals(get.captureId, deletion.captureId); assertNotEquals(deletion.captureId, deletion.read.captureId)
        assertNull(diagnostics.prepareServerAction(7, false)); assertNull(diagnostics.prepareServerAction(7, true))
        assertTrue(deletion.current()) // Refresh coalescing must not discard an explicit remote clear.
        assertNull(monitor.exportJson(export)); assertTrue(monitor.snapshot().serverDomains.isEmpty())
        assertTrue(execute(deletion, client(body = { "{\"cleared\":true}" }, seen = {
            assertEquals("DELETE", it.method); assertNull(it.url.query)
            assertEquals(get.captureId, it.header("X-Keepsite-Diagnostics")); assertEquals("private-csrf", it.header("X-CSRF-Token"))
            assertNull(it.header("X-Keepsite-Trace-Id"))
        })))
        assertEquals(deletion.read.captureId, diagnostics.prepareServerAction(7, false)!!.captureId)
        assertTrue(monitor.snapshot().serverDomains.isEmpty())
    }

    @Test fun authenticationCsrfAndActualSameOriginAreCheckedBeforeSendingAnything() {
        val (diagnostics, _) = ready(); var calls = 0
        val client = client(body = { "{}" }, seen = { calls++ })
        assertFalse(execute(diagnostics.prepareServerAction(7, false)!!, client, authenticated = { false }))
        assertFalse(execute(diagnostics.prepareServerAction(7, true)!!, client, csrf = { null }))
        for (url in listOf("https://github.com/api/diagnostics/events?after=0", "https://keepsite.space:8443/api/diagnostics/events?after=0",
            "http://keepsite.space/api/diagnostics/events?after=0", "https://keepsite.space/api/diagnostics/events?after=1")) {
            assertFalse(execute(diagnostics.prepareServerAction(7, false)!!, client, builder = { _, _ -> Request.Builder().url(url) }))
        }
        assertEquals(0, calls)
        assertNull(diagnostics.prepareServerAction(6, false))
    }

    @Test fun declaredAndChunkedOversizeBodiesAreBoundedAndNeverStored() {
        val (diagnostics, _) = ready()
        for (declared in listOf(true, false)) {
            val action = diagnostics.prepareServerAction(7, false)!!
            val content = " ".repeat(ServerTraceBatch.MAX_BODY + 1)
            val body: ResponseBody = if (declared) content.toResponseBody() else object : ResponseBody() {
                override fun contentType() = null
                override fun contentLength() = -1L
                override fun source(): BufferedSource = Buffer().writeUtf8(content)
            }
            val client = OkHttpClient.Builder().addInterceptor { chain ->
                Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(200).message("ignored").body(body).build()
            }.build()
            assertFalse(execute(action, client)); assertTrue(diagnostics.snapshot()!!.observations.serverDomains.isEmpty())
            assertEquals(MonitorError.PROTOCOL, diagnostics.snapshot()!!.serverRead.error)
        }
    }

    @Test fun nullExpiryRemainsUnavailableWithoutRotatingAndOldReadCannotFinishNewAction() {
        val (diagnostics, _) = ready(); val action = diagnostics.prepareServerAction(7, false)!!
        val empty = JSONObject(fixture(action.captureId)).put("events", org.json.JSONArray()).put("next", 0).put("expiresAt", JSONObject.NULL)
            .put("metrics", JSONObject().put("recorded", 0).put("dropped", 0).put("captureCostMs", 0))
        assertFalse(execute(action, client(body = { empty.toString() })))
        assertEquals(MonitorError.UNAVAILABLE, diagnostics.snapshot()!!.serverRead.error)
        val old = diagnostics.prepareServerAction(7, false)!!; var cancelled = 0; old.attach { cancelled++ }
        val replacement = diagnostics.prepareServerAction(7, false)!!
        assertEquals(1, cancelled); assertEquals(action.captureId, replacement.captureId)
        assertFalse(old.finished(MonitorError.NETWORK)); assertTrue(replacement.current())
        assertEquals(MonitorStatus.STARTED, diagnostics.snapshot()!!.serverRead.status)
    }

    @Test fun contradictoryDurationCannotAdvanceBookmarkOrBlockLaterValidRead() {
        val (diagnostics, _) = ready(); val initial = diagnostics.prepareServerAction(7, false)!!
        assertTrue(execute(initial, client(body = { fixture(initial.captureId) })))
        val bad = diagnostics.prepareServerAction(7, false)!!
        val malformed = JSONObject(fixture(bad.captureId)).put("next", 1000)
        malformed.getJSONArray("events").getJSONObject(0).put("startOffsetMs", 10).put("offsetMs", 20).put("durationMs", 1000)
        assertFalse(execute(bad, client(body = { malformed.toString() })))
        assertEquals(44L, diagnostics.snapshot()!!.observations.serverDomains.single().next)
        val recovery = diagnostics.prepareServerAction(7, false)!!
        val valid = JSONObject(fixture(recovery.captureId)).put("next", 45)
        valid.getJSONArray("events").getJSONObject(43).put("sequence", 45)
        valid.getJSONObject("metrics").put("recorded", 45)
        assertTrue(execute(recovery, client(body = { valid.toString() })))
        assertEquals(45L, diagnostics.snapshot()!!.observations.serverDomains.single().next)
        assertEquals(45L, diagnostics.snapshot()!!.observations.importedServerEvents)
    }

    @Test fun fifthClockIsCapacityWithoutEvictingBookmarksOrRecountingAnOldClock() {
        val (diagnostics, _) = ready()
        fun read(clock: String, next: Long = 44): Boolean {
            val action = diagnostics.prepareServerAction(7, false)!!
            val root = JSONObject(fixture(action.captureId)).put("clockId", clock).put("next", next)
            if (next == 45L) {
                root.getJSONArray("events").getJSONObject(43).put("sequence", 45)
                root.getJSONObject("metrics").put("recorded", 45)
            }
            return execute(action, client(body = { root.toString() }))
        }
        val clocks = ('a'..'e').map { it.toString().repeat(32) }
        for (clock in clocks.take(4)) assertTrue(read(clock))
        val before = diagnostics.snapshot()!!.observations
        assertFalse(read(clocks[4]))
        assertEquals(MonitorError.CAPACITY, diagnostics.snapshot()!!.serverRead.error)
        assertEquals(before, diagnostics.snapshot()!!.observations)
        assertTrue(read(clocks[0]))
        assertEquals(before, diagnostics.snapshot()!!.observations)
        assertTrue(read(clocks[0], 45))
        val after = diagnostics.snapshot()!!.observations
        assertEquals(177L, after.importedServerEvents)
        assertEquals(45L, after.serverDomains.single { it.clockId == clocks[0] }.next)
        assertEquals(4, after.serverDomains.size)
    }

    @Test fun deleteAcknowledgmentAllowsStructuralWhitespaceButNeverChangesStringsOrAcceptsTrailingValues() {
        val invalid = listOf("{\"clea red\":true}", "{\"cleared\":false}", "{\"cleared\":\"true\"}",
            "{\"cleared\":true,\"free\":\"PRIVATE\"}", "{\"cleared\":true}{}", "{\"cleared\":true}true",
            "{\"clear\\ted\":true}", "[true]", "{\"cleared\":true" )
        for (body in invalid) {
            val (diagnostics, _) = ready(); val action = diagnostics.prepareServerAction(7, true)!!
            assertFalse(body, execute(action, client(body = { body })))
            assertEquals(MonitorError.PROTOCOL, diagnostics.snapshot()!!.serverRead.error)
            assertFalse(diagnostics.snapshot().toString().contains("PRIVATE"))
        }
        val (diagnostics, _) = ready(); val action = diagnostics.prepareServerAction(7, true)!!
        assertTrue(execute(action, client(body = { " \r\n{ \t\"cleared\" : true \n} \t" })))
    }
}
