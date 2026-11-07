package io.github.youxiangyu.keepsite.core

import org.json.JSONArray
import org.json.JSONObject
import org.json.JSONTokener
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction

enum class ServerTraceEventType(val wire: String, val protocol: MonitorProtocol) {
    HTTP_REQUEST("http.request", MonitorProtocol.HTTP), SQL_COMMIT("sql.commit", MonitorProtocol.SQL), SQL_ROLLBACK("sql.rollback", MonitorProtocol.SQL),
    REDIS_SNAPSHOT_READ("redis.snapshot.read", MonitorProtocol.REDIS), REDIS_SNAPSHOT_WRITE("redis.snapshot.write", MonitorProtocol.REDIS),
    REDIS_STREAM_PUBLISH("redis.stream.publish", MonitorProtocol.REDIS), REDIS_STREAM_RECEIVE("redis.stream.receive", MonitorProtocol.REDIS),
    REDIS_STREAM_ACK("redis.stream.ack", MonitorProtocol.REDIS), REDIS_PUBSUB_PUBLISH("redis.pubsub.publish", MonitorProtocol.REDIS),
    REDIS_PUBSUB_RECEIVE("redis.pubsub.receive", MonitorProtocol.REDIS), WS_READY("ws.ready", MonitorProtocol.WS), WS_HINT("ws.hint", MonitorProtocol.WS),
    P2P_SIGNALING("p2p.signaling", MonitorProtocol.P2P), S3_OPEN("s3.open", MonitorProtocol.S3), S3_READ("s3.read", MonitorProtocol.S3),
    S3_HEAD("s3.head", MonitorProtocol.S3), S3_PUT("s3.put", MonitorProtocol.S3), WORKER_PREPARE("worker.prepare", MonitorProtocol.WORKER),
    WORKER_OCR("worker.ocr", MonitorProtocol.WORKER), WORKER_POSTER("worker.poster", MonitorProtocol.WORKER),
    WORKER_PUBLISH("worker.publish", MonitorProtocol.WORKER), WORKER_RETRY("worker.retry", MonitorProtocol.WORKER)
}
enum class ServerTraceStatus { STARTED, COMPLETED, FAILED, CANCELLED, RETRYING }
enum class ServerTraceError { OPERATION_FAILED, CANCELLED, UNAVAILABLE, TIMEOUT, RETRY_SCHEDULED }
@ConsistentCopyVisibility
data class ServerTraceEvent internal constructor(val sequence: Long, val timestamp: Long, val offsetMs: Double,
    val startOffsetMs: Double?, val durationMs: Double, val type: ServerTraceEventType, val status: ServerTraceStatus,
    val traceId: String?, val spanId: String, val parentSpanId: String?, val bytes: Long?, val attempt: Long?,
    val error: ServerTraceError?, val coalesced: Boolean?)
data class ServerTraceMetrics(val recorded: Long, val dropped: Long, val captureCostMs: Double)
@ConsistentCopyVisibility
data class ServerTraceDomain internal constructor(val captureId: String, val clockId: String, val next: Long, val dropped: Long,
    val expiresAt: Long?, val instrumented: Set<MonitorProtocol>, val metrics: ServerTraceMetrics, val events: List<ServerTraceEvent>)

/** Only this bounded, shallow decoder can mint a server batch. No raw JSON survives it. */
class ServerTraceBatch private constructor(internal val domain: ServerTraceDomain) {
    companion object {
        const val MAX_BODY = 256 * 1024
        const val MAX_DEPTH = 8
        private const val SAFE = 9_007_199_254_740_991L
        private val protocols = ServerTraceEventType.entries.map { it.protocol }.toSet()
        internal fun identifier(value: String) = value.matches(Regex("[0-9a-f]{32}")) && value != "0".repeat(32)
        private fun keys(value: JSONObject, required: Set<String>, optional: Set<String> = emptySet()) {
            val actual = value.keys().asSequence().toSet()
            require(actual.containsAll(required) && actual.all { it in required || it in optional })
        }
        private fun number(value: Any?): Double {
            require(value is Number)
            return value.toDouble().also { require(it.isFinite() && it >= 0 && it <= SAFE.toDouble()) }
        }
        private fun integer(value: Any?, max: Long = SAFE): Long {
            require(value is Number)
            return value.toString().toBigDecimal().longValueExact().also { require(it in 0..max) }
        }
        private fun nullableId(value: Any?): String? {
            if (value === JSONObject.NULL) return null
            return (value as? String)?.also { require(identifier(it)) } ?: error("id")
        }
        private fun shallow(text: String) {
            val stack = CharArray(MAX_DEPTH)
            var depth = 0; var quoted = false; var escaped = false
            for (c in text) {
                if (quoted) {
                    if (escaped) escaped = false else if (c == '\\') escaped = true else if (c == '"') quoted = false
                } else when (c) {
                    '"' -> quoted = true
                    '{', '[' -> { require(depth < MAX_DEPTH); stack[depth++] = c }
                    '}', ']' -> { require(depth > 0 && stack[--depth] == if (c == '}') '{' else '[') }
                    else -> require(c in " \t\r\n,:0123456789.-+eEtruefalsn") // Reject lenient JSON comments/single quotes before its parser.
                }
            }
            require(!quoted && !escaped && depth == 0)
        }
        fun parse(raw: ByteArray, captureId: String): ServerTraceBatch? = diagnostic(null) {
            require(raw.size <= MAX_BODY && identifier(captureId))
            val text = Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(raw)).toString()
            shallow(text)
            val tokener = JSONTokener(text)
            val root = tokener.nextValue() as? JSONObject ?: error("object")
            require(tokener.nextClean() == '\u0000')
            keys(root, setOf("v", "captureId", "clockId", "events", "next", "dropped", "capacity", "retentionMs", "expiresAt", "instrumented", "metrics"))
            require(integer(root.get("v")) == 1L && root.get("captureId") == captureId)
            val clock = root.get("clockId") as? String ?: error("clock")
            require(identifier(clock) && integer(root.get("capacity")) == 200L && integer(root.get("retentionMs")) == 600000L)
            val next = integer(root.get("next")); val dropped = integer(root.get("dropped"))
            val expiry = root.get("expiresAt").let { if (it === JSONObject.NULL) null else integer(it) }
            val installed = root.getJSONArray("instrumented")
            require(installed.length() <= protocols.size)
            val instrumented = (0 until installed.length()).map { i ->
                val name = installed.get(i) as? String ?: error("protocol")
                protocols.single { it.name.lowercase() == name }
            }.also { require(it.distinct().size == it.size) }.toSet()
            val metrics = root.getJSONObject("metrics")
            keys(metrics, setOf("recorded", "dropped", "captureCostMs"))
            val counts = ServerTraceMetrics(integer(metrics.get("recorded")), integer(metrics.get("dropped")), number(metrics.get("captureCostMs")))
            require(counts.dropped == dropped)
            val array = root.getJSONArray("events"); require(array.length() <= NetworkMonitor.MAX_EVENTS)
            var last = 0L; var size = 0
            val events = (0 until array.length()).map { i ->
                val row = array.getJSONObject(i)
                keys(row, setOf("sequence", "timestamp", "offsetMs", "durationMs", "origin", "protocol", "event", "status", "traceId", "spanId", "parentSpanId"),
                    setOf("startOffsetMs", "bytes", "attempt", "errorCode", "coalesced"))
                val event = ServerTraceEventType.entries.single { it.wire == row.get("event") }
                require(row.get("origin") == "server" && row.get("protocol") == event.protocol.name.lowercase())
                val sequence = integer(row.get("sequence")); require(sequence > last && sequence <= next); last = sequence
                val offset = number(row.get("offsetMs")); val start = if (row.has("startOffsetMs")) number(row.get("startOffsetMs")) else null
                require(start == null || start <= offset)
                val duration = number(row.get("durationMs"))
                require(start == null || duration <= offset - start + 0.001) // Backend rounds duration to three decimals; observe is at/after completion.
                val trace = nullableId(row.get("traceId")); val span = nullableId(row.get("spanId")) ?: error("span")
                val parent = nullableId(row.get("parentSpanId"))
                val coalesced = if (row.has("coalesced")) row.get("coalesced") as? Boolean ?: error("coalesced") else null
                require(coalesced != true || trace == null && parent == null)
                val value = ServerTraceEvent(sequence, integer(row.get("timestamp")), offset, start, duration, event,
                    ServerTraceStatus.entries.single { it.name.lowercase() == row.get("status") }, trace, span, parent,
                    if (row.has("bytes")) integer(row.get("bytes")) else null, if (row.has("attempt")) integer(row.get("attempt"), 1_000_000) else null,
                    if (row.has("errorCode")) ServerTraceError.entries.single { it.name.lowercase() == row.get("errorCode") } else null, coalesced)
                val bytes = eventJson(value).toString().toByteArray(Charsets.UTF_8).size
                require(bytes <= NetworkMonitor.MAX_EVENT_BYTES); size += bytes; require(size <= NetworkMonitor.MAX_HISTORY_BYTES)
                value
            }
            require(counts.recorded >= events.size && counts.recorded <= next)
            require(expiry != null || events.isEmpty() && next == 0L && dropped == 0L && counts.recorded == 0L && counts.captureCostMs == 0.0)
            ServerTraceBatch(ServerTraceDomain(captureId, clock, next, dropped, expiry, instrumented, counts, events))
        }
        /** Reconstruct fixed fields for export; never serialize a response or arbitrary object. */
        internal fun eventJson(event: ServerTraceEvent): JSONObject = JSONObject().put("sequence", event.sequence).put("timestamp", event.timestamp)
            .put("offsetMs", event.offsetMs).put("durationMs", event.durationMs).put("origin", "server")
            .put("protocol", event.type.protocol.name.lowercase()).put("event", event.type.wire).put("status", event.status.name.lowercase())
            .put("traceId", event.traceId ?: JSONObject.NULL).put("spanId", event.spanId).put("parentSpanId", event.parentSpanId ?: JSONObject.NULL).also {
                event.startOffsetMs?.let { value -> it.put("startOffsetMs", value) }; event.bytes?.let { value -> it.put("bytes", value) }
                event.attempt?.let { value -> it.put("attempt", value) }; event.error?.let { value -> it.put("errorCode", value.name.lowercase()) }
                event.coalesced?.let { value -> it.put("coalesced", value) }
            }
        internal fun domainJson(domain: ServerTraceDomain) = JSONObject().put("v", 1).put("captureId", domain.captureId).put("clockId", domain.clockId)
            .put("next", domain.next).put("dropped", domain.dropped).put("capacity", 200).put("retentionMs", 600000)
            .put("expiresAt", domain.expiresAt ?: JSONObject.NULL).put("instrumented", JSONArray(domain.instrumented.map { it.name.lowercase() }.sorted()))
            .put("metrics", JSONObject().put("recorded", domain.metrics.recorded).put("dropped", domain.metrics.dropped).put("captureCostMs", domain.metrics.captureCostMs))
            .put("events", JSONArray(domain.events.map(::eventJson)))
        internal fun exportJson(domain: ServerTraceDomain): JSONObject? {
            val projected = domainJson(domain)
            return projected.takeIf { parse(it.toString().toByteArray(Charsets.UTF_8), domain.captureId) != null }
        }
    }
}
