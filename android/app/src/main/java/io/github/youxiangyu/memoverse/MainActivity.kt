package io.github.youxiangyu.memoverse

import android.app.DatePickerDialog
import android.app.TimePickerDialog
import android.content.Intent
import android.net.Uri
import android.graphics.Rect
import android.os.Bundle
import android.text.InputType
import android.view.View
import android.view.ViewGroup
import android.widget.*
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.isEmpty
import androidx.core.view.isVisible
import androidx.core.view.doOnLayout
import androidx.core.net.toUri
import androidx.core.widget.doAfterTextChanged
import androidx.recyclerview.widget.GridLayoutManager
import androidx.recyclerview.widget.RecyclerView
import io.github.youxiangyu.memoverse.core.*
import io.github.youxiangyu.memoverse.databinding.ActivityMainBinding
import io.github.youxiangyu.memoverse.ui.*
import java.io.Closeable
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import java.util.concurrent.Executors

class MainActivity : AppCompatActivity() {
    private val model: UiViewModel by viewModels()
    private val controller get() = model.controller
    private lateinit var binding: ActivityMainBinding
    private val imageExecutor = Executors.newFixedThreadPool(2)
    private val media = mutableListOf<MediaPresenter>()
    private var subscription: Closeable? = null
    private var active = false
    private var account: String? = null
    private var screenKey: Any? = null
    private var adapter: TicketAdapter? = null
    private var editorFields = listOf<EditText>()
    private var editorDate: Button? = null
    private var editorPhoto: MediaPresenter? = null
    private var editorPhotoButton: Button? = null
    private var editorSave: Button? = null
    private var editorStatus: TextView? = null
    private var updatingFields = false
    private var keyboardVisible = false
    private var editorDateValue = ""
    private val picker = registerForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri ->
        if (uri != null && controller.snapshot().draft != null) controller.selectPhoto(uri)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        binding = ActivityMainBinding.inflate(layoutInflater)
        account = controller.snapshot().session?.userId
        setContentView(binding.root)
        WindowCompat.getInsetsController(window, binding.root).isAppearanceLightNavigationBars = true
        ViewCompat.setAccessibilityHeading(binding.brand, true)
        ViewCompat.setOnApplyWindowInsetsListener(binding.root) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            keyboardVisible = insets.isVisible(WindowInsetsCompat.Type.ime())
            updateChromeVisibility()
            if (keyboardVisible) view.doOnLayout {
                currentFocus?.takeIf { it is EditText }?.let { focused ->
                    focused.requestRectangleOnScreen(Rect(0, 0, focused.width, focused.height + dp(8)), true)
                }
            }
            insets
        }
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                when {
                    model.route == "editor" -> leaveDraft()
                    model.route != "library" -> navigate("library")
                    controller.snapshot().session?.authenticated != true && model.authMode != "login" -> { model.authMode = "login"; redraw() }
                    else -> { isEnabled = false; onBackPressedDispatcher.onBackPressed(); isEnabled = true }
                }
            }
        })
        callback(intent)
    }
    override fun onNewIntent(intent: Intent) { super.onNewIntent(intent); setIntent(intent); callback(intent) }
    private fun callback(intent: Intent?) {
        val uri = intent?.data ?: return
        if (Presentation.callback(uri.toString())) controller.handleGoogleCallback(uri)
        intent.data = null
    }
    override fun onStart() {
        super.onStart(); active = true; controller.setForeground(true)
        subscription = controller.subscribe(::render); render(controller.snapshot())
    }
    override fun onStop() {
        active = false; subscription?.close(); subscription = null
        releaseScreen(); screenKey = null; controller.setForeground(false); super.onStop()
    }
    override fun onDestroy() { imageExecutor.shutdownNow(); super.onDestroy() }
    private fun releaseScreen() {
        adapter?.close(); adapter = null; media.forEach { it.close() }; media.clear()
        editorPhoto = null; editorFields = emptyList(); editorDate = null
        editorSave = null; editorStatus = null; editorPhotoButton = null
    }
    private fun navigate(route: String) { model.route = route; screenKey = null; render(controller.snapshot()) }
    private fun redraw() { screenKey = null; render(controller.snapshot()) }
    private fun render(state: AppState) {
        if (!active) return
        val nextAccount = state.session?.userId
        if (nextAccount != account) {
            account = nextAccount; model.route = "library"; model.detailId = null
            model.awaitingDraft = false; model.editorWasOpen = false; screenKey = null; releaseScreen()
        }
        if (model.awaitingDraft && state.draft != null) { model.awaitingDraft = false; model.route = "editor"; screenKey = null }
        if (model.route == "editor" && state.draft != null) model.editorWasOpen = true
        if (model.route == "editor" && state.draft == null && model.editorWasOpen) {
            model.route = model.afterDraftRoute; model.afterDraftRoute = "library"; model.editorWasOpen = false; screenKey = null
        }
        binding.progress.visibility = if (state.busy.isNotBlank() || state.session == null && state.error.isBlank()) View.VISIBLE else View.GONE
        binding.connection.text = when {
            state.session == null -> "正在连接服务器"
            !state.session.authenticated -> "登录后查看你的电影票"
            state.connected -> "已连接 · 票夹同步中"
            else -> "连接暂不可用 · 已载入内容保留"
        }
        binding.notice.text = state.error.ifBlank { state.busy.ifBlank { state.message } }
        binding.notice.visibility = if (binding.notice.text.isBlank()) View.GONE else View.VISIBLE
        binding.notice.setTextColor(getColor(if (state.error.isBlank()) R.color.mv_muted else R.color.mv_error))
        val authenticated = state.session?.authenticated == true
        if (binding.navigation.isEmpty()) {
            listOf("票夹" to "library", "设备" to "devices").forEach { (label, route) ->
                binding.navigation.addView(action(label) {
                    if (model.route == "editor") leaveDraft(route) else navigate(route)
                }, LinearLayout.LayoutParams(0, -2, 1f))
            }
            binding.navigation.addView(action("退出") {
                AlertDialog.Builder(this).setTitle("退出登录？").setMessage("退出后停止同步与传输；云端记录和本机文件保留。").setNegativeButton("取消", null)
                    .setPositiveButton("退出") { _, _ -> controller.logout() }.show()
            }, LinearLayout.LayoutParams(0, -2, 1f))
        }
        updateChromeVisibility()
        val key: Any = when {
            state.session == null -> "boot"
            !authenticated -> listOf("auth", model.authMode, state.session.configured, state.session.emailEnabled, state.session.googleEnabled)
            model.route == "editor" -> listOf("editor", state.draft?.operationId)
            model.route == "detail" -> listOf("detail", state.tickets.find { it.id == model.detailId })
            model.route == "devices" -> listOf("devices", state.cache, state.peer, state.session.email)
            else -> listOf("library", state.tickets, state.ready, state.draft != null)
        }
        if (key != screenKey) {
            val oldScroll = (binding.content.getChildAt(0) as? ScrollView)?.scrollY ?: 0
            val preserveScroll = screenKey != null && model.route == "devices"
            releaseScreen(); binding.content.removeAllViews(); screenKey = key
            when {
                state.session == null -> scrollColumn().apply {
                    add(label("正在载入账号…")); add(action("重试连接", controller::refresh))
                }
                !authenticated -> authScreen(state.session!!)
                model.route == "editor" -> editorScreen(state)
                model.route == "detail" -> detailScreen(state)
                model.route == "devices" -> devicesScreen(state)
                else -> libraryScreen(state)
            }
            if (preserveScroll) (binding.content.getChildAt(0) as? ScrollView)?.let { scroll -> scroll.post { scroll.scrollTo(0, oldScroll) } }
        }
        if (model.route == "editor" && authenticated) updateEditor(state)
    }
    private fun updateChromeVisibility() {
        binding.brand.isVisible = !keyboardVisible
        binding.connection.isVisible = !keyboardVisible
        binding.navigation.isVisible = !keyboardVisible && controller.snapshot().session?.authenticated == true
    }
    private fun scrollColumn(): LinearLayout {
        val column = column().apply { setPadding(dp(20), dp(8), dp(20), dp(28)) }
        val scroll = ScrollView(this).apply { isFillViewport = true; addView(column) }
        binding.content.addView(scroll, ViewGroup.LayoutParams(-1, -1)); return column
    }
    private fun authScreen(session: Session) {
        val body = scrollColumn()
        val titles = mapOf("login" to "欢迎回来", "register" to "创建账号", "verify" to "验证邮箱", "forgot" to "找回密码", "reset" to "设置新密码")
        body.add(label(titles[model.authMode] ?: "登录", 24f, true))
        if (!session.configured) { body.add(label("服务器尚未配置身份验证，请稍后重试。")); body.add(action("重新连接", controller::refresh)); return }
        val email = input("邮箱", 254).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS
            setText(model.authEmail); doAfterTextChanged { model.authEmail = it.toString() }
        }
        val password = input(if (model.authMode == "reset") "新密码" else "密码", 256).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO
        }
        val code = input("邮箱验证码", 20)
        if (session.emailEnabled) {
            body.add(email)
            if (model.authMode in listOf("verify", "reset")) body.add(code)
            if (model.authMode in listOf("login", "register", "reset")) body.add(password)
            body.add(action(when (model.authMode) { "register" -> "注册"; "verify" -> "验证邮箱"; "forgot" -> "发送重置码"; "reset" -> "更新密码"; else -> "登录" }) {
                if (controller.snapshot().busy.isNotBlank()) return@action
                val address = email.text.toString().trim()
                if (address.isBlank()) { email.error = "请输入邮箱"; return@action }
                when (model.authMode) {
                    "register" -> controller.register(address, password.text.toString())
                    "verify" -> controller.confirmEmail(address, code.text.toString().trim())
                    "forgot" -> controller.resetPassword(address)
                    "reset" -> controller.confirmPassword(address, code.text.toString().trim(), password.text.toString())
                    else -> controller.login(address, password.text.toString())
                }
            })
            if (model.authMode == "verify") body.add(action("重新发送验证码") { controller.resendEmail(email.text.toString().trim()) })
            listOf("登录" to "login", "注册账号" to "register", "已有验证码，验证邮箱" to "verify", "忘记密码" to "forgot", "已有重置码" to "reset")
                .filter { it.second != model.authMode }.forEach { (text, mode) -> body.add(action(text) { model.authMode = mode; redraw() }) }
        }
        if (session.googleEnabled) body.add(action("使用 Google 登录") { controller.startGoogle(::openUrl) })
    }
    private fun libraryScreen(state: AppState) {
        val box = column().apply { setPadding(dp(16), 0, dp(16), 0) }
        box.add(label("我的票夹", 26f, true))
        box.add(action(if (state.draft == null) "录入电影票" else "继续未完成的录入") {
            if (controller.snapshot().draft != null) navigate("editor")
            else { model.awaitingDraft = true; controller.newDraft() }
        })
        box.add(action("刷新票夹") { controller.refresh(); redraw() })
        if (state.tickets.isEmpty()) box.add(label(if (state.ready) "还没有电影票。选择一张票面，记录这次观影。" else "正在同步票夹…"))
        val grid = RecyclerView(this)
        val manager = GridLayoutManager(this, 1); grid.layoutManager = manager
        grid.addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ ->
            val minimum = dp((260 * resources.configuration.fontScale.coerceAtLeast(1f)).toInt())
            val columns = (grid.width / minimum).coerceIn(1, 4)
            if (manager.spanCount != columns) manager.spanCount = columns
        }
        adapter = TicketAdapter(state.tickets); grid.adapter = adapter
        box.addView(grid, LinearLayout.LayoutParams(-1, 0, 1f)); binding.content.addView(box, ViewGroup.LayoutParams(-1, -1))
    }
    private inner class TicketAdapter(private val tickets: List<Ticket>) : RecyclerView.Adapter<TicketAdapter.Holder>(), Closeable {
        private val bound = mutableSetOf<Holder>()
        inner class Holder(val box: LinearLayout, val image: ImageView, val title: TextView, val caption: TextView, val meta: TextView) : RecyclerView.ViewHolder(box) {
            val presenter = MediaPresenter(controller, imageExecutor, image, caption)
        }
        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
            val box = column().apply { setPadding(dp(8), dp(8), dp(8), dp(20)); isFocusable = true; isClickable = true }
            val image = ImageView(this@MainActivity).apply { scaleType = ImageView.ScaleType.FIT_CENTER; setBackgroundColor(getColor(R.color.mv_soft)) }
            box.addView(image, LinearLayout.LayoutParams(-1, dp(220)))
            val title = label("", 20f, true); val caption = label("", 12f); val meta = label("", 14f)
            box.add(title); box.add(meta); box.add(caption)
            box.layoutParams = RecyclerView.LayoutParams(-1, -2)
            return Holder(box, image, title, caption, meta)
        }
        override fun onBindViewHolder(holder: Holder, position: Int) {
            val ticket = tickets[position]; bound.add(holder)
            holder.title.text = ticket.fields.title.ifBlank { "未命名电影票" }
            holder.image.contentDescription = "${holder.title.text}封面"
            holder.meta.text = listOf(ticket.fields.cinema, ticket.fields.showtime.replace('T', ' '), Presentation.processing(ticket.processingStatus)).filter { it.isNotBlank() }.joinToString("\n")
            holder.box.contentDescription = "打开${holder.title.text}详情"
            holder.box.setOnClickListener { model.detailId = ticket.id; navigate("detail") }
            holder.presenter.bind(ticket.cover)
        }
        override fun getItemCount() = tickets.size
        override fun onViewRecycled(holder: Holder) { holder.presenter.close(); bound.remove(holder) }
        override fun onViewDetachedFromWindow(holder: Holder) { holder.presenter.close() }
        override fun onViewAttachedToWindow(holder: Holder) {
            val position = holder.bindingAdapterPosition
            if (position != RecyclerView.NO_POSITION) holder.presenter.bind(tickets[position].cover)
        }
        override fun close() { bound.forEach { it.presenter.close() }; bound.clear() }
    }
    private fun picture(body: LinearLayout, description: String, ref: MediaRef? = null): MediaPresenter {
        val image = ImageView(this).apply { contentDescription = description; scaleType = ImageView.ScaleType.FIT_CENTER; setBackgroundColor(getColor(R.color.mv_soft)) }
        body.addView(image, LinearLayout.LayoutParams(-1, dp(300)))
        val caption = label("", 13f); body.add(caption)
        return MediaPresenter(controller, imageExecutor, image, caption).also { presenter ->
            media.add(presenter)
            if (ref != null) {
                presenter.bind(ref)
                body.add(action("重新加载图片") { presenter.close(); presenter.bind(ref) })
            }
        }
    }
    private fun detailScreen(state: AppState) {
        val body = scrollColumn(); body.add(action("返回票夹") { navigate("library") })
        val ticket = state.tickets.find { it.id == model.detailId }
        if (ticket == null) { body.add(label("这张电影票已不存在。")); return }
        body.add(label(ticket.fields.title.ifBlank { "未命名电影票" }, 26f, true)); picture(body, "电影封面", ticket.cover)
        listOf("电影", "影院", "影厅", "座位", "放映时间").zip(ticket.fields.values()).forEach { (name, value) -> body.add(label("$name：${value.ifBlank { "未填写" }.replace('T', ' ')}")) }
        body.add(label(Presentation.processing(ticket.processingStatus)))
        body.add(label("原票：${Presentation.cloud(ticket.original.cloudStatus)}", 13f))
        ticket.poster?.let { body.add(label("海报：${Presentation.cloud(it.cloudStatus)}", 13f)) }
        ticket.processingError?.takeIf { it.isNotBlank() }?.let { body.add(label(it)) }
        ticket.poster?.source?.let { source ->
            body.add(label("海报来源", 18f, true)); body.add(label("作者：${source.author.ifBlank { "未提供" }}")); body.add(label("许可：${source.licenseName}"))
            if (Presentation.safeExternalUrl(source.sourcePage)) body.add(action("查看来源页面") { openUrl(source.sourcePage) })
            if (Presentation.safeExternalUrl(source.licenseUrl)) body.add(action("查看许可条款") { openUrl(source.licenseUrl) })
        }
        if (ticket.poster != null) {
            val original = column(); original.visibility = View.GONE; var presenter: MediaPresenter? = null
            body.add(action("展开 / 收起原始票面") {
                if (original.isVisible) { original.visibility = View.GONE; presenter?.close() }
                else { original.visibility = View.VISIBLE; if (presenter == null) presenter = picture(original, "原始票面"); presenter?.bind(ticket.original) }
            }); body.add(original)
        }
        body.add(action("编辑电影票") { model.awaitingDraft = true; controller.editDraft(ticket.id) })
        if (ticket.processingStatus in listOf("failed", "no_match")) body.add(action("重试自动处理") { controller.retryProcessing(ticket.id) })
        body.add(action("删除电影票") {
            AlertDialog.Builder(this).setTitle("删除这张电影票？").setMessage("删除会同步到同账号的其他设备。").setNegativeButton("取消", null)
                .setPositiveButton("删除") { _, _ -> controller.deleteTicket(ticket.id) }.show()
        })
    }
    private fun editorScreen(state: AppState) {
        val body = scrollColumn(); val draft = state.draft ?: run { body.add(label("正在准备录入…")); return }
        body.add(label(if (draft.ticketId == null) "录入电影票" else "编辑电影票", 26f, true))
        body.add(label("票面为必需，其他信息可稍后填写；自动识别不会覆盖你的手动修改。"))
        editorPhoto = picture(body, "待保存的票面")
        editorPhotoButton = action("选择或替换票面") { picker.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)) }.also(body::add)
        editorFields = listOf("电影名称", "影院", "影厅", "座位").mapIndexed { index, name ->
            body.add(label(name)); input(name, if (index == 0) 120 else 200).also { field -> body.add(field); field.doAfterTextChanged { if (!updatingFields) saveFields() } }
        }
        body.add(label("放映时间"))
        editorDate = action("选择日期和时间", ::chooseDate).also(body::add)
        body.add(action("清除放映时间") { if (controller.snapshot().draft?.pendingConfirmation != true) { editorDateValue = ""; saveFields() } })
        editorStatus = label("", 14f).also(body::add)
        editorSave = action("保存电影票", controller::saveDraft).also(body::add)
        body.add(action("取消") { leaveDraft() })
    }
    private fun updateEditor(state: AppState) {
        val draft = state.draft ?: return
        updatingFields = true
        editorFields.zip(draft.fields.values()).forEach { (field, value) ->
            if (!field.hasFocus() && field.text.toString() != value) { field.setText(value); field.setSelection(field.text.length) }
            field.isEnabled = !draft.pendingConfirmation && state.busy.isBlank()
        }
        updatingFields = false; editorDateValue = draft.fields.showtime
        editorDate?.text = draft.fields.showtime.replace('T', ' ').ifBlank { "选择日期和时间" }
        editorDate?.isEnabled = !draft.pendingConfirmation && state.busy.isBlank()
        editorPhotoButton?.isEnabled = !draft.pendingConfirmation && state.busy.isBlank()
        editorSave?.isEnabled = state.busy.isBlank() && (draft.hasPhoto || draft.ticketId != null)
        editorSave?.text = if (draft.pendingConfirmation) "核对并重试保存" else "保存电影票"
        editorStatus?.text = when {
            draft.pendingConfirmation -> "保存结果尚未确认。请核对后重试，系统会先检查之前的提交，避免重复保存。"
            draft.hasPhoto -> "选中：${draft.photoName}"
            draft.ticketId != null -> "保留原始票面"
            else -> "请先选择一张票面"
        }
        if (draft.previewFile != null) editorPhoto?.local(draft.previewFile)
        else {
            val original = state.tickets.find { it.id == draft.ticketId }?.original
            if (original == null) editorPhoto?.local(null) else editorPhoto?.bind(original)
        }
    }
    private fun saveFields() {
        if (editorFields.size == 4) controller.setDraftFields(TicketFields(editorFields[0].text.toString(), editorFields[1].text.toString(), editorFields[2].text.toString(), editorFields[3].text.toString(), editorDateValue))
    }
    private fun chooseDate() {
        val date = runCatching { LocalDateTime.parse(editorDateValue) }.getOrElse { LocalDateTime.now() }
        DatePickerDialog(this, { _, year, month, day ->
            TimePickerDialog(this, { _, hour, minute ->
                editorDateValue = LocalDateTime.of(year, month + 1, day, hour, minute).format(DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm")); saveFields()
            }, date.hour, date.minute, true).show()
        }, date.year, date.monthValue - 1, date.dayOfMonth).show()
    }
    private fun leaveDraft(destination: String = "library") {
        if (controller.snapshot().draft?.pendingConfirmation == true) {
            AlertDialog.Builder(this).setTitle("请先核对保存结果").setMessage("上次提交可能已经保存。请使用“核对并重试保存”，确认结果后再离开。").setPositiveButton("继续核对", null).show()
            return
        }
        AlertDialog.Builder(this).setTitle("放弃未保存的修改？").setMessage("返回后，本次选择的票面和填写内容将被丢弃。").setNegativeButton("继续编辑", null)
            .setPositiveButton("放弃") { _, _ -> model.afterDraftRoute = destination; controller.discardDraft() }.show()
    }
    private fun devicesScreen(state: AppState) {
        val body = scrollColumn(); body.add(label("设备与缓存", 26f, true)); body.add(label(state.session?.email.orEmpty()))
        body.add(label("本机图片缓存", 20f, true))
        body.add(label(if (state.cache.readable) "${state.cache.files} 个文件 · ${Presentation.bytes(state.cache.bytes)} / ${Presentation.bytes(state.cache.limit)}" else "文件数量与容量未知 · 缓存目录暂不可读"))
        if (state.cache.error.isNotBlank()) body.add(label(state.cache.error))
        body.add(action("清空本机图片缓存") {
            AlertDialog.Builder(this).setTitle("清空本机图片缓存？").setMessage("电影票记录会保留。再次查看图片时需要从服务器或同账号设备获取。").setNegativeButton("取消", null)
                .setPositiveButton("清空") { _, _ -> controller.clearCache() }.show()
        })
        body.add(label("设备间传输", 20f, true))
        body.add(Switch(this).apply {
            text = "允许同账号设备传输图片"; minHeight = dp(48); isChecked = state.peer.enabled
            setOnCheckedChangeListener { _, enabled -> controller.setPeerEnabled(enabled) }
        })
        body.add(label(when { !state.peer.enabled -> "设备间传输已关闭"; state.peer.registered -> "本机已在线，可与同账号设备交换图片"; else -> "等待连接" }))
        body.add(label("发送中 ${state.peer.sending} · 接收中 ${state.peer.receiving}"))
        if (state.peer.error.isNotBlank()) body.add(label(state.peer.error))
        if (state.peer.devices.isEmpty()) body.add(label("暂无在线设备。"))
        state.peer.devices.forEach { device ->
            body.add(label(device.label.ifBlank { device.peerId } + if (device.peerId == state.peer.peerId) "（本机）" else "", 18f, true))
            body.add(label("${device.platform ?: "平台未提供"} · ${device.cacheCount?.let { "$it 个可共享文件" } ?: "共享文件数未知"}"))
        }
        if (state.peer.transfers.isNotEmpty()) body.add(label("最近传输", 20f, true))
        state.peer.transfers.take(50).forEach { transfer ->
            body.add(label("${if (transfer.direction == "send") "发送" else "接收"} · ${Presentation.transfer(transfer.status)} · ${Presentation.bytes(transfer.bytes)}\n${Presentation.route(transfer.route)}${if (transfer.error.isBlank()) "" else "\n${transfer.error}"}"))
        }
        body.add(action("刷新状态", controller::refresh))
    }
    private fun openUrl(url: String) {
        if (!Presentation.safeExternalUrl(url)) return
        runCatching { startActivity(Intent(Intent.ACTION_VIEW, url.toUri())) }.onFailure {
            AlertDialog.Builder(this).setMessage("当前设备没有可打开此链接的浏览器。").setPositiveButton("知道了", null).show()
        }
    }
}
