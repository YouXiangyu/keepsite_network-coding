package io.github.youxiangyu.keepsite.ui

import io.github.youxiangyu.keepsite.core.*
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

data class NetworkEventRow(val key: String, val clock: String, val origin: String, val capture: String?, val sequence: Long,
    val operation: String, val trace: String?, val parent: String?, val protocol: String, val event: String, val status: String,
    val device: String?, val timestamp: Long, val offset: Double, val start: Double?, val duration: Double?,
    val bytes: Long?, val route: String?, val linked: Boolean, val coalesced: Boolean = false, val error: String? = null) {
    val group: String get() = if (trace != null && !coalesced) "$capture:$trace" else key
}
data class NetworkFilter(val protocol: String? = null, val status: String? = null, val device: String? = null,
    val operation: String = "", val seconds: Long? = null)
data class NetworkCard(val title: String, val facts: String)

object NetworkPresentation {
    fun canOpen(authenticated: Boolean, @Suppress("UNUSED_PARAMETER") ready: Boolean) = authenticated
    fun bytes(value: Long?): String = when { value == null -> "未知"; value < 1024 -> "$value B"
        value < 1024 * 1024 -> String.format(Locale.ROOT, "%.1f KiB", value / 1024.0)
        else -> String.format(Locale.ROOT, "%.2f MiB", value / 1048576.0) }
    fun millis(value: Number?): String = value?.toDouble()?.takeIf { it.isFinite() && it >= 0 }?.let {
        if (it < 1000) String.format(Locale.ROOT, "%.1f ms", it) else String.format(Locale.ROOT, "%.1f s", it / 1000)
    } ?: "未知"
    fun instant(value: Long?): String = value?.let { runCatching {
        Instant.ofEpochMilli(it).atZone(ZoneId.systemDefault()).format(DateTimeFormatter.ofPattern("HH:mm:ss"))
    }.getOrNull() } ?: "暂无观测"
    fun status(value: String) = when (value) {
        "unknown" -> "未知"; "started" -> "进行中"; "open" -> "已连接"; "transferring" -> "传输中"; "verifying" -> "校验/确认中"
        "completed" -> "完成"; "failed" -> "失败"; "cancelled" -> "取消"; "retrying" -> "等待重连"; "paused" -> "已暂停"; "closed" -> "已关闭"; else -> "未知"
    }
    fun error(value: MonitorError) = when (value) {
        MonitorError.NONE -> "无"; MonitorError.AUTH_REQUIRED -> "登录已失效"; MonitorError.AUTH_DENIED -> "认证/权限拒绝"
        MonitorError.TIMEOUT -> "超时"; MonitorError.NETWORK -> "网络失败"; MonitorError.PROTOCOL -> "响应/协议异常"
        MonitorError.INTEGRITY -> "完整性校验失败"; MonitorError.CAPACITY -> "容量限制"; MonitorError.UNAVAILABLE -> "不可用"
        MonitorError.CANCELLED -> "已取消"; MonitorError.UNKNOWN -> "原因未知"
    }
    fun protocol(value: String) = when (value) { "media" -> "媒体操作"; "cache" -> "本机缓存"; "sync" -> "数据同步"; else -> value.uppercase(Locale.ROOT) }
    fun route(value: MonitorRoute) = when (value) {
        MonitorRoute.CACHE -> "本机缓存"; MonitorRoute.P2P_DIRECT -> "设备直连"; MonitorRoute.P2P_RELAY -> "设备中继"
        MonitorRoute.P2P_UNKNOWN -> "设备传输 · 路线未知"; MonitorRoute.MIXED -> "混合路线"; MonitorRoute.SERVER_PROXY -> "服务器代理"
        MonitorRoute.DIRECT_S3 -> "S3直读"; MonitorRoute.UNKNOWN -> "路线未知"
    }
    fun cards(state: NativeNetworkState): List<NetworkCard> = listOf(
        NetworkCard("API最近可达", when { state.api.lastStatus != null -> "HTTP ${state.api.lastStatus} · ${millis(state.api.lastDurationMs)}"
            state.api.lastFailureAt != null -> error(state.api.lastError); else -> "暂无观测" }),
        NetworkCard("实时连接", "${status(state.socket.status.name.lowercase())} · ${if (state.socket.opened) "持续" else "上次持续"} ${millis(state.socket.durationMs)}"),
        NetworkCard("数据同步", when { state.sync.running -> "正在拉取"; state.sync.error != MonitorError.NONE -> error(state.sync.error)
            state.sync.lastDeltaAt != null -> "应用成功 ${instant(state.sync.lastDeltaAt)}"; else -> "暂无成功应用" }),
        NetworkCard("设备传输", when { state.peer.enabled == false -> "用户已关闭"; state.peer.registered -> "已登记 · 发现 ${state.peer.discovered ?: "未知"} 台"
            else -> "等待登记 · ${state.peer.discovered?.let { "发现 $it 台" } ?: "发现未知"}" }),
        NetworkCard("最近文件路径", "${route(state.media.route)} · ${status(state.media.status.name.lowercase())}\n${bytes(state.media.bytes)} · 校验${when (state.media.verified) { true -> "通过"; false -> "未通过"; null -> "未知" }}")
    )
    fun rows(state: NativeNetworkState): List<NetworkEventRow> {
        val local = state.observations.events
        val known = local.mapNotNull { row -> row.captureId?.let { "$it:${row.traceId}" } }.toSet()
        return local.map { row ->
            val terminal = row.status in setOf(MonitorStatus.COMPLETED, MonitorStatus.FAILED, MonitorStatus.CANCELLED, MonitorStatus.CLOSED)
            val start = if (terminal && row.durationMs <= row.offsetMs) (row.offsetMs - row.durationMs).toDouble() else null
            NetworkEventRow("client:${row.captureId}:${state.observations.clockId}:${row.sequence}", state.observations.clockId, "client", row.captureId,
                row.sequence, row.operationId, row.traceId, row.parentOperationId, row.protocol.name.lowercase(), row.event.name.lowercase(), row.status.name.lowercase(),
                row.device, row.timestamp, row.offsetMs.toDouble(), start, if (terminal) row.durationMs.toDouble() else null,
                row.values.bytes, row.values.route.name.lowercase(), true, error = row.values.error.name.lowercase())
        } + state.observations.serverDomains.flatMap { domain -> domain.events.map { row ->
            NetworkEventRow("server:${domain.captureId}:${domain.clockId}:${row.sequence}", domain.clockId, "server", domain.captureId, row.sequence,
                row.spanId, row.traceId, row.parentSpanId, row.type.protocol.name.lowercase(), row.type.wire, row.status.name.lowercase(), null,
                row.timestamp, row.offsetMs, row.startOffsetMs, row.durationMs, row.bytes, null,
                row.traceId != null && "${domain.captureId}:${row.traceId}" in known, row.coalesced == true, row.error?.name?.lowercase())
        } }
    }
    fun filtered(all: List<NetworkEventRow>, filter: NetworkFilter): List<NetworkEventRow> {
        val last = all.groupBy { it.clock }.mapValues { (_, rows) -> rows.maxOf { it.offset } }
        return all.filter { row -> (filter.protocol == null || row.protocol == filter.protocol) && (filter.status == null || row.status == filter.status) &&
            (filter.device == null || row.device == filter.device) && (filter.operation.isBlank() || row.operation.startsWith(filter.operation) || row.trace?.startsWith(filter.operation) == true) &&
            (filter.seconds == null || row.offset >= (last[row.clock] ?: row.offset) - filter.seconds * 1000) }
    }
    /** Filters choose the clicked list row; expansion always comes from the full bounded snapshot. */
    fun expanded(all: List<NetworkEventRow>, clicked: NetworkEventRow): List<NetworkEventRow> =
        if (clicked.trace == null || clicked.coalesced) all.filter { it.key == clicked.key } else all.filter { it.group == clicked.group }
    fun lanes(rows: List<NetworkEventRow>) = rows.groupBy { it.clock }.mapValues { (_, members) -> members.sortedWith(compareBy<NetworkEventRow> { it.offset }.thenBy { it.sequence }) }
    fun interval(row: NetworkEventRow): Pair<Double, Double>? = row.start?.let { start -> row.duration?.let { duration ->
        (start to start + duration).takeIf { start.isFinite() && duration.isFinite() && start >= 0 && duration >= 0 && start + duration <= row.offset + 0.001 }
    } }
}
