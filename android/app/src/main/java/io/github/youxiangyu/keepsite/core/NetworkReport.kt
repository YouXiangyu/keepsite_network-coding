package io.github.youxiangyu.keepsite.core

import io.github.youxiangyu.keepsite.ui.NetworkPresentation
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import org.json.JSONArray
import org.json.JSONObject
import java.io.Closeable
import java.io.OutputStream
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.Executor

/** Owns only a newly created report destination; no arbitrary document is accepted elsewhere. */
internal class ReportFileJob(private val executor: Executor, private val cleanupExecutor: Executor,
    private val current: () -> Boolean, private val open: () -> OutputStream?, private val delete: () -> Unit,
    private val write: (OutputStream, () -> Boolean) -> Boolean, private val completed: (Boolean) -> Unit) : Closeable {
    val cancelled = AtomicBoolean(false)
    private var running = false
    private var ended = false
    private val cleanupOnce = AtomicBoolean(false)
    private fun valid() = diagnostic(false, current)
    fun start() {
        if (!valid()) { finish(false); return }
        try { executor.execute {
            synchronized(this) { if (ended || running || cancelled.get()) return@execute; running = true }
            var saved = false
            try {
                if (!cancelled.get() && valid()) open()?.use { output ->
                    if (!cancelled.get() && valid()) saved = write(output) { cancelled.get() || !valid() }
                }
            } catch (_: Exception) { saved = false }
            finish(saved)
        } } catch (_: java.util.concurrent.RejectedExecutionException) { finish(false) }
    }
    private fun finish(saved: Boolean) {
        val kept = synchronized(this) {
            if (ended) return
            ended = true; saved && !cancelled.get() && valid()
        }
        settle(kept)
    }
    private fun settle(saved: Boolean) {
        if (!saved && cleanupOnce.compareAndSet(false, true)) {
            try { cleanupExecutor.execute { diagnostic(Unit) { delete() } } } catch (_: java.util.concurrent.RejectedExecutionException) { }
        }
        completed(saved)
    }
    override fun close() {
        synchronized(this) {
            cancelled.set(true)
            if (running || ended) return
            ended = true // Claim an unstarted destination before a queued worker can open it.
        }
        settle(false)
    }
}

class NetworkPageOwner internal constructor(private val owner: NetworkDiagnostics, internal val account: NetworkMonitor.Account, private val seal: Any) : Closeable {
    private val closed = AtomicBoolean(false)
    internal fun matches(seal: Any) = !closed.get() && this.seal === seal
    fun current() = owner.pageCurrent(this)
    override fun close() { closed.set(true) } // Local capability only; never changes this or a replacement account's collector.
}
enum class NetworkReportFormat(val extension: String, val mime: String) { JSON("json", "application/json"), JSONL("jsonl", "application/x-ndjson"), TEXT("txt", "text/plain") }
data class NetworkReportEnvironment(val origin: String, val appVersion: String, val sdk: Int)
class NetworkReport internal constructor(private val owner: NetworkDiagnostics, internal val account: NetworkMonitor.Account, private val seal: Any,
    internal val history: NetworkMonitor.Export, private val state: NativeNetworkState, private val environment: NetworkReportEnvironment, private val capturedAt: Long) {
    internal fun contents(seal: Any) = if (this.seal === seal) Triple(state, environment, capturedAt) else null
    fun current() = owner.reportCurrent(this)
    internal fun render(format: NetworkReportFormat) = owner.renderReport(this, format)
    /** The caller checks before opening its owned destination; every actual chunk and completion rechecks. */
    internal fun write(format: NetworkReportFormat, stream: OutputStream, cancelled: () -> Boolean): Boolean {
        val text = render(format) ?: return false
        val bytes = text.toByteArray(Charsets.UTF_8)
        var offset = 0
        while (offset < bytes.size) {
            if (cancelled() || !current()) return false
            val count = minOf(4096, bytes.size - offset); stream.write(bytes, offset, count); offset += count
        }
        if (cancelled() || !current()) return false
        stream.flush(); return !cancelled() && current()
    }
}

internal object NetworkReports {
    private fun nullable(value: Any?) = value ?: JSONObject.NULL
    private fun values(value: MonitorValues): JSONObject {
        val v = value.safe()
        return JSONObject().put("bytes", nullable(v.bytes)).put("expectedBytes", nullable(v.expectedBytes)).put("controlBytes", nullable(v.controlBytes))
            .put("discardedBytes", nullable(v.discardedBytes)).put("resumedBytes", nullable(v.resumedBytes)).put("bufferedBytes", nullable(v.bufferedBytes))
            .put("bytesPerSecond", nullable(v.bytesPerSecond)).put("rttMs", nullable(v.rttMs)).put("verified", nullable(v.verified)).put("outgoing", nullable(v.outgoing))
            .put("route", v.route.name.lowercase()).put("localCandidate", v.localCandidate.name.lowercase()).put("remoteCandidate", v.remoteCandidate.name.lowercase())
            .put("dtlsState", v.dtlsState.name.lowercase()).put("pcState", v.pcState.name.lowercase()).put("iceState", v.iceState.name.lowercase())
            .put("channelOpen", nullable(v.channelOpen)).put("errorCode", v.error.name.lowercase())
    }
    fun build(historyJson: String, state: NativeNetworkState, environment: NetworkReportEnvironment, capturedAt: Long): JSONObject {
        val origin = environment.origin.toHttpUrlOrNull() ?: error("origin")
        require(origin.isHttps && origin.encodedPath == "/" && origin.query == null && origin.fragment == null && origin.username.isEmpty() && origin.password.isEmpty() && origin.host.any(Char::isLetter))
        require(environment.appVersion.matches(Regex("[0-9A-Za-z._-]{1,40}")) && environment.sdk in 1..10000)
        val a = state.api; val w = state.socket; val s = state.sync; val p = state.peer; val m = state.media
        val facts = JSONObject().put("api", JSONObject().put("active", a.active).put("paused", a.paused).put("lastStatus", nullable(a.lastStatus))
            .put("lastSuccessAt", nullable(a.lastSuccessAt)).put("lastFailureAt", nullable(a.lastFailureAt)).put("lastDurationMs", nullable(a.lastDurationMs)).put("errorCode", a.lastError.name.lowercase()))
            .put("ws", JSONObject().put("status", w.status.name.lowercase()).put("opened", w.opened).put("aligned", w.aligned)
                .put("connectedSince", nullable(w.connectedSince)).put("durationMs", nullable(w.durationMs)).put("lastMessageAt", nullable(w.lastMessageAt))
                .put("lastMessage", w.lastMessage.name.lowercase()).put("intervalMs", nullable(w.intervalMs)).put("heartbeatMs", nullable(w.heartbeatMs))
                .put("watchdogMs", nullable(w.watchdogMs)).put("retryDelayMs", nullable(w.retryDelayMs)).put("closeCode", nullable(w.closeCode))
                .put("diagnosticsVersion", nullable(w.diagnosticsVersion)).put("errorCode", w.error.name.lowercase()))
            .put("sync", JSONObject().put("running", s.running).put("paused", s.paused).put("localCursor", nullable(s.localCursor)).put("observedCursor", nullable(s.observedCursor))
                .put("pending", nullable(s.pending)).put("lastSnapshotAt", nullable(s.lastSnapshotAt)).put("lastDeltaAt", nullable(s.lastDeltaAt))
                .put("lastDataChangeAt", nullable(s.lastDataChangeAt)).put("lastSnapshotItems", nullable(s.lastSnapshotItems)).put("lastDeltaItems", nullable(s.lastDeltaItems))
                .put("durationMs", nullable(s.durationMs)).put("errorCode", s.error.name.lowercase()))
            .put("peer", JSONObject().put("enabled", nullable(p.enabled)).put("registered", p.registered).put("discovered", nullable(p.discovered)).put("active", p.active)
            .put("configuration", if (p.registered) "host_candidates_no_stun_turn" else "unknown"))
            .put("media", JSONObject().put("active", m.active).put("status", m.status.name.lowercase()).put("route", m.route.name.lowercase()).put("bytes", nullable(m.bytes))
                .put("expectedBytes", nullable(m.expectedBytes)).put("verified", nullable(m.verified)).put("lastSuccessAt", nullable(m.lastSuccessAt)).put("durationMs", nullable(m.durationMs)).put("errorCode", m.error.name.lowercase()))
            .put("serverRead", JSONObject().put("status", state.serverRead.status.name.lowercase()).put("errorCode", state.serverRead.error.name.lowercase()).put("httpStatus", nullable(state.serverRead.httpStatus)).put("clearing", state.serverRead.clearing))
            .put("httpActivities", JSONArray(state.httpActivities.take(16).map { JSONObject().put("id", it.id).put("routeTemplate", it.template.path)
                .put("status", it.status.name.lowercase()).put("startedAt", it.startedAt).put("elapsedMs", it.elapsedMs).put("httpStatus", nullable(it.httpStatus)) }))
            .put("httpOmitted", state.httpOmitted)
            .put("peerTransfers", JSONArray(state.peerTransfers.take(52).map { JSONObject().put("id", it.id).put("device", it.device).put("outgoing", it.outgoing)
                .put("status", it.status.name.lowercase()).put("elapsedMs", it.elapsedMs).put("updatedAt", it.updatedAt).put("statsAt", nullable(it.statsAt)).put("values", values(it.values)) }))
        val completed = state.observations.events.filter { it.event == MonitorEvent.MEDIA_READ && it.status == MonitorStatus.COMPLETED && it.values.verified == true }.distinctBy { it.operationId }
        val durations = completed.map { it.durationMs.toDouble() }.sorted()
        val median = if (durations.isEmpty()) null else if (durations.size % 2 == 1) durations[durations.size / 2] else (durations[durations.size / 2 - 1] + durations[durations.size / 2]) / 2
        val sum = completed.mapNotNull { it.values.bytes }.fold(0L) { total, n -> if (total > 9_007_199_254_740_991L - n) 9_007_199_254_740_992L else total + n }.takeIf { it <= 9_007_199_254_740_991L }
        return JSONObject().put("v", 1).put("kind", "keepsite_network_report").put("capturedAt", capturedAt)
            .put("environment", JSONObject().put("platform", "android").put("origin", origin.toString().removeSuffix("/")).put("appVersion", environment.appVersion).put("sdk", environment.sdk))
            .put("scope", JSONObject().put("localCounters", "since_last_clear").put("clearRevision", state.observations.clearRevision)
                .put("clocks", "separate_no_cross_clock_subtraction").put("bytes", "application_not_billed").put("captureCost", "observer_record_work_only")
                .put("aggregation", "verified_media_parent_terminal_only_other_layers_separate").put("installed", "sampling_points_not_health"))
            .put("state", facts).put("statistics", JSONObject().put("verifiedMediaSamples", completed.size).put("verifiedLogicalBytes", nullable(sum))
                .put("knownByteSamples", completed.count { it.values.bytes != null }).put("durationSamples", durations.size).put("p50MediaDurationMs", nullable(median))
                .put("firstObservedAt", nullable(state.observations.events.minOfOrNull { it.timestamp })).put("lastObservedAt", nullable(state.observations.events.maxOfOrNull { it.timestamp })))
            .put("history", JSONObject(historyJson))
    }
    fun format(root: JSONObject, state: NativeNetworkState, format: NetworkReportFormat): String = when (format) {
        NetworkReportFormat.JSON -> root.toString(2)
        NetworkReportFormat.JSONL -> buildString {
            val metadata = JSONObject(root.toString()); val history = metadata.getJSONObject("history")
            val local = history.getJSONArray("events"); val domains = history.getJSONArray("serverDomains")
            history.remove("events")
            for (i in 0 until domains.length()) domains.getJSONObject(i).remove("events")
            appendLine(JSONObject().put("kind", "metadata").put("report", metadata).toString())
            val source = root.getJSONObject("history")
            for (i in 0 until local.length()) appendLine(JSONObject().put("kind", "event").put("clockId", source.getString("clockId")).put("event", local.getJSONObject(i)).toString())
            val server = source.getJSONArray("serverDomains")
            for (i in 0 until server.length()) { val domain = server.getJSONObject(i); val events = domain.getJSONArray("events")
                for (j in 0 until events.length()) appendLine(JSONObject().put("kind", "event").put("clockId", domain.getString("clockId")).put("captureId", domain.getString("captureId")).put("event", events.getJSONObject(j)).toString()) }
        }
        NetworkReportFormat.TEXT -> buildString {
            appendLine("Keepsite 网络观测报告 · ${NetworkPresentation.instant(root.getLong("capturedAt"))}")
            NetworkPresentation.cards(state).forEach { appendLine("${it.title}：${it.facts}") }
            appendLine("本地计数：自本次清空以来；服务端各capture/clock另计。应用字节非计费流量，失败partial不算节流。")
            appendLine("时钟分泳道，不跨域相减。已安装采集点不表示健康；unknown表示未观测。采集成本仅observer记录工作。")
            appendLine("统计：${root.getJSONObject("statistics")}")
            appendLine("环境：${root.getJSONObject("environment")}")
            appendLine("当前事实：${root.getJSONObject("state")}")
            appendLine("容量/丢弃：${root.getJSONObject("history").getJSONObject("metrics")}")
            NetworkPresentation.lanes(NetworkPresentation.rows(state)).forEach { (clock, rows) ->
                appendLine("时钟 $clock")
                rows.forEach { appendLine("${it.origin} #${it.sequence} +${NetworkPresentation.millis(it.offset)} ${it.protocol}/${it.event} ${it.status} · ${NetworkPresentation.bytes(it.bytes)} · duration ${NetworkPresentation.millis(it.duration)} · operation ${it.operation} · trace ${it.trace ?: "unknown"}") }
            }
            appendLine("完整白名单明细（与JSON等价）："); append(root.toString(2))
        }
    }
}
