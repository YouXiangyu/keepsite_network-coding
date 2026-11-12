package io.github.youxiangyu.keepsite.core

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.util.concurrent.CancellationException

class MediaObservationTest {
    @get:Rule val temporary = TemporaryFolder()
    private val bytes = byteArrayOf(1, 2, 3, 4)
    private val ref = MediaRef(sha256(bytes), sha256(bytes), 4, "image/png", 1, 1)
    private fun ready(): Pair<NetworkDiagnostics, NetworkMonitor.Account> {
        val diagnostics = NetworkDiagnostics(); val account = diagnostics.openAccount(3)!!
        diagnostics.setDetailed(true); return diagnostics to account
    }
    private fun auth(transport: String = "s3") = JSONObject().put("sha256", ref.sha256).put("size", ref.size).put("mimeType", ref.mimeType)
        .put("transport", transport).put("url", if (transport == "s3") "https://bucket.s3.amazonaws.com/PRIVATE?signature=SECRET" else ref.url).put("expiresAt", 1000)

    @Test fun actualCacheReadIsVerifiedAndRecordedWithoutUsingDeclaredSizeAsReadBytes() {
        val (diagnostics, account) = ready(); val parent = diagnostics.media(account, 3, ref.size)!!
        val cache = FileCache(temporary.newFolder(), 32) { raw, media -> require(raw.contentEquals(bytes) && media.size == raw.size.toLong()) }
        cache.setRefs(listOf(ref)); assertTrue(cache.commit(ref, bytes, cache.token()))
        val attempt = parent.attempt(MonitorRoute.CACHE, MonitorProtocol.CACHE)!!
        assertNotNull(cache.file(ref, attempt)); assertEquals(4L, attempt.payloadBytes())
        attempt.complete(verified = true); attempt.close(); parent.cacheValidated(); parent.complete(attempt.payloadBytes(), MonitorRoute.CACHE); parent.close()
        val state = diagnostics.snapshot()!!
        assertEquals(4L, state.media.bytes); assertEquals(true, state.media.verified)
        assertEquals(MonitorRoute.CACHE, state.media.route); assertEquals(0, state.observations.active)
        assertEquals(MonitorProtocol.CACHE, state.observations.events.last { it.event == MonitorEvent.MEDIA_READ }.protocol)
        assertEquals(MonitorProtocol.MEDIA, state.observations.events.first { it.event == MonitorEvent.MEDIA_READ }.protocol)
        cache.close()
    }

    @Test fun corruptCacheIsAnIntegrityFailureAttemptAndDoesNotClaimParentSuccess() {
        val (diagnostics, account) = ready(); val parent = diagnostics.media(account, 3, ref.size)!!
        val directory = temporary.newFolder(); val cache = FileCache(directory, 32) { raw, _ -> require(raw.contentEquals(bytes)) }
        cache.setRefs(listOf(ref)); File(directory, ref.assetId).writeBytes(byteArrayOf(9, 9, 9, 9))
        val attempt = parent.attempt(MonitorRoute.CACHE, MonitorProtocol.CACHE)!!
        assertNull(cache.file(ref, attempt)); attempt.close()
        val row = diagnostics.snapshot()!!.observations.events.last()
        assertEquals(4L, row.values.bytes); assertEquals(MonitorError.INTEGRITY, row.values.error)
        assertNull(diagnostics.snapshot()!!.media.lastSuccessAt)
        parent.failed(MonitorError.UNAVAILABLE); parent.close(); cache.close()
        assertEquals(0, diagnostics.snapshot()!!.observations.active)
    }

    @Test fun s3ForbiddenReauthorizesThenPartialFailureFallsBackToProxyWithSeparateActualCounts() {
        val (diagnostics, account) = ready(); val parent = diagnostics.media(account, 3, 4)!!
        var authorizations = 0; var reads = 0
        val result = downloadMedia(ref, parent, { authorizations++; auth() }, { _, signed, attempt ->
            reads++
            when (reads) {
                1 -> { assertTrue(signed); attempt!!.read(0); throw ApiFailure(403, message = "isolated forbidden") }
                2 -> { assertTrue(signed); attempt!!.read(2); throw ApiFailure(502, message = "isolated partial") }
                else -> { assertFalse(signed); attempt!!.read(4); bytes }
            }
        }, {}, { 0 })
        assertEquals(2, authorizations); assertEquals(3, reads); assertSame(bytes, result.first); assertEquals("server", result.second)
        parent.validating { require(result.first.contentEquals(bytes)) }; parent.complete(result.first.size.toLong(), MonitorRoute.SERVER_PROXY); parent.close()
        val state = diagnostics.snapshot()!!
        val attempts = state.observations.events.filter { it.event == MonitorEvent.MEDIA_ATTEMPT && it.status in setOf(MonitorStatus.FAILED, MonitorStatus.COMPLETED) }
        assertEquals(listOf(0L, 2L, 4L), attempts.map { it.values.bytes })
        assertEquals(4L, state.media.bytes); assertEquals(4L, state.media.expectedBytes); assertEquals(0, state.observations.active)
        assertFalse(state.observations.events.toString().contains("SECRET")); assertFalse(state.observations.events.toString().contains("PRIVATE"))
    }

    @Test fun validS3AndExpiredAuthorizationsRetainTheOriginalRouteAlgorithm() {
        for (expired in listOf(false, true)) {
            val (diagnostics, account) = ready(); val parent = diagnostics.media(account, 3, 4)!!
            var authorizationCount = 0; var reads = 0
            val result = downloadMedia(ref, parent, { authorizationCount++; auth().put("expiresAt", if (expired) 0 else 1000) }, { _, signed, attempt ->
                reads++; assertEquals(!expired, signed); attempt!!.read(4); bytes
            }, {}, { 0 })
            assertEquals(if (expired) 2 else 1, authorizationCount); assertEquals(1, reads)
            assertEquals(if (expired) "server" else "s3", result.second)
            parent.validating { Unit }; parent.complete(4, if (expired) MonitorRoute.SERVER_PROXY else MonitorRoute.DIRECT_S3); parent.close()
            assertEquals(0, diagnostics.snapshot()!!.observations.active)
            assertEquals(if (expired) MonitorProtocol.HTTP else MonitorProtocol.S3,
                diagnostics.snapshot()!!.observations.events.last { it.event == MonitorEvent.MEDIA_READ }.protocol)
        }
    }

    @Test fun cancellationAndIntegrityKeepOriginalThrowableAndNeverBecomeSuccessfulMedia() {
        val (diagnostics, account) = ready(); val parent = diagnostics.media(account, 3, 4)!!
        val original = CancellationException("private cancellation")
        assertSame(original, assertThrows(CancellationException::class.java) {
            downloadMedia(ref, parent, { auth() }, { _, _, attempt -> attempt!!.read(2); throw original }, {}, { 0 })
        })
        parent.failed(MonitorError.CANCELLED); parent.close()
        assertEquals(MonitorProtocol.MEDIA, diagnostics.snapshot()!!.observations.events.last { it.event == MonitorEvent.MEDIA_READ }.protocol)
        assertNull(diagnostics.snapshot()!!.media.lastSuccessAt); assertEquals(0, diagnostics.snapshot()!!.observations.active)
        val broken = diagnostics.media(account, 3, 4)!!; val failure = IllegalArgumentException("private hash failure")
        assertSame(failure, assertThrows(IllegalArgumentException::class.java) { broken.validating { throw failure } })
        broken.close()
        assertEquals(MonitorError.INTEGRITY, diagnostics.snapshot()!!.media.error)
        assertNull(diagnostics.snapshot()!!.media.lastSuccessAt); assertEquals(0, diagnostics.snapshot()!!.observations.active)
    }

    @Test fun clearAndNewAccountFenceOldMediaWithoutStealingNewActiveCounts() {
        val (diagnostics, account) = ready()
        val old = diagnostics.media(account, 3, 4)!!; val span = old.attempt(MonitorRoute.DIRECT_S3, MonitorProtocol.S3)!!
        diagnostics.clearHistory(); span.read(4); span.complete(); old.validating { Unit }; old.complete(4, MonitorRoute.DIRECT_S3); old.close()
        assertTrue(diagnostics.snapshot()!!.observations.events.isEmpty())
        val beforeReplacement = diagnostics.media(account, 3, 4)!!
        val next = diagnostics.openAccount(3)!!
        assertNull(diagnostics.media(account, 3, 4))
        val current = diagnostics.media(next, 3, 4)!!
        beforeReplacement.failed(MonitorError.CANCELLED); beforeReplacement.close()
        assertEquals(1, diagnostics.snapshot()!!.media.active)
        current.failed(MonitorError.CANCELLED); current.close(); assertEquals(0, diagnostics.snapshot()!!.media.active)
        assertEquals(0, diagnostics.snapshot()!!.observations.active)
    }
}
