package io.github.youxiangyu.memoverse.core

import android.content.Context
import android.graphics.Bitmap
import android.net.Uri
import android.os.SystemClock
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import io.github.youxiangyu.memoverse.BuildConfig
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.Closeable
import java.io.File
import java.util.UUID
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/** Real BFF, filesystem and libwebrtc against the explicitly owned loopback fixture. */
@RunWith(AndroidJUnit4::class)
class NativeInteropInstrumentedTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context: Context get() = instrumentation.targetContext
    private val origin = "http://127.0.0.1:4180"
    private val password = "TestTicket123!"

    private fun main(action: () -> Unit) = instrumentation.runOnMainSync(action)
    private fun await(label: String, timeoutMs: Long = 30_000, predicate: () -> Boolean) {
        require(timeoutMs in 1..60_000)
        val deadline = SystemClock.elapsedRealtime() + timeoutMs
        while (SystemClock.elapsedRealtime() < deadline) {
            if (predicate()) return
            Thread.sleep(50)
        }
        assertTrue("Timed out waiting for $label", predicate())
    }
    private fun ready(repository: AppRepository) {
        main { repository.setForeground(true) }
        await("initial authentication check") { repository.snapshot().session != null }
    }
    private fun login(repository: AppRepository, who: String) {
        if (repository.snapshot().session?.authenticated == true) logout(repository)
        main { repository.login("$who@example.test", password) }
        await("$who account snapshot", 60_000) {
            val state = repository.snapshot()
            state.session?.email == "$who@example.test" && state.ready && state.busy.isEmpty()
        }
        assertEquals("", repository.snapshot().error)
    }
    private fun logout(repository: AppRepository) {
        main { repository.logout() }
        await("anonymous account") {
            val state = repository.snapshot()
            state.session?.authenticated != true && state.tickets.isEmpty() && state.busy.isEmpty()
        }
    }
    private fun clearCache(repository: AppRepository) {
        main { repository.clearCache() }
        await("empty owned cache") {
            val state = repository.snapshot()
            state.busy.isEmpty() && state.cache.files == 0 && state.cache.bytes == 0L
        }
        assertEquals("", repository.snapshot().cache.error)
    }
    private fun load(repository: AppRepository, media: MediaRef, leases: MutableList<Closeable>): MediaLoad {
        val result = AtomicReference<MediaLoad?>()
        main { leases.add(repository.loadMedia(media) { if (it.status != "loading") result.set(it) }) }
        await("media result", 60_000) { result.get() != null }
        return result.get()!!
    }
    private fun verifyCached(load: MediaLoad, ref: MediaRef, source: String): ByteArray {
        assertEquals("Media must use the requested transport", source, load.source)
        assertEquals("cached", load.status)
        val file = load.file ?: throw AssertionError("Verified cache file missing")
        val bytes = file.readBytes()
        assertEquals(ref.size, file.length())
        assertEquals(ref.sha256, sha256(bytes))
        ImageFiles.validate(bytes, ref)
        return bytes
    }
    private fun assertAccountCannotRead(ref: MediaRef) {
        HttpApi(origin, SecretStore(context)).use { reader ->
            reader.session = Wire.session(reader.json("/api/auth/session", authenticated = false))
            assertTrue(reader.session!!.authenticated)
            for (path in listOf(ref.url, "${ref.url}/download")) {
                try { reader.json(path); fail("Another account must not read the old media") }
                catch (error: ApiFailure) { assertEquals(404, error.status) }
            }
        }
    }

    private inner class Coordination : Closeable {
        private val token = InstrumentationRegistry.getArguments().getString("runToken")
            ?.takeIf { Regex("[a-f0-9]{32}").matches(it) }
            ?: throw AssertionError("An owned fixture runToken instrumentation argument is required")
        private val client = OkHttpClient.Builder().connectTimeout(5, TimeUnit.SECONDS)
            .readTimeout(5, TimeUnit.SECONDS).callTimeout(10, TimeUnit.SECONDS)
            .followRedirects(false).retryOnConnectionFailure(false).build()
        fun request(path: String, body: JSONObject? = null): JSONObject {
            require(path.startsWith("/__test/"))
            val request = Request.Builder().url(origin + path).header("Origin", origin)
                .header("X-MemoVerse-Fixture", "native-browser-fixture")
                .header("X-MemoVerse-Fixture-Run", token)
            if (body != null) request.post(body.toString().toRequestBody("application/json".toMediaType()))
            return client.newCall(request.build()).execute().use { response ->
                assertEquals("Fixture coordination HTTP status", 200, response.code)
                val raw = response.body?.string() ?: throw AssertionError("Missing fixture coordination response")
                assertTrue("Fixture response must remain bounded", raw.length <= 256 * 1024)
                JSONObject(raw)
            }
        }
        fun report(phase: String, data: JSONObject) {
            request("/__test/progress", JSONObject().put("phase", phase).put("data", data))
        }
        fun phase(name: String): JSONObject {
            val result = AtomicReference<JSONObject?>()
            await("fixture phase $name", 60_000) {
                request("/__test/progress").getJSONObject("phases").optJSONObject(name)?.also(result::set) != null
            }
            return result.get()!!
        }
        override fun close() {
            client.dispatcher.cancelAll(); client.connectionPool.evictAll(); client.dispatcher.executorService.shutdown()
        }
    }

    @Test fun nativeWebRoundTripAndAccountIsolation() {
        assertEquals("This test must use the owned loopback debug build", origin, BuildConfig.API_BASE_URL)
        val leases = CopyOnWriteArrayList<Closeable>()
        val repository = AppRepository(context)
        try {
            Coordination().use { coordination ->
                val web = coordination.phase("web_ready")
                ready(repository); login(repository, "alice")
                val wantedAsset = web.optString("assetId")
                val ref = repository.snapshot().tickets.firstOrNull { wantedAsset.isEmpty() || it.original.assetId == wantedAsset }?.original
                    ?: throw AssertionError("The Web participant must bootstrap a synthetic original")
                if (web.has("assetId")) assertEquals(ref.assetId, web.getString("assetId"))
                clearCache(repository)
                main { repository.setPeerEnabled(true) }
                await("native peer registration", 30_000) { repository.snapshot().peer.registered }
                val received = load(repository, ref, leases)
                verifyCached(received, ref, "peer")
                coordination.report("native_received", JSONObject().put("assetId", ref.assetId)
                    .put("sha256", ref.sha256).put("size", ref.size).put("source", received.source))

                val webReceived = coordination.phase("web_received")
                assertEquals(ref.assetId, webReceived.getString("assetId"))
                assertEquals(ref.sha256, webReceived.getString("sha256"))
                await("native send completion", 15_000) {
                    repository.snapshot().peer.transfers.any { it.assetId == ref.assetId && it.direction == "send" && it.status == "complete" }
                }
                coordination.report("native_sent", JSONObject().put("assetId", ref.assetId).put("sha256", ref.sha256)
                    .put("size", ref.size).put("status", "complete"))

                leases.forEach(Closeable::close); leases.clear()
                logout(repository); login(repository, "bob")
                assertTrue(repository.snapshot().tickets.isEmpty())
                assertEquals("error", load(repository, ref, leases).status)
                assertAccountCannotRead(ref)
                assertTrue(repository.snapshot().peer.transfers.none { it.assetId == ref.assetId })
                main { repository.setForeground(false) }
                await("background resource withdrawal") {
                    val state = repository.snapshot()
                    !state.connected && !state.peer.registered && state.peer.sending == 0 && state.peer.receiving == 0
                }
                main { repository.setForeground(true) }
                await("foreground reconciliation", 30_000) {
                    val state = repository.snapshot()
                    state.connected && state.ready && state.tickets.isEmpty() && state.session?.email == "bob@example.test"
                }
                logout(repository); login(repository, "alice")
                assertTrue(repository.snapshot().tickets.any { it.original == ref })
                main { repository.setPeerEnabled(false) }
                clearCache(repository)
                val cancelledResults = CopyOnWriteArrayList<MediaLoad>()
                coordination.request("/__test/media-gate", JSONObject().put("assetId", ref.assetId).put("action", "hold"))
                try {
                    main { leases.add(repository.loadMedia(ref) { cancelledResults.add(it) }) }
                    await("real HTTP media request at gate", 15_000) {
                        val started = coordination.request("/__test/progress").getJSONArray("gateStarted")
                        (0 until started.length()).any { started.getString(it) == ref.assetId }
                    }
                    main { repository.setForeground(false) }
                    await("background cancellation of the active HTTP load") {
                        val state = repository.snapshot()
                        !state.connected && !state.peer.registered && state.cache.files == 0
                    }
                } finally {
                    coordination.request("/__test/media-gate", JSONObject().put("assetId", ref.assetId).put("action", "release"))
                }
                assertTrue(cancelledResults.none { it.status != "loading" })
                assertEquals(0, repository.snapshot().cache.files)
                main { repository.setForeground(true) }
                await("foreground reconciliation after HTTP cancellation", 30_000) {
                    val state = repository.snapshot()
                    state.connected && state.ready && state.session?.email == "alice@example.test"
                }
                verifyCached(load(repository, ref, leases), ref, "server")
                main { /* Drain already dispatched UI callbacks before checking the old lifetime. */ }
                assertTrue("A cancelled generation must not deliver late media results", cancelledResults.none { it.status != "loading" })
                coordination.report("native_complete", JSONObject().put("accountIsolation", true)
                    .put("foregroundCatchup", true).put("backgroundInFlightCancelled", true)
                    .put("httpRecovery", true).put("sha256", ref.sha256))
            }
        } finally {
            leases.forEach(Closeable::close)
            main { repository.close() }
        }
    }

    @Test fun realCrudRetainsDraftIdsAndEnforcesAccountIsolation() {
        assertEquals(origin, BuildConfig.API_BASE_URL)
        var repository = AppRepository(context)
        val photo = File.createTempFile("native-crud-", ".png", context.cacheDir)
        val uniqueTitle = "Native instrumented ${UUID.randomUUID()}"
        try {
            Bitmap.createBitmap(32, 48, Bitmap.Config.ARGB_8888).let { bitmap ->
                try { bitmap.eraseColor(android.graphics.Color.rgb(38, 96, 142)); photo.outputStream().use { assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)) } }
                finally { bitmap.recycle() }
            }
            ready(repository); login(repository, "bob")
            main { repository.setPeerEnabled(false); repository.newDraft() }
            await("new draft") { repository.snapshot().draft != null }
            val fields = TicketFields(uniqueTitle, "Synthetic Cinema", "Hall 1", "A7", "2026-10-22T19:30")
            main { repository.setDraftFields(fields); repository.selectPhoto(Uri.fromFile(photo)) }
            await("durable photo draft") {
                val state = repository.snapshot()
                state.draft?.fields == fields && state.draft?.hasPhoto == true && state.busy.isEmpty()
            }
            val before = repository.snapshot().draft!!
            main { repository.close() }
            repository = AppRepository(context)
            ready(repository)
            await("restored draft and account") { repository.snapshot().ready && repository.snapshot().draft != null }
            val restored = repository.snapshot().draft!!
            assertEquals(before.operationId, restored.operationId)
            assertEquals(before.uploadId, restored.uploadId)
            assertEquals(fields, restored.fields)
            assertTrue(restored.hasPhoto)
            // A local preparation error must not persist confirmation for an upload never sent.
            val restoredPhoto = restored.previewFile ?: throw AssertionError("Restored draft photo missing")
            val validPhoto = restoredPhoto.readBytes()
            restoredPhoto.writeBytes(byteArrayOf(1, 2, 3))
            main { repository.saveDraft() }
            await("local photo failure leaves the draft replaceable") {
                val state = repository.snapshot()
                state.busy.isEmpty() && state.error.isNotEmpty() && state.draft?.pendingConfirmation == false
            }
            main { repository.selectPhoto(Uri.fromFile(photo)) }
            await("replace photo after failed preparation") {
                val state = repository.snapshot()
                state.busy.isEmpty() && state.draft?.uploadId != restored.uploadId && state.draft?.hasPhoto == true
            }
            assertArrayEquals(validPhoto, repository.snapshot().draft!!.previewFile!!.readBytes())
            val createCursor = repository.snapshot().cursor
            main { repository.saveDraft() }
            await("created ticket write barrier", 60_000) {
                val state = repository.snapshot()
                state.draft == null && state.busy.isEmpty() && state.cursor > createCursor && state.tickets.any { it.fields.title == uniqueTitle }
            }
            val created = repository.snapshot().tickets.single { it.fields.title == uniqueTitle }
            assertEquals(before.operationId, created.operationId)
            assertEquals(fields, created.fields)
            main { repository.editDraft(created.id) }
            await("edit draft") { repository.snapshot().draft?.ticketId == created.id }
            main { repository.setDraftFields(fields.copy(hall = "Hall 2")) }
            await("edited field") { repository.snapshot().draft?.fields?.hall == "Hall 2" }
            val editId = repository.snapshot().draft!!.operationId
            val editCursor = repository.snapshot().cursor
            main { repository.saveDraft() }
            await("updated ticket write barrier", 60_000) {
                val state = repository.snapshot()
                state.draft == null && state.busy.isEmpty() && state.cursor > editCursor && state.tickets.any { it.id == created.id && it.version > created.version }
            }
            val edited = repository.snapshot().tickets.single { it.id == created.id }
            assertEquals(editId, edited.operationId)
            assertEquals(fields.copy(hall = "Hall 2"), edited.fields)
            assertEquals(created.original, edited.original)
            logout(repository); login(repository, "alice")
            assertTrue(repository.snapshot().tickets.none { it.id == created.id })
            assertAccountCannotRead(created.original)
            logout(repository); login(repository, "bob")
            main { repository.editDraft(created.id) }
            await("draft before concurrent deletion") { repository.snapshot().draft?.ticketId == created.id }
            main { repository.setDraftFields(edited.fields.copy(hall = "Deleted while editing")) }
            await("changed draft before concurrent deletion") { repository.snapshot().draft?.fields?.hall == "Deleted while editing" }
            val rejectedDraft = repository.snapshot().draft!!
            val deletionCursor = repository.snapshot().cursor
            main { repository.deleteTicket(created.id) }
            await("deleted ticket write barrier", 60_000) {
                val state = repository.snapshot()
                state.busy.isEmpty() && state.cursor > deletionCursor && state.tickets.none { it.id == created.id }
            }
            assertAccountCannotRead(created.original)
            main { repository.saveDraft() }
            await("deleted ticket rejects save without trapping the draft") {
                val state = repository.snapshot()
                state.busy.isEmpty() && state.error.isNotEmpty() && state.draft?.pendingConfirmation == false
            }
            assertEquals("电影票不存在或已删除。", repository.snapshot().error)
            assertEquals(rejectedDraft.operationId, repository.snapshot().draft!!.operationId)
            HttpApi(origin, SecretStore(context)).use { reader ->
                reader.session = Wire.session(reader.json("/api/auth/session", authenticated = false))
                assertFalse(reader.json("/api/operations/${rejectedDraft.operationId}").getBoolean("applied"))
            }
            main { repository.close() }
            repository = AppRepository(context)
            ready(repository)
            await("rejected draft restored without pending confirmation") {
                val state = repository.snapshot()
                state.ready && state.draft?.operationId == rejectedDraft.operationId && state.draft?.pendingConfirmation == false
            }
            assertEquals(rejectedDraft.fields, repository.snapshot().draft!!.fields)
            main { repository.discardDraft() }
            await("rejected draft can be discarded") { repository.snapshot().draft == null }
            main { repository.newDraft() }
            await("new draft after deleted ticket rejection") { repository.snapshot().draft != null }
            assertNotEquals(rejectedDraft.operationId, repository.snapshot().draft!!.operationId)
            assertNull(repository.snapshot().draft!!.ticketId)
            main { repository.discardDraft() }
            await("new empty draft discarded") { repository.snapshot().draft == null }
            logout(repository)
            val triggered = AtomicBoolean()
            val listener = repository.subscribe { state ->
                if (state.session?.authenticated == true && triggered.compareAndSet(false, true)) repository.logout()
            }
            try {
                main { repository.login("bob@example.test", password) }
                await("immediate login cancellation") { triggered.get() }
                await("anonymous final state after login/logout overlap") {
                    val state = repository.snapshot()
                    state.session?.authenticated != true && state.tickets.isEmpty() && !state.peer.registered && state.busy.isEmpty()
                }
                HttpApi(origin, SecretStore(context)).use { reader ->
                    assertFalse(Wire.session(reader.json("/api/auth/session", authenticated = false)).authenticated)
                }
                assertTrue(repository.snapshot().session?.authenticated != true)
            } finally { listener.close() }
        } finally {
            main { repository.close() }
            assertTrue("Delete only this test's selected synthetic photo", !photo.exists() || photo.delete())
        }
    }
}
