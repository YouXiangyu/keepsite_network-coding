package io.github.youxiangyu.memoverse.core

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class SyncEngineTest {
    private val capabilities = JSONObject("""{"processing":false,"cloudStorage":true,"deviceSync":true,"processingUnavailableReason":"fixture"}""")
    private fun ticket(id: String, version: Long = 1): JSONObject {
        val ref = JSONObject().put("assetId", "a".repeat(64)).put("sha256", "a".repeat(64)).put("size", 5)
            .put("mimeType", "image/png").put("width", 1).put("height", 1).put("cloudStatus", "saved").put("url", "/api/media/" + "a".repeat(64))
        return JSONObject().put("id", id).put("fields", Wire.fieldsJson(TicketFields(title = id)))
            .put("fieldSources", JSONObject(FIELD_NAMES.associateWith { "empty" })).put("createdAt", 1).put("updatedAt", 1)
            .put("version", version).put("inputVersion", 1).put("deletedAt", JSONObject.NULL).put("operationId", "operation-$id")
            .put("media", JSONObject().put("original", ref).put("poster", JSONObject.NULL))
            .put("processing", JSONObject().put("status", "not_started").put("error", JSONObject.NULL))
    }
    private fun snapshot(items: List<JSONObject>, cursor: Int = 0, total: Int = items.size, offset: Int = 0, token: String? = null, snapshot: String = "one") =
        JSONObject().put("serverId", "server").put("userId", "user").put("snapshotId", snapshot).put("cursor", cursor).put("total", total)
            .put("offset", offset).put("expiresAt", 600000).put("tickets", JSONArray(items)).put("nextPageToken", token ?: JSONObject.NULL).put("capabilities", capabilities)
    private fun changes(after: Int, items: List<JSONObject>, head: Int = after + items.size) = JSONObject().put("serverId", "server").put("userId", "user")
        .put("fromCursor", after).put("nextCursor", after + items.size).put("headCursor", head).put("hasMore", after + items.size < head)
        .put("changes", JSONArray(items)).put("capabilities", capabilities)

    @Test fun frozenPagesThenIncrementalWriteBarrierAndDeletion() {
        val reads = mutableListOf<String>()
        val responses = ArrayDeque(listOf(snapshot(listOf(ticket("a")), 2, 2, 0, "next"), snapshot(listOf(ticket("b")), 2, 2, 1),
            changes(2, listOf(JSONObject().put("cursor", 3).put("type", "upsert").put("ticket", ticket("a", 2)))),
            changes(3, listOf(JSONObject().put("cursor", 4).put("type", "delete").put("id", "b").put("version", 2).put("deletedAt", 5)))))
        val engine = SyncEngine("user") { path -> reads.add(path); responses.removeFirst() }
        val initial = engine.sync(Receipt("server", "write", 3))
        assertEquals(listOf("a", "b"), initial.tickets.map { it.id }); assertEquals(3L, initial.cursor)
        val next = engine.sync(Receipt("server", "delete", 4))
        assertEquals(listOf("a"), next.tickets.map { it.id }); assertEquals(4L, next.cursor)
        assertEquals(2, reads.count { it.startsWith("/api/sync/snapshot") })
        assertTrue(reads[1].endsWith("pageToken=next")); assertTrue(reads.last().endsWith("after=3"))
    }
    @Test fun mixedSnapshotIsDiscardedBeforePublishing() {
        val replies = ArrayDeque(listOf(snapshot(listOf(ticket("partial")), total = 2, token = "next"),
            snapshot(listOf(ticket("other")), total = 2, offset = 1, snapshot = "different"), snapshot(listOf(ticket("fresh"))), changes(0, emptyList())))
        lateinit var engine: SyncEngine
        engine = SyncEngine("user") { assertNull(engine.current); replies.removeFirst() }
        assertEquals(listOf("fresh"), engine.sync().tickets.map { it.id })
        assertTrue(replies.isEmpty())
    }
    @Test fun duplicateRowsAndWrongAccountFailWithoutPartialPublish() {
        for (bad in listOf(snapshot(listOf(ticket("same"), ticket("same"))), snapshot(emptyList()).put("userId", "another"))) {
            val engine = SyncEngine("user") { bad }
            assertThrows(ApiFailure::class.java) { engine.sync() }; assertNull(engine.current)
        }
    }
    @Test fun cursorGapRebuildsAndCannotSatisfyDifferentServerReceipt() {
        val replies = ArrayDeque(listOf(snapshot(emptyList()), changes(0, listOf(JSONObject().put("cursor", 2).put("type", "upsert").put("ticket", ticket("bad")))),
            snapshot(listOf(ticket("fresh")), cursor = 2), changes(2, emptyList())))
        val engine = SyncEngine("user") { replies.removeFirst() }
        assertEquals(2L, engine.sync().cursor)
        val mismatch = SyncEngine("user") { snapshot(emptyList()).put("serverId", "replacement") }
        assertThrows(ApiFailure::class.java) { mismatch.sync(Receipt("original", "write", 5)) }
        assertNull(mismatch.current)
    }
    @Test fun staleWatermarkCannotAcknowledgeCommittedWrite() {
        val replies = ArrayDeque(listOf(snapshot(emptyList(), cursor = 2), changes(2, emptyList())))
        val engine = SyncEngine("user") { replies.removeFirst() }
        assertThrows(ApiFailure::class.java) { engine.sync(Receipt("server", "write", 3)) }
        assertNull(engine.current)
    }
    @Test fun wireRejectsCoercedNumbersInvalidDatesAndUnsafeMedia() {
        assertThrows(ApiFailure::class.java) { Wire.number(JSONObject().put("n", "1"), "n") }
        assertThrows(ApiFailure::class.java) { Wire.validateFields(TicketFields(showtime = "2025-02-29T10:00")) }
        Wire.validateFields(TicketFields(showtime = "2024-02-29T10:00"))
        val body = ticket("a"); body.getJSONObject("media").getJSONObject("original").put("url", "https://bad.example/image")
        assertThrows(ApiFailure::class.java) { Wire.ticket(body) }
        assertTrue(HttpApi.validS3Url("https://bucket.s3.ap-northeast-1.amazonaws.com/key?X-Amz-Signature=fixture"))
        assertFalse(HttpApi.validS3Url("https://s3.amazonaws.com.evil.example/key"))
        assertFalse(HttpApi.validS3Url("https://user@s3.amazonaws.com/key"))
        assertFalse(HttpApi.validS3Url("http://s3.amazonaws.com/key"))
    }
    @Test fun commonsSourceUsesTheServerCanonicalFilenameEncoding() {
        val body = ticket("poster")
        val poster = JSONObject(body.getJSONObject("media").getJSONObject("original").toString())
        poster.put("source", JSONObject().put("provider", "wikimedia_commons")
            .put("sourceFile", "File:Safety Last! (1923)~poster.jpg")
            .put("sourcePage", "https://commons.wikimedia.org/wiki/File:Safety_Last!_(1923)~poster.jpg")
            .put("licenseName", "Public domain").put("licenseUrl", "https://creativecommons.org/publicdomain/mark/1.0/")
            .put("author", "First National").put("fetchedAt", 1))
        body.getJSONObject("media").put("poster", poster)
        assertEquals("File:Safety Last! (1923)~poster.jpg", Wire.ticket(body).poster!!.source!!.sourceFile)
        poster.getJSONObject("source").put("sourcePage", "https://other.example/redirect")
        assertThrows(ApiFailure::class.java) { Wire.ticket(body) }
    }
    @Test fun tmdbSourceRejectsUntrustedPagesAndImagePaths() {
        val source = JSONObject().put("provider", "tmdb").put("sourcePage", "https://www.themoviedb.org/movie/157336")
            .put("sourceFile", "/gEU2QniE6E77NI6lCU6MxlNBvIx.jpg").put("termsUrl", "https://www.themoviedb.org/api-terms-of-use").put("fetchedAt", 1)
        assertTrue(Wire.posterSource(source) is TmdbPosterSource)
        for ((key, value) in listOf("sourcePage" to "https://www.themoviedb.org/movie/01", "sourcePage" to "https://www.themoviedb.org.evil.test/movie/1", "sourceFile" to "/../../poster.jpg", "termsUrl" to "https://evil.test/terms")) {
            assertThrows(ApiFailure::class.java) { Wire.posterSource(JSONObject(source.toString()).put(key, value)) }
        }
    }
    @Test fun accountMethodsRequireConsistentLinkedEmail() {
        val methods = JSONObject("""{"password":{"linked":true,"email":"a@example.test"},"google":{"linked":false,"email":null}}""")
        assertEquals("a@example.test", Wire.accountMethods(methods).password.email)
        methods.getJSONObject("password").put("email", JSONObject.NULL)
        assertThrows(ApiFailure::class.java) { Wire.accountMethods(methods) }
    }
}
