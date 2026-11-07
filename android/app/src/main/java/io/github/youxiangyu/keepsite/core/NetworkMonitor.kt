package io.github.youxiangyu.keepsite.core

import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

enum class MonitorProtocol { HTTP, WS, SYNC, P2P, CACHE, S3, SQL, REDIS, WORKER }
enum class MonitorLane { HTTP, WS, SYNC, PEER, MEDIA }
enum class MonitorEvent { REQUEST, CONNECTION, MESSAGE, SNAPSHOT, DELTA, DISCOVERY, SIGNAL, ICE, CHANNEL, MEDIA_READ, MEDIA_ATTEMPT, VERIFICATION }
enum class MonitorStatus { UNKNOWN, STARTED, OPEN, TRANSFERRING, VERIFYING, COMPLETED, FAILED, CANCELLED, RETRYING, PAUSED, CLOSED }
enum class MonitorRoute { UNKNOWN, CACHE, P2P_DIRECT, P2P_RELAY, P2P_UNKNOWN, MIXED, SERVER_PROXY, DIRECT_S3 }
enum class MonitorError { NONE, UNKNOWN, NETWORK, AUTH_REQUIRED, AUTH_DENIED, TIMEOUT, PROTOCOL, INTEGRITY, CAPACITY, UNAVAILABLE, CANCELLED }
enum class MonitorDestination { AUTHENTICATED_API, ANONYMOUS_API, EXTERNAL, DIAGNOSTICS }
enum class MonitorTemplate(val path: String) {
    UNKNOWN("unknown"), SESSION("/api/auth/session"), AUTH("/api/auth/:action"),
    SNAPSHOT("/api/sync/snapshot"), DELTA("/api/sync/changes"), EVENTS("/api/sync/events"),
    MEDIA("/api/media/:asset"), DOWNLOAD("/api/media/:asset/download"), UPLOAD("/api/media"),
    TICKET("/api/tickets/:ticket"), DIAGNOSTICS("/api/diagnostics/events"), EXTERNAL("external")
}

data class MonitorValues(
    val bytes: Long? = null, val expectedBytes: Long? = null, val httpStatus: Int? = null, val closeCode: Int? = null,
    val localCursor: Long? = null, val observedCursor: Long? = null, val items: Long? = null,
    val heartbeatMs: Long? = null, val retryDelayMs: Long? = null, val resumedBytes: Long? = null,
    val bufferedBytes: Long? = null, val rttMs: Double? = null, val bytesPerSecond: Double? = null,
    val verified: Boolean? = null, val route: MonitorRoute = MonitorRoute.UNKNOWN, val error: MonitorError = MonitorError.NONE,
) {
    internal fun safe() = copy(bytes = number(bytes), expectedBytes = number(expectedBytes),
        httpStatus = httpStatus?.takeIf { it in 100..599 }, closeCode = closeCode?.takeIf { it in 1000..4999 },
        localCursor = number(localCursor), observedCursor = number(observedCursor), items = number(items),
        heartbeatMs = number(heartbeatMs), retryDelayMs = number(retryDelayMs), resumedBytes = number(resumedBytes),
        bufferedBytes = number(bufferedBytes), rttMs = finite(rttMs), bytesPerSecond = finite(bytesPerSecond))
    companion object {
        private fun number(value: Long?) = value?.takeIf { it in 0..9_007_199_254_740_991L }
        private fun finite(value: Double?) = value?.takeIf { it.isFinite() && it >= 0 && it <= 9_007_199_254_740_991.0 }
        fun selectedRoute(local: String?, remote: String?): MonitorRoute {
            val types = setOf("host", "srflx", "prflx", "relay")
            return if (local !in types || remote !in types) MonitorRoute.P2P_UNKNOWN
            else if (local == "relay" || remote == "relay") MonitorRoute.P2P_RELAY else MonitorRoute.P2P_DIRECT
        }
        fun mergeRoute(previous: MonitorRoute, sample: MonitorRoute): MonitorRoute = when {
            sample !in setOf(MonitorRoute.P2P_DIRECT, MonitorRoute.P2P_RELAY) -> previous
            previous in setOf(MonitorRoute.UNKNOWN, MonitorRoute.P2P_UNKNOWN, sample) -> sample
            else -> MonitorRoute.MIXED
        }
    }
}

data class MonitorRecord(
    val sequence: Long, val timestamp: Long, val offsetMs: Long, val durationMs: Long,
    val protocol: MonitorProtocol, val event: MonitorEvent, val status: MonitorStatus,
    val operationId: String, val traceId: String, val parentOperationId: String?, val captureId: String?,
    val device: String?, val template: MonitorTemplate, val values: MonitorValues,
)
data class MonitorSnapshot(
    val sessionId: String?, val clockId: String, val detailed: Boolean, val historyPaused: Boolean,
    val latest: Map<MonitorProtocol, MonitorRecord>, val events: List<MonitorRecord>, val active: Int,
    val historyBytes: Int, val recorded: Long, val dropped: Long, val droppedOperations: Long, val captureCostNs: Long,
    val aliases: Int, val completedTraces: Int,
    val serverDomains: List<ServerTraceDomain> = emptyList(), val importedServerEvents: Long = 0,
)
data class MonitorTraceContext(val captureId: String, val traceId: String)

internal interface MonitorClock {
    val id: String
    fun wallMillis(): Long
    fun nanos(): Long
}
private object ProcessMonitorClock : MonitorClock {
    override val id = UUID.randomUUID().toString().replace("-", "")
    private val origin = System.nanoTime()
    override fun wallMillis() = System.currentTimeMillis()
    override fun nanos() = (System.nanoTime() - origin).coerceAtLeast(0)
}

/** Bounded in-memory observations only. Callers retain business/account ownership. */
class NetworkMonitor internal constructor(private val clock: MonitorClock = ProcessMonitorClock,
                                         private val eventSize: (MonitorRecord) -> Int = { recordJson(it).toString().toByteArray(Charsets.UTF_8).size }) {
    class Account internal constructor(internal val sessionId: String)
    class Connection internal constructor(internal val account: Account, internal val lane: MonitorLane)
    class DeviceAlias internal constructor(internal val account: Account, internal val value: String)
    class Operation internal constructor(
        internal val connection: Connection, internal val id: String, internal val traceId: String,
        internal val parentId: String?, internal val history: Long, internal val capture: String?,
        internal val started: Long, internal val template: MonitorTemplate, internal val device: String?,
    )
    class Export internal constructor(private val seal: Any, internal val account: Account, internal val history: Long, private val snapshot: MonitorSnapshot) {
        internal fun contents(seal: Any) = snapshot.takeIf { this.seal === seal }
    }
    class ServerRead internal constructor(internal val connection: Connection, internal val history: Long, val captureId: String)
    private data class Stored(val record: MonitorRecord?, val bytes: Int, val server: ServerTraceEvent? = null, val domain: String? = null)
    private val serverDomains = linkedMapOf<String, ServerTraceDomain>()
    private var importedServerEvents = 0L
    private var account: Account? = null
    private val exportSeal = Any()
    private val connections = mutableMapOf<MonitorLane, Connection>()
    private val active = linkedMapOf<String, Operation>()
    private val completed = linkedMapOf<String, String>()
    private val aliases = linkedMapOf<String, DeviceAlias>()
    private val latest = mutableMapOf<MonitorProtocol, MonitorRecord>()
    private val events = ArrayDeque<Stored>()
    private var detailed = false
    private var paused = false
    private var capture: String? = null
    private var history = 0L
    private var sequence = 0L
    private var historyBytes = 0
    private var recorded = 0L
    private var dropped = 0L
    private var droppedOperations = 0L
    private var costNs = 0L

    @Synchronized fun openAccount(): Account {
        reset()
        return Account(id()).also { account = it }
    }
    @Synchronized fun closeAccount(owner: Account) { if (account === owner) reset() }
    private fun reset() {
        account = null; connections.clear(); active.clear(); completed.clear(); aliases.clear(); latest.clear(); events.clear(); serverDomains.clear()
        detailed = false; paused = false; capture = null; history++; sequence = 0; historyBytes = 0
        recorded = 0; dropped = 0; droppedOperations = 0; costNs = 0; importedServerEvents = 0
    }
    @Synchronized fun connect(owner: Account, lane: MonitorLane): Connection? {
        if (account !== owner) return null
        connections[lane]?.let { old -> active.entries.removeAll { it.value.connection === old } }
        return Connection(owner, lane).also { connections[lane] = it }
    }
    @Synchronized fun disconnect(connection: Connection) {
        if (!valid(connection)) return
        connections.remove(connection.lane)
        active.entries.removeAll { it.value.connection === connection }
    }
    private fun valid(connection: Connection) = account === connection.account && connections[connection.lane] === connection
    private fun valid(operation: Operation) = valid(operation.connection) && active[operation.id] === operation
    @Synchronized fun abandon(operation: Operation) { if (active[operation.id] === operation) active.remove(operation.id) }
    private inline fun <T> observing(fallback: T, rollback: () -> Unit, block: () -> T): T = try { block() }
        catch (_: Exception) { rollback(); dropped++; fallback }
    private fun rotate() { history++; capture = if (detailed && !paused) id() else null; completed.clear() }
    @Synchronized fun setDetailed(owner: Account, enabled: Boolean) {
        if (account !== owner || detailed == enabled) return
        detailed = enabled; rotate()
    }
    @Synchronized fun pauseHistory(owner: Account, pause: Boolean) {
        if (account !== owner || paused == pause) return
        paused = pause; rotate()
    }
    @Synchronized fun clearHistory(owner: Account) {
        if (account !== owner) return
        events.clear(); serverDomains.clear(); aliases.clear(); historyBytes = 0; recorded = 0; dropped = 0; importedServerEvents = 0; rotate()
    }
    @Synchronized fun alias(owner: Account, peerKey: String): DeviceAlias? {
        if (account !== owner || peerKey.isEmpty() || peerKey.length > 128) return null
        aliases.remove(peerKey)?.let { aliases[peerKey] = it; return it }
        if (aliases.size == MAX_SLOTS) aliases.remove(aliases.keys.first())
        return DeviceAlias(owner, id()).also { aliases[peerKey] = it }
    }
    @Synchronized fun begin(connection: Connection, protocol: MonitorProtocol, event: MonitorEvent,
                            template: MonitorTemplate = MonitorTemplate.UNKNOWN, device: DeviceAlias? = null,
                            parent: Operation? = null): Operation? {
        if (!valid(connection) || device != null && (device.account !== account || aliases.values.none { it === device })) return null
        if (parent != null && (!valid(parent) || parent.history != history)) return null
        if (active.size >= MAX_SLOTS) { droppedOperations++; return null }
        var created: Operation? = null
        return observing(null, { created?.let(::abandon) }) {
            val operationId = id()
            val operation = Operation(connection, operationId, parent?.traceId ?: id(), parent?.id, history, capture,
                clock.nanos(), template, device?.value)
            created = operation; active[operation.id] = operation
            if (observe(operation, protocol, event, MonitorStatus.STARTED)) operation else { abandon(operation); null }
        }
    }
    @Synchronized fun observe(operation: Operation, protocol: MonitorProtocol, event: MonitorEvent,
                              status: MonitorStatus, values: MonitorValues = MonitorValues()): Boolean {
        if (!valid(operation)) return false
        return observing(false, { abandon(operation) }) {
            val started = clock.nanos()
            val keep = detailed && !paused && operation.history == history && operation.capture == capture && capture != null
            val record = MonitorRecord(++sequence, clock.wallMillis().coerceAtLeast(0), millis(started),
                millis((started - operation.started).coerceAtLeast(0)), protocol, event, status, operation.id, operation.traceId,
                operation.parentId, if (keep) capture else null, operation.device, operation.template, values.safe())
            latest[protocol] = record
            if (keep) {
                val size = eventSize(record)
                if (size !in 1..MAX_EVENT_BYTES) dropped++ else {
                    while (events.size >= MAX_EVENTS || historyBytes + size > MAX_HISTORY_BYTES) {
                        historyBytes -= events.removeFirst().bytes; dropped++
                    }
                    events.addLast(Stored(record, size)); historyBytes += size; recorded++
                }
            }
            if (status in TERMINAL) {
                active.remove(operation.id)
                if (keep) {
                    completed.remove(operation.traceId)
                    if (completed.size == MAX_SLOTS) completed.remove(completed.keys.first())
                    completed[operation.traceId] = capture!!
                }
            }
            costNs = add(costNs, (clock.nanos() - started).coerceAtLeast(0))
            true
        }
    }
    @Synchronized fun traceContext(operation: Operation, destination: MonitorDestination): MonitorTraceContext? {
        if (destination != MonitorDestination.AUTHENTICATED_API || operation.template in setOf(MonitorTemplate.DIAGNOSTICS, MonitorTemplate.EXTERNAL, MonitorTemplate.UNKNOWN)
            || !valid(operation) || !detailed || paused || operation.history != history || operation.capture != capture) return null
        return capture?.let { MonitorTraceContext(it, operation.traceId) }
    }
    @Synchronized fun knowsTrace(owner: Account, captureId: String, traceId: String): Boolean {
        if (account !== owner || captureId != capture || !detailed || paused) return false
        return completed[traceId] == captureId || active.values.any { it.traceId == traceId && it.capture == captureId && it.history == history }
    }
    @Synchronized fun prepareServerRead(connection: Connection): ServerRead? {
        if (!valid(connection) || connection.lane != MonitorLane.HTTP || !detailed || paused) return null
        return capture?.let { ServerRead(connection, history, it) }
    }
    @Synchronized fun serverReadCurrent(read: ServerRead) = valid(read.connection) && read.history == history &&
        detailed && !paused && read.captureId == capture
    /** Replace a full manual after=0 read. A bounded high-water per domain prevents reimporting evicted rows. */
    @Synchronized fun installServer(read: ServerRead, batch: ServerTraceBatch): MonitorError {
        if (!serverReadCurrent(read)) return MonitorError.CANCELLED
        if (batch.domain.captureId != read.captureId) return MonitorError.PROTOCOL
        val domain = batch.domain
        val key = domain.captureId + domain.clockId
        val previous = serverDomains[key]
        if (domain.expiresAt != null && previous != null && domain.next < previous.next) return MonitorError.PROTOCOL
        if (previous == null && serverDomains.size == MAX_SERVER_DOMAINS) {
            return MonitorError.CAPACITY // Keep every admitted high-water; returning old clocks must never replay evicted sequences.
        }
        // A null expiry is only an unavailable capture, not a locally inferred TTL/capacity diagnosis.
        if (domain.expiresAt == null) removeServerDomain(key)
        else for (event in domain.events) {
            if (event.sequence <= (previous?.next ?: 0)) continue
            val size = ServerTraceBatch.eventJson(event).toString().toByteArray(Charsets.UTF_8).size
            while (events.size >= MAX_EVENTS || historyBytes + size > MAX_HISTORY_BYTES) {
                historyBytes -= events.removeFirst().bytes; dropped++
            }
            events.addLast(Stored(null, size, event, key)); historyBytes += size; importedServerEvents++
        }
        serverDomains[key] = domain.copy(events = emptyList(), instrumented = domain.instrumented.toSet())
        return MonitorError.NONE
    }
    private fun removeServerDomain(key: String) {
        val removed = events.filter { it.domain == key }
        historyBytes -= removed.sumOf { it.bytes }; dropped += removed.size
        events.removeAll { it.domain == key }
    }
    @Synchronized fun snapshot(): MonitorSnapshot = MonitorSnapshot(account?.sessionId, clock.id, detailed, paused,
        latest.toMap(), events.mapNotNull { it.record }, active.size, historyBytes, recorded, dropped, droppedOperations, costNs, aliases.size, completed.size,
        serverDomains.map { (key, domain) -> domain.copy(instrumented = domain.instrumented.toSet(), events = events.filter { it.domain == key }.mapNotNull { it.server }) }, importedServerEvents)
    @Synchronized fun prepareExport(owner: Account): Export? = if (account === owner) Export(exportSeal, owner, history, snapshot()) else null
    @Synchronized fun exportCurrent(export: Export) = account === export.account && history == export.history && export.contents(exportSeal) != null
    fun exportJson(export: Export): String? {
        if (!exportCurrent(export)) return null
        val state = export.contents(exportSeal) ?: return null
        val value = JSONObject().put("v", 1).put("clockId", state.clockId).put("sessionId", state.sessionId)
            .put("origin", "client").put("detailed", state.detailed).put("historyPaused", state.historyPaused)
            .put("events", JSONArray(state.events.map(::recordJson)))
            .put("serverDomains", JSONArray(state.serverDomains.map { ServerTraceBatch.exportJson(it) ?: return null }))
            .put("latest", JSONArray(state.latest.values.map(::recordJson)))
            .put("metrics", JSONObject().put("recorded", state.recorded).put("dropped", state.dropped)
                .put("droppedOperations", state.droppedOperations).put("captureCostNs", state.captureCostNs)
                .put("historyBytes", state.historyBytes).put("active", state.active).put("importedServerEvents", state.importedServerEvents))
            .put("capacity", MAX_EVENTS).put("maxBytes", MAX_HISTORY_BYTES).put("byteScope", "application_payload")
            .put("clockScope", "separate_domains_no_cross_clock_subtraction").put("countScope", "client_recorded_server_imported_separate_not_business_totals")
        val result = value.toString()
        return result.takeIf { exportCurrent(export) }
    }
    fun exportText(export: Export): String? {
        val safe = exportJson(export) ?: return null
        val state = export.contents(exportSeal) ?: return null
        val text = buildString {
            appendLine("Keepsite network observations")
            appendLine("Bounded session metadata; application payload bytes, not billed traffic.")
            appendLine("Client/server clocks are separate. Durations are observed operations, not pure RTT.")
            appendLine("Client recorded: ${state.recorded}; server imported: ${state.importedServerEvents}; history dropped: ${state.dropped}.")
            appendLine("Events retained: ${state.events.size + state.serverDomains.sumOf { it.events.size }}/$MAX_EVENTS; history bytes: ${state.historyBytes}/$MAX_HISTORY_BYTES.")
            appendLine("Installed instrumentation is not health. Unknown fields were not measured. Counts are not summed business totals.")
            for (domain in state.serverDomains) appendLine("Server clock ${domain.clockId}: ${domain.events.size} retained; capture ${if (domain.expiresAt == null) "unavailable" else "observed"}.")
            appendLine(); append(safe)
        }
        return text.takeIf { exportCurrent(export) }
    }
    companion object {
        const val MAX_EVENTS = 200
        const val MAX_HISTORY_BYTES = 128 * 1024
        const val MAX_EVENT_BYTES = 2 * 1024
        const val MAX_SLOTS = 32
        const val MAX_SERVER_DOMAINS = 4
        private val TERMINAL = setOf(MonitorStatus.COMPLETED, MonitorStatus.FAILED, MonitorStatus.CANCELLED, MonitorStatus.CLOSED)
        private fun id() = UUID.randomUUID().toString().replace("-", "")
        private fun millis(nanos: Long) = (nanos.coerceAtLeast(0) / 1_000_000).coerceAtMost(9_007_199_254_740_991L)
        private fun add(left: Long, right: Long) = if (Long.MAX_VALUE - left < right) Long.MAX_VALUE else left + right
        private fun json(value: Any?) = value ?: JSONObject.NULL
        private fun recordJson(record: MonitorRecord): JSONObject {
            val v = record.values.safe()
            return JSONObject().put("sequence", record.sequence).put("timestamp", record.timestamp).put("offsetMs", record.offsetMs)
                .put("durationMs", record.durationMs).put("origin", "client").put("protocol", record.protocol.name.lowercase())
                .put("event", record.event.name.lowercase()).put("status", record.status.name.lowercase())
                .put("operationId", record.operationId).put("traceId", record.traceId).put("parentOperationId", json(record.parentOperationId))
                .put("captureId", json(record.captureId)).put("device", json(record.device)).put("routeTemplate", record.template.path)
                .put("values", JSONObject().put("bytes", json(v.bytes)).put("expectedBytes", json(v.expectedBytes))
                    .put("httpStatus", json(v.httpStatus)).put("closeCode", json(v.closeCode)).put("localCursor", json(v.localCursor))
                    .put("observedCursor", json(v.observedCursor)).put("items", json(v.items)).put("heartbeatMs", json(v.heartbeatMs))
                    .put("retryDelayMs", json(v.retryDelayMs)).put("resumedBytes", json(v.resumedBytes)).put("bufferedBytes", json(v.bufferedBytes))
                    .put("rttMs", json(v.rttMs)).put("bytesPerSecond", json(v.bytesPerSecond)).put("verified", json(v.verified))
                    .put("route", v.route.name.lowercase()).put("errorCode", v.error.name.lowercase()))
        }
    }
}
