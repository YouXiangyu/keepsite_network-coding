package io.github.youxiangyu.keepsite.ui

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.*
import androidx.core.widget.doAfterTextChanged
import androidx.core.view.doOnNextLayout
import com.google.android.material.switchmaterial.SwitchMaterial
import io.github.youxiangyu.keepsite.BuildConfig
import io.github.youxiangyu.keepsite.R
import io.github.youxiangyu.keepsite.core.*
import io.github.youxiangyu.keepsite.ui.Design.Space
import io.github.youxiangyu.keepsite.ui.Design.Type
import java.io.Closeable

/** UI state contains aliases and control positions, never a business account or device object. */
class NetworkViewState {
    var tab = 0
    var filter = NetworkFilter()
    var expandedKey: String? = null
    var scope: String? = null
    val scroll = IntArray(3)
    var format = NetworkReportFormat.JSON
    var overviewDetails = false
    var traceTools = false
    fun reset() { filter = NetworkFilter(); expandedKey = null; scope = null; scroll.fill(0); overviewDetails = false; traceTools = false }
}

/** This page holds its original account capability until disposal. Ticks only read bounded memory. */
class NetworkMonitorScreen(private val context: Context, private val controller: AppController,
    private val state: NetworkViewState, private val back: () -> Unit,
    private val export: (NetworkMonitorScreen, NetworkReport, NetworkReportFormat, Boolean) -> Unit) : Closeable {
    val owner = controller.openNetworkPage()
    val view = ScrollView(context).apply { isFillViewport = true }
    private val body = BoundedColumn(context).apply {
        maxWidth = context.dp(1080); setPadding(context.dp(Space.PAGE), context.dp(Space.S), context.dp(Space.PAGE), context.dp(Space.XXL))
    }
    private val handler = Handler(Looper.getMainLooper())
    private var closed = false
    private var updating = false
    private val notice = context.label("", Type.Caption, R.color.mv_muted)
    private val content = context.column()
    private val summary = context.column()
    private val connections = context.column()
    private val traces = context.column()
    private val cards = mutableListOf<TextView>()
    private lateinit var detail: TextView
    private val overviewDetail = context.column()
    private val traceTools = context.column()
    private lateinit var detailToggle: Button
    private lateinit var toolsToggle: Button
    private val traceLive = context.label("", Type.Caption, R.color.mv_muted)
    private val activityRows = context.column()
    private val peerRows = context.column()
    private val events = context.column()
    private val expansion = context.column()
    private val traceCounts = context.label("", Type.Caption, R.color.mv_muted)
    private lateinit var detailed: SwitchMaterial
    private lateinit var pause: SwitchMaterial
    private lateinit var operation: EditText
    private lateinit var protocols: Spinner
    private lateinit var statuses: Spinner
    private lateinit var devices: Spinner
    private lateinit var times: Spinner
    private lateinit var readServer: View
    private lateinit var clearServer: View
    private lateinit var clearLocal: View
    private val protocolOptions = listOf("全部协议") + MonitorProtocol.entries.map { it.name.lowercase() }
    private val statusOptions = listOf("全部状态") + MonitorStatus.entries.map { it.name.lowercase() }
    private val timeOptions = listOf("各 clock 全部保留事件", "各 clock 最后 1 分钟", "各 clock 最后 5 分钟")
    private var deviceOptions = listOf("全部设备别名")
    private var allRows = emptyList<NetworkEventRow>()
    private var rowsSignature: Any? = null
    private var activitySignature: Any? = null
    private var peerSignature: Any? = null
    private val tick = object : Runnable {
        override fun run() { if (!closed) { refresh(); if (!closed) handler.postDelayed(this, 500) } }
    }

    init {
        view.addView(body, FrameLayout.LayoutParams(-1, -2, Gravity.CENTER_HORIZONTAL))
        body.add(context.action("返回同步与设备", ButtonKind.Text, back))
        body.add(context.label("网络监控", Type.Display, heading = true), Space.M)
        body.add(notice, Space.S)
        val tabs = context.row()
        listOf("总览", "连接与传输", "事件 Trace").forEachIndexed { index, title ->
            tabs.addView(context.action(title, ButtonKind.Text) { switchTab(index) }, LinearLayout.LayoutParams(0, -2, 1f))
        }
        body.add(tabs, Space.M); body.add(content, Space.M)
        buildOverview(); buildConnections(); buildTrace()
        switchTab(state.tab, false)
        handler.post(tick)
    }
    fun current() = !closed && owner?.current() == true
    fun message(text: String) { if (!closed) notice.text = text }
    private fun act(action: (NetworkPageOwner) -> Unit) {
        val page = owner ?: return
        if (!current()) { invalidate(); return }
        action(page); refresh()
    }
    private fun invalidate() {
        content.removeAllViews(); notice.text = "此账号会话已结束，请返回后重新打开网络监控。"
        state.reset(); handler.removeCallbacks(tick)
    }
    private fun switchTab(index: Int, remember: Boolean = true) {
        if (remember) state.scroll[state.tab] = view.scrollY
        state.tab = index.coerceIn(0, 2); content.removeAllViews()
        content.add(listOf(summary, connections, traces)[state.tab])
        rowsSignature = null; refresh()
        val selected = state.tab
        val restoreY = state.scroll[selected]
        // The first event render may enqueue its own restoration; the selected view's saved position wins last.
        view.doOnNextLayout { if (current() && state.tab == selected) view.scrollTo(0, restoreY) }
    }
    private fun buildOverview() {
        val grid = context.column()
        val columns = if (context.resources.configuration.fontScale > 1.3f) 1 else when {
            context.resources.configuration.screenWidthDp >= 1000 -> 3
            context.resources.configuration.screenWidthDp >= 700 -> 2
            else -> 1
        }
        repeat((5 + columns - 1) / columns) { line ->
            val row = context.row().apply { gravity = Gravity.TOP }
            repeat(columns) { column ->
                if (line * columns + column < 5) {
                    val label = context.label("暂无观测")
                    cards += label
                    row.addView(context.panel().apply { add(label, Space.S); setPadding(paddingLeft, paddingTop, paddingRight, context.dp(Space.M)) },
                        LinearLayout.LayoutParams(0, -2, 1f).apply { marginEnd = context.dp(Space.S) })
                } else row.addView(View(context), LinearLayout.LayoutParams(0, 0, 1f))
            }
            grid.add(row, Space.S)
        }
        summary.add(grid)
        detail = context.label("", Type.Caption, R.color.mv_muted)
        detailToggle = context.action("展开观测详情", ButtonKind.Text) {
            if (current()) { state.overviewDetails = !state.overviewDetails; updateDisclosure() }
        }
        summary.add(detailToggle, Space.M)
        overviewDetail.add(context.panel().apply { add(detail, Space.S) })
        summary.add(overviewDetail, Space.S)
        summary.add(context.label("连接角色示意", Type.Title, heading = true), Space.XL)
        listOf(
            "WSS 控制与通知" to "本机 ⇄ API 实时连接 ⇄ 同账号设备\n同步水位、发现与信令走控制通道。",
            "RTCDataChannel 文件" to "本机 ⇄ RTCDataChannel ⇄ 已授权设备\n候选对观测决定直连/中继/未知；控制消息不算文件载荷。",
            "服务器或 S3 文件读取" to "本机 → 服务器代理 → 存储\n或：本机 → 授权的 S3 直读\n以校验后的最终文件路径为准。"
        ).forEach { (title, text) -> summary.add(context.panel().apply {
            add(context.label(title, Type.Subtitle), Space.S); add(context.label(text, Type.Caption), Space.S)
        }, Space.M) }
        summary.add(context.label("静态示意说明角色；当前实际路线见上方观测。请求耗时不等于 RTT。", Type.Caption, R.color.mv_muted), Space.M)
    }
    private fun buildConnections() {
        connections.add(context.label("当前 HTTP 请求", Type.Title, heading = true))
        connections.add(activityRows, Space.S)
        connections.add(context.label("当前与近期设备传输", Type.Title, heading = true), Space.XL)
        connections.add(peerRows, Space.S)
    }
    private fun spinner(labels: List<String>, selection: Int = 0, changed: (Int) -> Unit): Spinner = Spinner(context).apply {
        adapter = ArrayAdapter(context, android.R.layout.simple_spinner_dropdown_item, labels)
        setSelection(selection.coerceIn(0, labels.lastIndex)); minimumHeight = context.dp(48)
        onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onNothingSelected(parent: AdapterView<*>?) { }
            override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) { if (!updating && current()) changed(position) }
        }
    }
    private fun buildTrace() {
        traces.add(traceLive)
        toolsToggle = context.action("展开采集与报告选项", ButtonKind.Text) {
            if (current()) { state.traceTools = !state.traceTools; updateDisclosure() }
        }
        traces.add(toolsToggle, Space.S); traces.add(traceTools, Space.S)
        detailed = SwitchMaterial(context).apply {
            text = "详细 Trace"; style(Type.Subtitle)
            setOnCheckedChangeListener { _, checked -> if (!updating) act { controller.setNetworkDetailed(it, checked) } }
        }
        pause = SwitchMaterial(context).apply {
            text = "暂停历史采集"; style(Type.Subtitle)
            setOnCheckedChangeListener { _, checked -> if (!updating) act { controller.pauseNetworkHistory(it, checked) } }
        }
        traceTools.add(detailed); traceTools.add(pause)
        traceTools.add(context.label("轻状态继续更新；清空仅清诊断记录。详细 Trace 默认关闭。", Type.Caption, R.color.mv_muted), Space.S)
        clearLocal = context.action("清空本机诊断记录", ButtonKind.Text) { serverIdle { controller.clearNetworkHistory(it) } }
        readServer = context.action("读取一次服务端 Trace", ButtonKind.Text) { serverIdle { controller.refreshServerTrace(it) } }
        clearServer = context.action("清空本次服务端捕获", ButtonKind.Text) { serverIdle { controller.refreshServerTrace(it, true) } }
        traceTools.add(clearLocal, Space.S); traceTools.add(readServer); traceTools.add(clearServer)
        traceTools.add(traceCounts, Space.M)
        protocols = spinner(protocolOptions, protocolOptions.indexOf(state.filter.protocol).coerceAtLeast(0)) { index ->
            state.filter = state.filter.copy(protocol = protocolOptions[index].takeIf { index > 0 }); updateEvents()
        }.apply { contentDescription = "协议筛选" }
        statuses = spinner(statusOptions, statusOptions.indexOf(state.filter.status).coerceAtLeast(0)) { index ->
            state.filter = state.filter.copy(status = statusOptions[index].takeIf { index > 0 }); updateEvents()
        }.apply { contentDescription = "状态筛选" }
        devices = spinner(deviceOptions) { index -> state.filter = state.filter.copy(device = deviceOptions[index].takeIf { index > 0 }); updateEvents() }
            .apply { contentDescription = "设备别名筛选" }
        times = spinner(timeOptions, when (state.filter.seconds) { 60L -> 1; 300L -> 2; else -> 0 }) { index ->
            state.filter = state.filter.copy(seconds = when (index) { 1 -> 60L; 2 -> 300L; else -> null }); updateEvents()
        }.apply { contentDescription = "每个 clock 的时间窗口筛选" }
        traces.add(protocols); traces.add(statuses); traces.add(devices); traces.add(times)
        operation = context.input("操作或 trace 别名前缀", 64).apply {
            setText(state.filter.operation)
            doAfterTextChanged { if (!updating) { state.filter = state.filter.copy(operation = it.toString()); updateEvents() } }
        }
        traces.add(operation, Space.S)
        traces.add(context.label("时间窗口相对各 clock 最后一次事件。展开显示完整当前快照中的关联成员。", Type.Caption, R.color.mv_muted), Space.S)
        traces.add(expansion, Space.M); traces.add(events, Space.M)
        traceTools.add(context.label("导出诊断报告", Type.Title, heading = true), Space.L)
        val formats = NetworkReportFormat.entries
        traceTools.add(spinner(listOf("JSON 完整快照", "JSONL 逐事件", "可读文本"), formats.indexOf(state.format)) { state.format = formats[it] })
        traceTools.add(context.action("保存文件") { requestExport(false) }, Space.S)
        traceTools.add(context.action("系统分享", ButtonKind.Text) { requestExport(true) }, Space.S)
        updateDisclosure()
    }
    private fun updateDisclosure() {
        overviewDetail.visibility = if (state.overviewDetails) View.VISIBLE else View.GONE
        traceTools.visibility = if (state.traceTools) View.VISIBLE else View.GONE
        detailToggle.text = if (state.overviewDetails) "收起观测详情" else "展开观测详情"
        toolsToggle.text = if (state.traceTools) "收起采集与报告选项" else "展开采集与报告选项"
    }
    private fun requestExport(share: Boolean) {
        if (!current()) { invalidate(); return }
        val report = owner?.let(controller::prepareNetworkReport)
        if (report == null) message("报告不可用，请重新打开页面。") else export(this, report, state.format, share)
    }
    private fun serverIdle(action: (NetworkPageOwner) -> Unit) = act { page ->
        if (controller.networkSnapshot(page)?.serverRead?.clearing != true) action(page)
    }
    private fun refresh() {
        if (closed) return
        val snapshot = owner?.let(controller::networkSnapshot)
        if (snapshot == null) { invalidate(); return }
        val observation = snapshot.observations
        listOf(clearLocal, readServer, clearServer).forEach { it.isEnabled = !snapshot.serverRead.clearing }
        val scope = "${observation.sessionId}:${observation.clearRevision}"
        if (state.scope != scope) {
            state.reset(); state.scope = scope; updating = true
            operation.setText(""); protocols.setSelection(0); statuses.setSelection(0); devices.setSelection(0); times.setSelection(0)
            updating = false; rowsSignature = null
            updateDisclosure()
        }
        updating = true; detailed.isChecked = observation.detailed; pause.isChecked = observation.historyPaused; updating = false
        if (state.tab == 0) updateOverview(snapshot)
        if (state.tab == 1) updateConnections(snapshot)
        if (state.tab == 2) {
            val server = snapshot.serverRead
            traceLive.text = "详细 Trace ${if (observation.detailed) "已开启" else "未开启"} · 历史${if (observation.historyPaused) "已暂停" else "未暂停"} · 保留 ${observation.events.size + observation.serverDomains.sumOf { it.events.size }} 个事件" +
                if (server.clearing) " · 服务端清理中" else ""
            traceCounts.text = "本次清空以来：本机记录 ${observation.recorded} · 丢弃 ${observation.dropped} · 拒绝操作 ${observation.droppedOperations}\n" +
                "保留 ${observation.events.size + observation.serverDomains.sumOf { it.events.size }}/200 · ${NetworkPresentation.bytes(observation.historyBytes.toLong())}/128 KiB\n" +
                "采集计量 ${NetworkPresentation.millis(observation.captureCostNs / 1_000_000.0)}（不含全部 UI/网络开销）\n" +
                "服务端${if (server.clearing) "清理中，请等待" else "读取：" + NetworkPresentation.status(server.status.name.lowercase())} · ${NetworkPresentation.error(server.error)}" +
                observation.serverDomains.joinToString("") { domain -> "\n服务端 ${domain.clockId.take(8)}：${if (domain.expiresAt == null) "不可用" else "已安装观测"} · 累计记录 ${domain.metrics.recorded} / 丢弃 ${domain.dropped} · ${NetworkPresentation.millis(domain.metrics.captureCostMs)}" }
            allRows = NetworkPresentation.rows(snapshot)
            val aliases = listOf("全部设备别名") + allRows.mapNotNull { it.device }.distinct().sorted()
            if (aliases != deviceOptions && !devices.hasFocus()) {
                updating = true; deviceOptions = aliases
                devices.adapter = ArrayAdapter(context, android.R.layout.simple_spinner_dropdown_item, aliases)
                devices.setSelection(aliases.indexOf(state.filter.device).coerceAtLeast(0)); updating = false
            }
            updateEvents()
        }
    }
    private fun updateOverview(s: NativeNetworkState) {
        NetworkPresentation.cards(s).forEachIndexed { index, card -> cards[index].text = "${card.title}\n${card.facts}" }
        val w = s.socket; val y = s.sync
        detail.text = "服务地址 ${BuildConfig.API_BASE_URL} · HTTPS\n" +
            "API 活动 ${s.api.active} · 上次成功 ${NetworkPresentation.instant(s.api.lastSuccessAt)} · 上次失败 ${NetworkPresentation.instant(s.api.lastFailureAt)}\n" +
            "WS 最后消息 ${NetworkPresentation.instant(w.lastMessageAt)} · ${w.lastMessage.name.lowercase()} · 实测间隔 ${NetworkPresentation.millis(w.intervalMs)}\n" +
            "WS 心跳配置 ${NetworkPresentation.millis(w.heartbeatMs)} · watchdog ${NetworkPresentation.millis(w.watchdogMs)} · 重试 ${NetworkPresentation.millis(w.retryDelayMs)}\n" +
            "WS 关闭码 ${w.closeCode ?: "未知"} · ${NetworkPresentation.error(w.error)} · 诊断能力 ${w.diagnosticsVersion ?: "未知"}\n" +
            "同步本地 cursor ${y.localCursor ?: "未知"} · 观测水位 ${y.observedCursor ?: "未知"} · 待应用 ${when (y.pending) { true -> "有"; false -> "无"; null -> "未知" }}\n" +
            "快照应用 ${NetworkPresentation.instant(y.lastSnapshotAt)}（${y.lastSnapshotItems ?: "未知"} 行） · 增量应用 ${NetworkPresentation.instant(y.lastDeltaAt)}（${y.lastDeltaItems ?: "未知"} 项）\n" +
            "最近非空应用 ${NetworkPresentation.instant(y.lastDataChangeAt)} · ${NetworkPresentation.error(y.error)}\n" +
            "Peer 支持 RTCDataChannel · ${if (s.peer.registered) "当前登记配置未设置 STUN/TURN" else "当前登记配置未知"}"
    }
    private fun updateConnections(s: NativeNetworkState) {
        val signature = s.httpActivities to (s.api.active to s.httpOmitted)
        if (activitySignature != signature) {
            activitySignature = signature; activityRows.removeAllViews()
            activityRows.add(context.label("实际活动 ${s.api.active} · 展示 ${s.httpActivities.size} · 超出展示上限 ${s.httpOmitted}", Type.Caption, R.color.mv_muted))
            if (s.httpActivities.isEmpty()) activityRows.add(context.label("暂无活动请求。", color = R.color.mv_muted), Space.S)
            s.httpActivities.forEach { item -> activityRows.add(context.panel().apply {
                add(context.label("${item.template.path} · ${NetworkPresentation.status(item.status.name.lowercase())}"), Space.S)
                add(context.label("${item.id.take(12)} · ${NetworkPresentation.millis(item.elapsedMs)} · HTTP ${item.httpStatus ?: "未知"}", Type.Caption), Space.S)
            }, Space.S) }
        }
        val names = owner?.let(controller::networkDeviceNames).orEmpty()
        val peerSignatureNow = s.peerTransfers to names
        if (peerSignature == peerSignatureNow) return
        peerSignature = peerSignatureNow; peerRows.removeAllViews()
        peerRows.add(context.label("授权在线设备：${names.joinToString("、").ifBlank { "暂无" }}\n设备名称仅在本页展示；报告只含别名。", Type.Caption, R.color.mv_muted))
        val terminal = setOf(MonitorStatus.COMPLETED, MonitorStatus.FAILED, MonitorStatus.CANCELLED, MonitorStatus.CLOSED)
        val active = s.peerTransfers.filter { it.status !in terminal }; val recent = s.peerTransfers.filter { it.status in terminal }
        listOf("当前 ${active.size} 条" to active, "近期最后观测 ${recent.size} 条" to recent).forEach { (title, rows) ->
            peerRows.add(context.label(title, Type.Subtitle), Space.M)
            if (rows.isEmpty()) peerRows.add(context.label("暂无观测。", Type.Caption, R.color.mv_muted), Space.S)
            rows.forEach { item -> val v = item.values; peerRows.add(context.panel().apply {
                add(context.label("${if (item.outgoing) "发送" else "接收"} · ${item.device} · ${NetworkPresentation.status(item.status.name.lowercase())}"), Space.S)
                add(context.label("${NetworkPresentation.bytes(v.bytes)} / ${NetworkPresentation.bytes(v.expectedBytes)} · ${v.bytesPerSecond?.let { NetworkPresentation.bytes(it.toLong()) + "/s" } ?: "速率未知"}\n" +
                    "${NetworkPresentation.route(v.route)} · 耗时 ${NetworkPresentation.millis(item.elapsedMs)}\n" +
                    "有效前缀 ${NetworkPresentation.bytes(v.resumedBytes)} · 发送缓冲 ${NetworkPresentation.bytes(v.bufferedBytes)}\n" +
                    "控制 ${NetworkPresentation.bytes(v.controlBytes)} · 丢弃 ${NetworkPresentation.bytes(v.discardedBytes)} · 校验 ${v.verified?.let { if (it) "通过" else "未通过" } ?: "未知"}\n" +
                    "PC ${v.pcState.name.lowercase()} · ICE ${v.iceState.name.lowercase()} · DTLS ${v.dtlsState.name.lowercase()} · DC ${v.channelOpen?.let { if (it) "打开" else "关闭" } ?: "未知"}\n" +
                    "候选 ${v.localCandidate.name.lowercase()} / ${v.remoteCandidate.name.lowercase()} · RTT ${NetworkPresentation.millis(v.rttMs)}\n" +
                    "${NetworkPresentation.error(v.error)} · 更新 ${NetworkPresentation.instant(item.updatedAt)} · stats ${NetworkPresentation.instant(item.statsAt)}", Type.Caption), Space.S)
            }, Space.S) }
        }
    }
    private fun updateEvents() {
        if (closed || state.tab != 2) return
        val selected = NetworkPresentation.filtered(allRows, state.filter)
        val signature = Triple(allRows, state.filter, state.expandedKey)
        if (rowsSignature == signature) return
        rowsSignature = signature
        val y = view.scrollY
        events.removeAllViews(); expansion.removeAllViews()
        val clicked = allRows.find { it.key == state.expandedKey }
        if (clicked != null) {
            expansion.add(context.label("操作详情 · 完整关联成员", Type.Title, heading = true))
            NetworkPresentation.lanes(NetworkPresentation.expanded(allRows, clicked)).forEach { (clock, rows) ->
                expansion.add(context.label("${rows.first().origin} clock ${clock.take(12)} · ${rows.size} 个事件", Type.Subtitle), Space.M)
                expansion.add(ClockWaterfall(context, rows), Space.S)
                rows.forEach { row -> expansion.add(context.label(eventText(row), Type.Caption), Space.S) }
            }
            expansion.add(context.action("收起操作", ButtonKind.Text) { state.expandedKey = null; updateEvents() }, Space.S)
        }
        events.add(context.label("保留事件 ${allRows.size} · 筛选结果 ${selected.size}", Type.Subtitle))
        if (selected.isEmpty()) events.add(context.label("暂无匹配事件。", color = R.color.mv_muted), Space.S)
        selected.forEach { row -> events.add(context.action(eventText(row), ButtonKind.Quiet) {
            if (current()) { state.expandedKey = row.key; updateEvents() }
        }, Space.S) }
        view.doOnNextLayout { if (current() && state.tab == 2) view.scrollTo(0, y) }
    }
    private fun eventText(row: NetworkEventRow): String =
        "${NetworkPresentation.protocol(row.protocol)} · ${row.event} · ${NetworkPresentation.status(row.status)}\n" +
            "${row.operation.take(12)} · ${row.device ?: "设备未知"} · ${NetworkPresentation.instant(row.timestamp)} · +${NetworkPresentation.millis(row.offset)}\n" +
            "${NetworkPresentation.bytes(row.bytes)} · 时长 ${NetworkPresentation.millis(row.duration)}" +
            (row.error?.takeUnless { it == "none" }?.let { " · $it" } ?: "") +
            (if (row.coalesced) " · 合并事件，无父关联" else if (!row.linked) " · 未关联本地索引" else "")
    override fun close() {
        if (closed) return
        state.scroll[state.tab] = view.scrollY; closed = true; handler.removeCallbacks(tick); owner?.close()
    }
}

/** Intervals are computed within one clock only. Point observations stay points. Text remains accessible outside the canvas. */
private class ClockWaterfall(context: Context, private val rows: List<NetworkEventRow>) : View(context) {
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = context.getColor(R.color.mv_ink); strokeWidth = context.dp(2).toFloat() }
    init { importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_NO }
    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) = setMeasuredDimension(MeasureSpec.getSize(widthMeasureSpec), context.dp(18 * rows.size + 16))
    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val low = rows.minOf { NetworkPresentation.interval(it)?.first ?: it.offset }
        val high = rows.maxOf { maxOf(NetworkPresentation.interval(it)?.second ?: it.offset, it.offset) }
        val span = (high - low).coerceAtLeast(1.0)
        fun x(value: Double) = context.dp(8) + ((width - context.dp(16)) * ((value - low) / span)).toFloat()
        rows.forEachIndexed { index, row ->
            val y = context.dp(16 + index * 18).toFloat(); val interval = NetworkPresentation.interval(row)
            if (interval != null) canvas.drawLine(x(interval.first), y, x(interval.second), y, paint)
            canvas.drawCircle(x(row.offset), y, context.dp(3).toFloat(), paint)
        }
    }
}
