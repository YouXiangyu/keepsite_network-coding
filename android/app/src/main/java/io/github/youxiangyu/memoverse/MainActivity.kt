package io.github.youxiangyu.memoverse

import android.app.DatePickerDialog
import android.app.TimePickerDialog
import android.content.Intent
import android.net.Uri
import android.graphics.Rect
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.text.InputType
import android.text.TextUtils
import android.view.Gravity
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
import androidx.recyclerview.widget.RecyclerView
import androidx.recyclerview.widget.StaggeredGridLayoutManager
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
    private var libraryCount: TextView? = null
    private var libraryEmpty: TextView? = null
    private var libraryDraft: Button? = null
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
        libraryCount = null; libraryEmpty = null; libraryDraft = null
    }
    private fun navigate(route: String) { model.route = route; screenKey = null; render(controller.snapshot()) }
    private fun redraw() { screenKey = null; render(controller.snapshot()) }
    private fun render(state: AppState) {
        if (!active) return
        val nextAccount = state.session?.userId
        if (model.libraryAccount != nextAccount) {
            model.libraryAccount = nextAccount; model.libraryQuery = ""; model.libraryOldestFirst = false
        }
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
            listOf("票夹" to "library", "录入电影票" to "editor", "设备" to "devices").forEach { (label, route) ->
                binding.navigation.addView(navigationItem(label, route) {
                    if (route == "editor") {
                        if (controller.snapshot().draft != null) navigate("editor")
                        else { model.awaitingDraft = true; controller.newDraft() }
                    } else if (model.route == "editor") leaveDraft(route) else navigate(route)
                }, LinearLayout.LayoutParams(0, -2, 1f))
            }
        }
        for (index in 0 until binding.navigation.childCount) {
            val item = binding.navigation.getChildAt(index) as LinearLayout
            item.isSelected = item.tag == model.route || item.tag == "library" && model.route == "detail"
            val color = getColor(if (item.isSelected || item.tag == "editor") R.color.mv_ink else R.color.mv_muted)
            (item.getChildAt(0) as ImageView).setColorFilter(color)
            (item.getChildAt(1) as TextView).setTextColor(color)
        }
        updateChromeVisibility()
        val key: Any = when {
            state.session == null -> "boot"
            !authenticated -> listOf("auth", model.authMode, state.session.configured, state.session.emailEnabled, state.session.googleEnabled)
            model.route == "editor" -> listOf("editor", state.draft?.operationId)
            model.route == "detail" -> listOf("detail", state.tickets.find { it.id == model.detailId })
            model.route == "devices" -> listOf("devices", state.cache, state.peer, state.session.email)
            else -> "library"
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
                else -> libraryScreen()
            }
            if (preserveScroll) (binding.content.getChildAt(0) as? ScrollView)?.let { scroll -> scroll.post { scroll.scrollTo(0, oldScroll) } }
        }
        if (model.route == "editor" && authenticated) updateEditor(state)
        if (model.route == "library" && authenticated) updateLibrary(state)
    }
    private fun navigationItem(title: String, route: String, clicked: () -> Unit): LinearLayout = column().apply {
        tag = route; gravity = Gravity.CENTER; isClickable = true; isFocusable = true
        contentDescription = title; minimumHeight = dp(52); setPadding(dp(4), dp(2), dp(4), dp(2))
        setOnClickListener { clicked() }
        addView(ImageView(this@MainActivity).apply {
            setImageResource(when (route) { "editor" -> R.drawable.ic_add; "devices" -> R.drawable.ic_devices; else -> R.drawable.ic_ticket })
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
            if (route == "editor") {
                background = roundedBackground(R.color.mv_soft, 30)
                setPadding(dp(12), dp(12), dp(12), dp(12))
            } else setPadding(dp(8), dp(8), dp(8), dp(8))
        }, LinearLayout.LayoutParams(dp(if (route == "editor") 44 else 36), dp(if (route == "editor") 44 else 36)))
        addView(label(if (route == "editor") "录入" else title, 11f).apply {
            setPadding(0, dp(2), 0, 0); importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
            isVisible = route != "editor"
        })
    }
    private fun roundedBackground(color: Int, radius: Int): GradientDrawable = GradientDrawable().apply {
        setColor(getColor(color)); cornerRadius = dp(radius).toFloat()
    }
    private fun updateChromeVisibility() {
        binding.brand.isVisible = !keyboardVisible && (controller.snapshot().session?.authenticated != true || model.route != "library")
        binding.connection.isVisible = !keyboardVisible && !controller.snapshot().connected
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
    private fun libraryScreen() {
        val box = column().apply { setPadding(dp(18), 0, dp(18), 0) }
        val heading = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        heading.addView(label("电影", 30f, true).apply { setPadding(0, dp(6), 0, dp(4)) }, LinearLayout.LayoutParams(0, -2, 1f))
        libraryCount = label("", 14f).apply { setTextColor(getColor(R.color.mv_muted)); setPadding(0, dp(6), 0, 0) }
        heading.addView(libraryCount); box.add(heading)
        val tools = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        val sort = action(if (model.libraryOldestFirst) "最早添加⌄" else "最近添加⌄") {}.apply {
            textSize = 14f; background = null; backgroundTintList = null; setPadding(0, 0, dp(8), 0)
            minWidth = 0; minimumWidth = 0; gravity = Gravity.START or Gravity.CENTER_VERTICAL
            contentDescription = "电影票排序"
            setOnClickListener {
                AlertDialog.Builder(this@MainActivity).setTitle("排序方式")
                    .setSingleChoiceItems(arrayOf("最近添加", "最早添加"), if (model.libraryOldestFirst) 1 else 0) { dialog, selected ->
                        model.libraryOldestFirst = selected == 1
                        text = if (model.libraryOldestFirst) "最早添加⌄" else "最近添加⌄"
                        updateLibrary(controller.snapshot()); dialog.dismiss()
                    }.setNegativeButton("取消", null).show()
            }
        }
        tools.addView(sort, LinearLayout.LayoutParams(0, dp(48), 1f))
        tools.addView(action("↻") { controller.refresh(); adapter?.retryFailedImages() }.apply {
            contentDescription = "刷新票夹"; textSize = 24f; minWidth = 0; minimumWidth = 0
            background = null; backgroundTintList = null; setPadding(0, 0, 0, 0)
        }, LinearLayout.LayoutParams(dp(48), dp(48)))
        box.add(tools)
        box.add(input("搜索电影票", 200).apply {
            setText(model.libraryQuery); textSize = 15f; setTextColor(getColor(R.color.mv_ink)); setHintTextColor(getColor(R.color.mv_muted))
            background = roundedBackground(R.color.mv_soft, 28); backgroundTintList = null
            setPadding(dp(16), 0, dp(16), 0); compoundDrawablePadding = dp(10)
            setCompoundDrawablesRelativeWithIntrinsicBounds(R.drawable.ic_search, 0, 0, 0)
            inputType = InputType.TYPE_CLASS_TEXT; imeOptions = android.view.inputmethod.EditorInfo.IME_ACTION_SEARCH or android.view.inputmethod.EditorInfo.IME_FLAG_NO_EXTRACT_UI
            minimumHeight = dp(48)
            doAfterTextChanged { model.libraryQuery = it.toString(); updateLibrary(controller.snapshot()) }
        })
        libraryDraft = action("继续未完成的录入") { navigate("editor") }.also(box::add)
        libraryEmpty = label("", 14f).apply { setPadding(0, dp(20), 0, dp(16)); setTextColor(getColor(R.color.mv_muted)) }.also(box::add)
        val grid = RecyclerView(this).apply { setPadding(0, dp(14), 0, 0); clipToPadding = false; itemAnimator = null }
        val manager = StaggeredGridLayoutManager(2, StaggeredGridLayoutManager.VERTICAL); grid.layoutManager = manager
        grid.addItemDecoration(object : RecyclerView.ItemDecoration() {
            override fun getItemOffsets(outRect: Rect, view: View, parent: RecyclerView, state: RecyclerView.State) {
                val gap = dp(14); val columns = manager.spanCount
                val span = ((view.layoutParams as? StaggeredGridLayoutManager.LayoutParams)?.spanIndex ?: 0).coerceIn(0, columns - 1)
                outRect.set(gap * span / columns, 0, gap - gap * (span + 1) / columns, dp(12))
            }
        })
        grid.addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ ->
            val minimum = dp((156 * resources.configuration.fontScale.coerceAtLeast(1f)).toInt())
            val columns = (grid.width / minimum).coerceIn(1, 4)
            if (manager.spanCount != columns) { manager.spanCount = columns; grid.invalidateItemDecorations() }
        }
        adapter = TicketAdapter(); grid.adapter = adapter
        box.addView(grid, LinearLayout.LayoutParams(-1, 0, 1f)); binding.content.addView(box, ViewGroup.LayoutParams(-1, -1))
    }
    private fun updateLibrary(state: AppState) {
        val query = model.libraryQuery.trim()
        val filtered = state.tickets.filter { query.isBlank() || it.fields.title.contains(query, true) || it.fields.cinema.contains(query, true) }
        val ordered = if (model.libraryOldestFirst) filtered.sortedWith(compareBy<Ticket> { it.createdAt }.thenBy { it.id })
            else filtered.sortedWith(compareByDescending<Ticket> { it.createdAt }.thenBy { it.id })
        libraryCount?.text = "${state.tickets.size} 张"
        libraryDraft?.isVisible = state.draft != null
        libraryEmpty?.apply {
            isVisible = ordered.isEmpty()
            text = when {
                !state.ready -> "正在同步票夹…"
                state.tickets.isEmpty() -> "还没有电影票。选择一张票面，记录这次观影。"
                else -> "没有匹配的电影票"
            }
        }
        adapter?.submitTickets(ordered)
    }
    private inner class TicketAdapter : RecyclerView.Adapter<TicketAdapter.Holder>(), Closeable {
        private var tickets = emptyList<Ticket>()
        private val bound = mutableSetOf<Holder>()
        fun submitTickets(value: List<Ticket>) {
            if (tickets == value) return
            tickets = value; notifyDataSetChanged()
        }
        fun retryFailedImages() {
            bound.filter { it.box.isAttachedToWindow }.forEach { holder ->
                tickets.getOrNull(holder.bindingAdapterPosition)?.let { holder.presenter.retryFailed(it.cover) }
            }
        }
        inner class Holder(val box: LinearLayout, val image: ImageView, val title: TextView, val caption: TextView, val date: TextView, val meta: TextView) : RecyclerView.ViewHolder(box) {
            val presenter = MediaPresenter(controller, imageExecutor, image, caption, compact = true)
        }
        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
            val box = column().apply { isFocusable = true; isClickable = true }
            val image = TicketCoverView(this@MainActivity).apply { scaleType = ImageView.ScaleType.FIT_CENTER
                background = roundedBackground(R.color.mv_soft, 6); clipToOutline = true }
            box.addView(image, LinearLayout.LayoutParams(-1, -2))
            val title = label("", 15f).apply {
                typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL); setPadding(0, dp(4), 0, 0)
                maxLines = 2; ellipsize = TextUtils.TruncateAt.END
            }
            fun secondaryLabel() = label("", 12f).apply { setTextColor(getColor(R.color.mv_muted)); setPadding(0, dp(2), 0, 0) }
            val date = secondaryLabel(); val caption = secondaryLabel(); val meta = secondaryLabel()
            box.add(title); box.add(date); box.add(meta); box.add(caption)
            box.layoutParams = RecyclerView.LayoutParams(-1, -2)
            return Holder(box, image, title, caption, date, meta)
        }
        override fun onBindViewHolder(holder: Holder, position: Int) {
            val ticket = tickets[position]; bound.add(holder)
            holder.title.text = ticket.fields.title.ifBlank { "未命名电影票" }
            holder.image.contentDescription = "${holder.title.text}封面"
            (holder.image as TicketCoverView).setRatio(ticket.cover.width, ticket.cover.height)
            holder.date.text = ticket.fields.showtime.take(10).replace('-', '.').ifBlank { "日期待补充" }
            holder.meta.text = listOf(
                if (ticket.processingStatus in listOf("ready", "not_started")) "" else Presentation.processing(ticket.processingStatus),
                if (ticket.original.cloudStatus == "saved") "" else "原票尚未云存",
                if (ticket.poster != null && ticket.poster.cloudStatus != "saved") "海报尚未云存" else "",
            ).filter { it.isNotBlank() }.joinToString(" · ")
            holder.meta.isVisible = holder.meta.text.isNotBlank()
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
        body.add(action("退出") {
            AlertDialog.Builder(this).setTitle("退出登录？").setMessage("退出后停止同步与传输；云端记录和本机文件保留。").setNegativeButton("取消", null)
                .setPositiveButton("退出") { _, _ -> controller.logout() }.show()
        })
    }
    private fun openUrl(url: String) {
        if (!Presentation.safeExternalUrl(url)) return
        runCatching { startActivity(Intent(Intent.ACTION_VIEW, url.toUri())) }.onFailure {
            AlertDialog.Builder(this).setMessage("当前设备没有可打开此链接的浏览器。").setPositiveButton("知道了", null).show()
        }
    }
}
