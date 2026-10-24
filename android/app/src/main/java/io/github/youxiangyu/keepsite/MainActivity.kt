package io.github.youxiangyu.keepsite

import android.app.DatePickerDialog
import android.app.TimePickerDialog
import android.content.Intent
import android.content.res.ColorStateList
import android.graphics.Rect
import android.graphics.drawable.LayerDrawable
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
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.appcompat.content.res.AppCompatResources
import androidx.core.view.isEmpty
import androidx.core.view.isNotEmpty
import androidx.core.view.isVisible
import androidx.core.view.doOnLayout
import androidx.core.net.toUri
import androidx.core.widget.doAfterTextChanged
import androidx.recyclerview.widget.RecyclerView
import androidx.recyclerview.widget.StaggeredGridLayoutManager
import com.google.android.material.button.MaterialButton
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.progressindicator.LinearProgressIndicator
import com.google.android.material.switchmaterial.SwitchMaterial
import io.github.youxiangyu.keepsite.core.*
import io.github.youxiangyu.keepsite.databinding.ActivityMainBinding
import io.github.youxiangyu.keepsite.ui.*
import io.github.youxiangyu.keepsite.ui.Design.Radius
import io.github.youxiangyu.keepsite.ui.Design.Size
import io.github.youxiangyu.keepsite.ui.Design.Space
import io.github.youxiangyu.keepsite.ui.Design.Type
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
    private var accountDialog: androidx.appcompat.app.AlertDialog? = null
    private var editorDateValue = ""
    private val noticeSurface by lazy { rounded(R.color.mv_soft, Radius.CONTROL) }
    private val errorSurface by lazy { rounded(R.color.mv_error_soft, Radius.CONTROL) }
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
        styleChrome()
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
        accountDialog?.dismiss(); accountDialog = null
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
    private fun navigate(route: String) {
        model.route = route; screenKey = null; render(controller.snapshot())
        if (route == "devices") controller.refreshAccountMethods()
    }
    private fun redraw() { screenKey = null; render(controller.snapshot()) }
    private fun render(state: AppState) {
        if (!active) return
        val nextAccount = state.session?.userId
        if (model.libraryAccount != nextAccount) {
            model.libraryAccount = nextAccount; model.libraryQuery = ""; model.libraryOldestFirst = false
        }
        if (nextAccount != account) {
            accountDialog?.dismiss(); accountDialog = null
            account = nextAccount; model.route = "library"; model.detailId = null
            model.awaitingDraft = false; model.editorWasOpen = false; screenKey = null; releaseScreen()
        }
        if (model.awaitingDraft && state.draft != null) { model.awaitingDraft = false; model.route = "editor"; screenKey = null }
        if (model.route == "editor" && state.draft != null) model.editorWasOpen = true
        if (model.route == "editor" && state.draft == null && model.editorWasOpen) {
            model.route = model.afterDraftRoute; model.afterDraftRoute = "library"; model.editorWasOpen = false; screenKey = null
        }
        binding.progress.visibility = if (state.busy.isNotBlank() || state.session == null && state.error.isBlank()) View.VISIBLE else View.GONE
        binding.notice.text = state.error.ifBlank { state.busy.ifBlank { state.message } }
        binding.notice.visibility = if (binding.notice.text.isBlank()) View.GONE else View.VISIBLE
        binding.notice.setTextColor(getColor(if (state.error.isBlank()) R.color.mv_muted else R.color.mv_error))
        binding.notice.background = if (state.error.isBlank()) noticeSurface else errorSurface
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
            (item.getChildAt(1) as TextView).apply {
                setTextColor(color); typeface = (if (item.isSelected) Design.Weight.Bold else Design.Weight.Medium).typeface
            }
        }
        updateChromeVisibility()
        val key: Any = when {
            state.session == null -> "boot"
            !authenticated -> listOf("auth", model.authMode, state.session.configured, state.session.emailEnabled, state.session.googleEnabled)
            model.route == "editor" -> listOf("editor", state.draft?.operationId)
            model.route == "detail" -> listOf("detail", state.tickets.find { it.id == model.detailId })
            model.route == "devices" -> listOf("devices", state.cache, state.peer, state.session.email, state.accountMethods, state.accountPasswordEmail)
            else -> "library"
        }
        if (key != screenKey) {
            val oldScroll = (binding.content.getChildAt(0) as? ScrollView)?.scrollY ?: 0
            val preserveScroll = screenKey != null && model.route == "devices"
            releaseScreen(); binding.content.removeAllViews(); screenKey = key
            when {
                state.session == null -> scrollColumn().apply {
                    gravity = Gravity.CENTER
                    add(label("正在载入账号…", color = R.color.mv_muted).apply { gravity = Gravity.CENTER })
                    add(action("重试连接", clicked = controller::refresh), Space.L)
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
        contentDescription = title; minimumHeight = dp(Size.NAV_ITEM); background = ripple(borderless = true)
        setOnClickListener { clicked() }
        val add = route == "editor"
        addView(ImageView(this@MainActivity).apply {
            setImageResource(when (route) { "editor" -> R.drawable.ic_add; "devices" -> R.drawable.ic_devices; else -> R.drawable.ic_ticket })
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
            if (add) { background = pill(R.color.mv_soft); scaleType = ImageView.ScaleType.CENTER }
        }, LinearLayout.LayoutParams(dp(if (add) Size.NAV_ADD else Size.ICON), dp(if (add) Size.NAV_ADD else Size.ICON)))
        addView(label(if (add) "录入" else title, Type.Nav).apply {
            gravity = Gravity.CENTER; setPadding(0, dp(Space.XS), 0, 0)
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO; isVisible = !add
        })
    }
    private fun styleChrome() {
        binding.brand.apply {
            style(Type.Subtitle); setTextColor(getColor(R.color.mv_ink)); ViewCompat.setAccessibilityHeading(this, true)
            setCompoundDrawablesRelativeWithIntrinsicBounds(R.drawable.ic_ticket, 0, 0, 0); compoundDrawablePadding = dp(Space.S)
            setPadding(dp(Space.PAGE), dp(Space.L), dp(Space.PAGE), dp(Space.S))
        }
        listOf(binding.connection, binding.notice).forEach { banner ->
            banner.style(Type.Label); banner.setTextColor(getColor(R.color.mv_muted))
            banner.background = noticeSurface
            banner.setPadding(dp(Space.M), dp(Space.S), dp(Space.M), dp(Space.S))
            (banner.layoutParams as ViewGroup.MarginLayoutParams).setMargins(dp(Space.PAGE), dp(Space.S), dp(Space.PAGE), 0)
        }
        binding.connection.text = "连接暂不可用 · 已载入内容保留"
        binding.progress.indeterminateTintList = ColorStateList.valueOf(getColor(R.color.mv_ink))
    }
    private fun updateChromeVisibility() {
        val authenticated = controller.snapshot().session?.authenticated == true
        binding.brand.isVisible = !keyboardVisible && !authenticated
        binding.connection.isVisible = !keyboardVisible && authenticated && !controller.snapshot().connected
        binding.navigation.isVisible = !keyboardVisible && authenticated
    }
    private fun scrollColumn(): LinearLayout {
        val column = BoundedColumn(this).apply { maxWidth = dp(Size.CONTENT_MAX); setPadding(dp(Space.PAGE), dp(Space.S), dp(Space.PAGE), dp(Space.XXL)) }
        val scroll = ScrollView(this).apply { isFillViewport = true; addView(column, FrameLayout.LayoutParams(-1, -2, Gravity.CENTER_HORIZONTAL)) }
        binding.content.addView(scroll, ViewGroup.LayoutParams(-1, -1)); return column
    }
    private fun pageTitle(text: String): TextView = label(text, Type.Display, heading = true)
    private fun sectionTitle(text: String): TextView = label(text, Type.Title, heading = true)
    private fun LinearLayout.addWrapped(view: View, top: Int = 0, gravity: Int = Gravity.START) {
        addView(view, LinearLayout.LayoutParams(-2, -2).apply { topMargin = dp(top); this.gravity = gravity })
    }
    private fun authScreen(session: Session) {
        val body = scrollColumn()
        val titles = mapOf("login" to "欢迎回来", "register" to "创建账号", "verify" to "验证邮箱", "forgot" to "找回密码", "reset" to "设置新密码")
        val hints = mapOf("login" to "登录后查看你的电影票", "register" to "用邮箱创建账号，开始记录每一次观影", "verify" to "输入邮件中的验证码完成验证",
            "forgot" to "输入注册邮箱，我们会发送重置码", "reset" to "输入重置码并设置新密码")
        body.add(pageTitle(titles[model.authMode] ?: "登录"), Space.L)
        if (!session.configured) {
            body.add(label("服务器尚未配置身份验证，请稍后重试。", color = R.color.mv_muted), Space.S)
            body.add(action("重新连接", clicked = controller::refresh), Space.XL); return
        }
        body.add(label(hints[model.authMode].orEmpty(), color = R.color.mv_muted), Space.S)
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
            body.add(email, Space.XL)
            if (model.authMode in listOf("verify", "reset")) body.add(code, Space.M)
            if (model.authMode in listOf("login", "register", "reset")) body.add(password, Space.M)
            body.add(action(when (model.authMode) { "register" -> "注册"; "verify" -> "验证邮箱"; "forgot" -> "发送重置码"; "reset" -> "更新密码"; else -> "登录" }, ButtonKind.Primary) {
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
            }, Space.XL)
            if (model.authMode == "verify") body.add(action("重新发送验证码", ButtonKind.Text) { controller.resendEmail(email.text.toString().trim()) }, Space.S)
        }
        if (session.googleEnabled) {
            if (session.emailEnabled) body.add(row().apply {
                addView(divider(), LinearLayout.LayoutParams(0, 1, 1f))
                addView(label("或", Type.Caption, R.color.mv_muted).apply { setPadding(dp(Space.M), 0, dp(Space.M), 0) })
                addView(divider(), LinearLayout.LayoutParams(0, 1, 1f))
            }, Space.XL)
            body.add(action("使用 Google 登录") { controller.startGoogle(::openUrl) }, Space.XL)
        }
        if (session.emailEnabled) {
            val links = listOf("登录" to "login", "注册账号" to "register", "忘记密码" to "forgot", "已有验证码" to "verify", "已有重置码" to "reset")
                .filter { it.second != model.authMode }
            links.chunked(2).forEachIndexed { index, pair ->
                body.add(row().apply {
                    pair.forEach { (text, mode) ->
                        addView(action(text, ButtonKind.Quiet) { model.authMode = mode; redraw() },
                            LinearLayout.LayoutParams(0, -2, 1f))
                    }
                    if (pair.size == 1) addView(View(this@MainActivity), LinearLayout.LayoutParams(0, 0, 1f))
                }, if (index == 0) Space.L else 0)
            }
        }
    }
    private fun libraryScreen() {
        val box = column().apply { setPadding(dp(Space.PAGE), dp(Space.S), dp(Space.PAGE), 0) }
        val heading = row().apply { gravity = Gravity.BOTTOM }
        heading.addView(pageTitle("电影"), LinearLayout.LayoutParams(0, -2, 1f))
        libraryCount = label("", color = R.color.mv_muted).apply { setPadding(0, 0, 0, dp(Space.XS)) }
        heading.addView(libraryCount); box.add(heading)
        val tools = row()
        fun sortLabel() = if (model.libraryOldestFirst) "最早添加" else "最近添加"
        val sort = action(sortLabel(), ButtonKind.Text) {}.apply {
            setPadding(0, 0, dp(Space.S), 0); contentDescription = "电影票排序"
            setIconResource(R.drawable.ic_chevron_down); iconGravity = MaterialButton.ICON_GRAVITY_END; iconPadding = dp(Space.XS)
            setOnClickListener {
                MaterialAlertDialogBuilder(this@MainActivity).setTitle("排序方式")
                    .setSingleChoiceItems(arrayOf("最近添加", "最早添加"), if (model.libraryOldestFirst) 1 else 0) { dialog, selected ->
                        model.libraryOldestFirst = selected == 1
                        text = sortLabel()
                        updateLibrary(controller.snapshot()); dialog.dismiss()
                    }.setNegativeButton("取消", null).show()
            }
        }
        tools.addView(sort, LinearLayout.LayoutParams(-2, dp(Size.TOUCH)))
        tools.addView(View(this), LinearLayout.LayoutParams(0, 0, 1f))
        tools.addView(iconAction(R.drawable.ic_refresh, "刷新票夹") { controller.refresh(); adapter?.retryFailedImages() },
            LinearLayout.LayoutParams(dp(Size.TOUCH), dp(Size.TOUCH)))
        box.add(tools, Space.XS)
        box.add(input("搜索电影票", 200).apply {
            setText(model.libraryQuery)
            background = pill(R.color.mv_soft); compoundDrawablePadding = dp(Space.S)
            setCompoundDrawablesRelativeWithIntrinsicBounds(R.drawable.ic_search, 0, 0, 0)
            inputType = InputType.TYPE_CLASS_TEXT; imeOptions = android.view.inputmethod.EditorInfo.IME_ACTION_SEARCH or android.view.inputmethod.EditorInfo.IME_FLAG_NO_EXTRACT_UI
            minHeight = dp(Size.SEARCH); minimumHeight = minHeight
            doAfterTextChanged { model.libraryQuery = it.toString(); updateLibrary(controller.snapshot()) }
        }, Space.XS)
        libraryDraft = action("继续未完成的录入") { navigate("editor") }.also { box.add(it, Space.M) }
        libraryEmpty = label("", color = R.color.mv_muted).apply { gravity = Gravity.CENTER; setPadding(0, dp(Space.XXL), 0, dp(Space.L)) }.also(box::add)
        val grid = RecyclerView(this).apply { setPadding(0, dp(Space.L), 0, dp(Space.L)); clipToPadding = false; itemAnimator = null }
        val manager = StaggeredGridLayoutManager(2, StaggeredGridLayoutManager.VERTICAL); grid.layoutManager = manager
        grid.addItemDecoration(object : RecyclerView.ItemDecoration() {
            override fun getItemOffsets(outRect: Rect, view: View, parent: RecyclerView, state: RecyclerView.State) {
                val gap = dp(Space.M); val columns = manager.spanCount
                val span = ((view.layoutParams as? StaggeredGridLayoutManager.LayoutParams)?.spanIndex ?: 0).coerceIn(0, columns - 1)
                outRect.set(gap * span / columns, 0, gap - gap * (span + 1) / columns, dp(Space.L))
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
            val box = column().apply { isFocusable = true; isClickable = true; foreground = ripple() }
            val image = TicketCoverView(this@MainActivity).apply { scaleType = ImageView.ScaleType.FIT_CENTER
                background = rounded(R.color.mv_soft, Radius.IMAGE); clipToOutline = true }
            box.addView(image, LinearLayout.LayoutParams(-1, -2))
            val title = label("", Type.Subtitle).apply { maxLines = 2; ellipsize = TextUtils.TruncateAt.END }
            fun secondaryLabel() = label("", Type.Caption, R.color.mv_muted)
            val date = secondaryLabel(); val caption = secondaryLabel(); val meta = secondaryLabel()
            box.add(title, Space.S); box.add(date, Space.XS); box.add(meta, Space.XS); box.add(caption, Space.XS)
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
    private fun picture(body: LinearLayout, description: String, ref: MediaRef? = null, top: Int = Space.L, size: MediaRef? = ref): MediaPresenter {
        val surface = rounded(R.color.mv_soft, Radius.PANEL)
        val image = (if (size != null) TicketCoverView(this).apply { setRatio(size.width, size.height) } else ImageView(this)).apply {
            contentDescription = description; scaleType = ImageView.ScaleType.FIT_CENTER; clipToOutline = true
            background = if (size != null) surface else LayerDrawable(arrayOf(surface, AppCompatResources.getDrawable(this@MainActivity, R.drawable.ic_image)!!.mutate().apply {
                setTint(getColor(R.color.mv_muted))
            })).apply { setLayerGravity(1, Gravity.CENTER); setLayerSize(1, dp(Size.PLACEHOLDER_ICON), dp(Size.PLACEHOLDER_ICON)) }
        }
        body.addView(image, LinearLayout.LayoutParams(-1, if (size != null) -2 else dp(Size.PREVIEW)).apply { topMargin = dp(top) })
        val caption = label("", Type.Caption, R.color.mv_muted)
        val status = row().apply { addView(caption, LinearLayout.LayoutParams(0, -2, 1f)) }
        body.add(status, Space.XS)
        return MediaPresenter(controller, imageExecutor, image, caption).also { presenter ->
            media.add(presenter)
            if (ref != null) {
                presenter.bind(ref)
                status.addView(action("重新加载图片", ButtonKind.Text) { presenter.close(); presenter.bind(ref) }.apply {
                    style(Type.Label); setPadding(dp(Space.S), 0, 0, 0)
                })
            }
        }
    }
    private fun infoPanel(body: LinearLayout, title: String, rows: List<Pair<String, String>>, top: Int = Space.XL): LinearLayout {
        body.add(sectionTitle(title), top)
        return panel().apply {
            rows.forEachIndexed { index, (name, value) ->
                if (index > 0) addDivider()
                add(infoRow(name, value.ifBlank { "未填写" }, if (value.isBlank()) R.color.mv_muted else R.color.mv_ink))
            }
        }.also { body.add(it, Space.M) }
    }
    private fun detailScreen(state: AppState) {
        val body = scrollColumn()
        body.addWrapped(action("票夹", ButtonKind.Quiet) { navigate("library") }.apply {
            contentDescription = "返回票夹"; setIconResource(R.drawable.ic_chevron_left); iconPadding = dp(Space.XS)
            setPadding(0, 0, dp(Space.S), 0)
        })
        val ticket = state.tickets.find { it.id == model.detailId }
        if (ticket == null) { body.add(label("这张电影票已不存在。", color = R.color.mv_muted), Space.XL); return }
        body.add(pageTitle(ticket.fields.title.ifBlank { "未命名电影票" }), Space.XS)
        picture(body, "电影封面", ticket.cover)
        infoPanel(body, "票面信息", listOf("电影", "影院", "影厅", "座位", "放映时间").zip(ticket.fields.copy(showtime = ticket.fields.showtime.replace('T', ' ')).values()))
        val saved = infoPanel(body, "保存状态", listOfNotNull("自动处理" to Presentation.processing(ticket.processingStatus),
            "原票" to Presentation.cloud(ticket.original.cloudStatus), ticket.poster?.let { "海报" to Presentation.cloud(it.cloudStatus) }))
        ticket.processingError?.takeIf { it.isNotBlank() }?.let { saved.addDivider(); saved.add(label(it, color = R.color.mv_error).apply { setPadding(0, dp(Space.M), 0, dp(Space.M)) }) }
        ticket.poster?.source?.let { source ->
            val facts = when (source) {
                is CommonsPosterSource -> listOf("作者" to source.author.ifBlank { "未提供" }, "许可" to source.licenseName)
                is TmdbPosterSource -> listOf("来源" to "TMDB", "版权" to "海报版权归原权利人所有")
            }
            val terms = when (source) { is CommonsPosterSource -> source.licenseUrl; is TmdbPosterSource -> source.termsUrl }
            val panel = infoPanel(body, "海报来源", facts)
            val links = row()
            if (Presentation.safeExternalUrl(source.sourcePage)) links.addView(action("查看来源页面", ButtonKind.Text) { openUrl(source.sourcePage) }.apply { setPadding(0, 0, dp(Space.L), 0) })
            if (Presentation.safeExternalUrl(terms)) links.addView(action("查看使用条款", ButtonKind.Text) { openUrl(terms) }.apply { setPadding(0, 0, dp(Space.L), 0) })
            if (links.isNotEmpty()) { panel.addDivider(); panel.add(links) }
        }
        if (ticket.poster != null) {
            val original = column(); original.visibility = View.GONE; var presenter: MediaPresenter? = null
            body.add(action("展开 / 收起原始票面") {
                if (original.isVisible) { original.visibility = View.GONE; presenter?.close() }
                else { original.visibility = View.VISIBLE; if (presenter == null) presenter = picture(original, "原始票面", top = Space.M, size = ticket.original); presenter?.bind(ticket.original) }
            }, Space.XL); body.add(original)
        }
        body.add(action("编辑电影票", ButtonKind.Primary) { model.awaitingDraft = true; controller.editDraft(ticket.id) }, Space.XL)
        if (ticket.processingStatus in listOf("failed", "no_match")) body.add(action("重试自动处理") { controller.retryProcessing(ticket.id) }, Space.M)
        body.add(action("删除电影票", ButtonKind.Danger) {
            MaterialAlertDialogBuilder(this).setTitle("删除这张电影票？").setMessage("删除会同步到同账号的其他设备。").setNegativeButton("取消", null)
                .setPositiveButton("删除") { _, _ -> controller.deleteTicket(ticket.id) }.show()
        }, Space.M)
    }
    private fun editorScreen(state: AppState) {
        val body = scrollColumn(); val draft = state.draft ?: run { body.add(label("正在准备录入…", color = R.color.mv_muted), Space.XL); return }
        body.add(pageTitle(if (draft.ticketId == null) "录入电影票" else "编辑电影票"), Space.S)
        body.add(label("票面为必需，其他信息可稍后填写；自动识别不会覆盖你的手动修改。", color = R.color.mv_muted), Space.S)
        editorPhoto = picture(body, "待保存的票面", top = Space.XL)
        editorPhotoButton = action("选择或替换票面") { picker.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)) }
            .apply { setIconResource(R.drawable.ic_image); iconGravity = MaterialButton.ICON_GRAVITY_TEXT_START }.also { body.add(it, Space.M) }
        fun fieldName(name: String) = label(name, Type.Label, R.color.mv_muted)
        editorFields = listOf("电影名称", "影院", "影厅", "座位").mapIndexed { index, name ->
            body.add(fieldName(name), if (index == 0) Space.XL else Space.L)
            input(name, if (index == 0) 120 else 200).also { field -> body.add(field, Space.S); field.doAfterTextChanged { if (!updatingFields) saveFields() } }
        }
        val dateHeader = row().apply {
            addView(fieldName("放映时间"), LinearLayout.LayoutParams(0, -2, 1f))
            addView(action("清除放映时间", ButtonKind.Quiet) { if (controller.snapshot().draft?.pendingConfirmation != true) { editorDateValue = ""; saveFields() } }.apply {
                style(Type.Label); setPadding(dp(Space.S), 0, 0, 0)
            })
        }
        body.add(dateHeader, Space.S)
        editorDate = action("选择日期和时间", clicked = ::chooseDate).apply {
            style(Type.Body); gravity = Gravity.START or Gravity.CENTER_VERTICAL; textAlignment = View.TEXT_ALIGNMENT_VIEW_START
            setIconResource(R.drawable.ic_calendar); iconGravity = MaterialButton.ICON_GRAVITY_END
            iconTint = ColorStateList.valueOf(getColor(R.color.mv_muted)); minHeight = dp(Size.INPUT)
        }.also { body.add(it) }
        editorStatus = label("", Type.Caption, R.color.mv_muted).also { body.add(it, Space.XL) }
        editorSave = action("保存电影票", ButtonKind.Primary, controller::saveDraft).also { body.add(it, Space.M) }
        body.add(action("取消", ButtonKind.Text) { leaveDraft() }, Space.S)
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
            MaterialAlertDialogBuilder(this).setTitle("请先核对保存结果").setMessage("上次提交可能已经保存。请使用“核对并重试保存”，确认结果后再离开。").setPositiveButton("继续核对", null).show()
            return
        }
        MaterialAlertDialogBuilder(this).setTitle("放弃未保存的修改？").setMessage("返回后，本次选择的票面和填写内容将被丢弃。").setNegativeButton("继续编辑", null)
            .setPositiveButton("放弃") { _, _ -> model.afterDraftRoute = destination; controller.discardDraft() }.show()
    }
    private fun devicesScreen(state: AppState) {
        val body = scrollColumn(); body.add(pageTitle("设备与缓存"), Space.S)
        val email = state.session?.email.orEmpty()
        body.add(row().apply {
            addView(avatar(email), LinearLayout.LayoutParams(dp(Size.AVATAR), dp(Size.AVATAR)))
            addView(column().apply {
                setPadding(dp(Space.L), 0, 0, 0)
                add(label(email.ifBlank { "当前账号" }, Type.Subtitle).apply { singleLine() })
                add(label("同账号设备共享票夹与图片", Type.Caption, R.color.mv_muted), Space.XS)
            }, LinearLayout.LayoutParams(0, -2, 1f))
        }, Space.L)

        body.add(sectionTitle("登录方式"), Space.XL)
        body.add(panel().apply {
            add(label("绑定后可使用任一方式独立登录当前票夹。其他票夹的登录方式不能合并到此账号。"), Space.M)
            val methods = state.accountMethods
            if (methods == null) add(action("读取登录方式", clicked = controller::refreshAccountMethods), Space.M)
            else {
                add(label("Google：" + if (methods.google.linked) "已绑定 · ${methods.google.email}" else "未绑定"), Space.M)
                if (!methods.google.linked && state.session?.googleEnabled == true) {
                    if (methods.password.linked) add(label("请绑定使用另一个邮箱的 Google 账号。", Type.Caption), Space.S)
                    add(action("绑定 Google") { controller.linkGoogle(::openUrl) }, Space.S)
                }
                add(label("邮箱密码：" + if (methods.password.linked) "已添加 · ${methods.password.email}" else "未添加"), Space.M)
                if (!methods.password.linked && state.session?.emailEnabled == true) {
                    if (methods.google.linked) add(label("Google 先登录时，请使用另一个可收验证码的邮箱设置密码。", Type.Caption), Space.S)
                    if (state.accountPasswordEmail != null) add(action("输入验证码并完成添加") { accountPasswordDialog("confirm", state.accountPasswordEmail) }, Space.S)
                    add(action("添加新邮箱密码") { accountPasswordDialog("register") }, Space.S)
                    add(action("绑定已有邮箱密码", ButtonKind.Text) { accountPasswordDialog("link") }, Space.S)
                    if (state.accountPasswordEmail == null) add(action("已有邮件验证码", ButtonKind.Text) { accountPasswordDialog("confirm") }, Space.S)
                }
            }
            setPadding(paddingLeft, paddingTop, paddingRight, dp(Space.L))
        }, Space.M)

        body.add(sectionTitle("本机图片缓存"), Space.XL)
        body.add(panel().apply {
            setPadding(dp(Space.L), dp(Space.L), dp(Space.L), dp(Space.XS))
            if (state.cache.readable) {
                add(label("${state.cache.files} 个文件", Type.Subtitle))
                add(label("${Presentation.bytes(state.cache.bytes)} / ${Presentation.bytes(state.cache.limit)}", Type.Caption, R.color.mv_muted), Space.XS)
                add(LinearProgressIndicator(this@MainActivity).apply {
                    max = 1000; progress = if (state.cache.limit > 0) (state.cache.bytes * 1000 / state.cache.limit).toInt().coerceIn(0, 1000) else 0
                    trackThickness = dp(Size.PROGRESS); trackCornerRadius = dp(Size.PROGRESS) / 2
                    setIndicatorColor(getColor(R.color.mv_ink)); trackColor = getColor(R.color.mv_soft)
                    importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
                }, Space.M)
            } else add(label("文件数量与容量未知 · 缓存目录暂不可读", color = R.color.mv_muted))
            if (state.cache.error.isNotBlank()) add(label(state.cache.error, color = R.color.mv_error), Space.S)
            addDivider(Space.M)
            add(listRow(R.drawable.ic_trash, "清空本机图片缓存") {
                MaterialAlertDialogBuilder(this@MainActivity).setTitle("清空本机图片缓存？").setMessage("电影票记录会保留。再次查看图片时需要从服务器或同账号设备获取。").setNegativeButton("取消", null)
                    .setPositiveButton("清空") { _, _ -> controller.clearCache() }.show()
            })
        }, Space.M)

        body.add(sectionTitle("设备间传输"), Space.XL)
        body.add(panel().apply {
            add(SwitchMaterial(this@MainActivity).apply {
                text = "允许同账号设备传输图片"; style(Type.Subtitle); setTextColor(getColor(R.color.mv_ink))
                minHeight = dp(Size.BUTTON); isChecked = state.peer.enabled
                setOnCheckedChangeListener { _, enabled -> controller.setPeerEnabled(enabled) }
            })
            addDivider()
            add(label(when { !state.peer.enabled -> "设备间传输已关闭"; state.peer.registered -> "本机已在线，可与同账号设备交换图片"; else -> "等待连接" }), Space.M)
            add(label("发送中 ${state.peer.sending} · 接收中 ${state.peer.receiving}", Type.Caption, R.color.mv_muted), Space.XS)
            if (state.peer.error.isNotBlank()) add(label(state.peer.error, color = R.color.mv_error), Space.S)
            if (state.peer.devices.isEmpty()) add(label("暂无在线设备。", Type.Caption, R.color.mv_muted), Space.S)
            state.peer.devices.forEach { device ->
                addDivider(Space.M)
                add(label(device.label.ifBlank { device.peerId } + if (device.peerId == state.peer.peerId) "（本机）" else "", Type.Subtitle), Space.M)
                add(label("${device.platform ?: "平台未提供"} · ${device.cacheCount?.let { "$it 个可共享文件" } ?: "共享文件数未知"}", Type.Caption, R.color.mv_muted), Space.XS)
            }
            setPadding(paddingLeft, paddingTop, paddingRight, dp(Space.L))
        }, Space.M)

        if (state.peer.transfers.isNotEmpty()) {
            body.add(sectionTitle("最近传输"), Space.XL)
            body.add(panel().apply {
                state.peer.transfers.take(50).forEachIndexed { index, transfer ->
                    if (index > 0) addDivider()
                    add(column().apply {
                        setPadding(0, dp(Space.M), 0, dp(Space.M))
                        add(label("${if (transfer.direction == "send") "发送" else "接收"} · ${Presentation.transfer(transfer.status)} · ${Presentation.bytes(transfer.bytes)}"))
                        add(label(Presentation.route(transfer.route), Type.Caption, R.color.mv_muted), Space.XS)
                        if (transfer.error.isNotBlank()) add(label(transfer.error, Type.Caption, R.color.mv_error), Space.XS)
                    })
                }
            }, Space.M)
        }

        body.add(sectionTitle("关于海报数据"), Space.XL)
        body.add(panel().apply {
            addView(ImageView(this@MainActivity).apply {
                setImageResource(R.drawable.tmdb_logo); contentDescription = "TMDB"
                scaleType = ImageView.ScaleType.FIT_START; adjustViewBounds = true
            }, LinearLayout.LayoutParams(dp(150), dp(65)).apply { topMargin = dp(Space.L) })
            add(label("This product uses the TMDB API but is not endorsed or certified by TMDB.", Type.Caption), Space.M)
            add(action("TMDB 数据与使用条款", ButtonKind.Text) { openUrl("https://www.themoviedb.org/api-terms-of-use") }, Space.S)
        }, Space.M)

        body.add(panel().apply {
            setPadding(dp(Space.L), dp(Space.XS), dp(Space.L), dp(Space.XS))
            add(listRow(R.drawable.ic_refresh, "刷新状态", clicked = controller::refresh))
            addDivider()
            add(listRow(R.drawable.ic_logout, "退出", danger = true) {
                MaterialAlertDialogBuilder(this@MainActivity).setTitle("退出登录？").setMessage("退出后停止同步与传输；云端记录和本机文件保留。").setNegativeButton("取消", null)
                    .setPositiveButton("退出") { _, _ -> controller.logout() }.show()
            })
        }, Space.XL)
    }
    private fun accountPasswordDialog(mode: String, fixedEmail: String? = null) {
        val fields = column().apply { setPadding(dp(Space.L), dp(Space.S), dp(Space.L), 0) }
        val email = input("邮箱", 254).apply { inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS; setText(fixedEmail.orEmpty()); isEnabled = fixedEmail == null }
        val code = input("邮件验证码", 100)
        val password = input(if (mode == "link") "已有邮箱密码" else if (mode == "confirm") "刚设置的密码" else "设置密码", 128).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            setAutofillHints(if (mode == "link") View.AUTOFILL_HINT_PASSWORD else "newPassword")
        }
        fields.add(label("邮箱", Type.Label)); fields.add(email, Space.S)
        if (mode == "confirm") { fields.add(label("邮件验证码", Type.Label), Space.M); fields.add(code, Space.S) }
        fields.add(label(password.hint.toString(), Type.Label), Space.M); fields.add(password, Space.S)
        fields.add(label("密码需为 12–128 个字符，包含大小写字母、数字和符号。", Type.Caption), Space.S)
        accountDialog?.dismiss()
        val builder = MaterialAlertDialogBuilder(this).setTitle(if (mode == "confirm") "验证并添加邮箱密码" else if (mode == "link") "绑定已有邮箱密码" else "添加新邮箱密码")
            .setView(fields).setNegativeButton("取消", null).setPositiveButton(if (mode == "register") "发送验证邮件" else "验证并绑定", null)
        if (mode == "confirm") builder.setNeutralButton("重发验证码", null)
        val dialog = builder.create()
        accountDialog = dialog
        dialog.setOnShowListener {
            if (mode == "confirm") dialog.getButton(androidx.appcompat.app.AlertDialog.BUTTON_NEUTRAL).setOnClickListener {
                if (email.text.isNullOrBlank()) email.error = "请填写邮箱" else controller.resendEmail(email.text.toString())
            }
            dialog.getButton(androidx.appcompat.app.AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                if (email.text.isNullOrBlank()) { email.error = "请填写邮箱"; return@setOnClickListener }
                if (mode == "confirm" && code.text.isNullOrBlank()) { code.error = "请填写邮件验证码"; return@setOnClickListener }
                if (password.text.isNullOrEmpty()) { password.error = "请填写密码"; return@setOnClickListener }
                controller.addAccountPassword(mode, email.text.toString(), password.text.toString(), code.text.toString())
                password.text?.clear(); dialog.dismiss()
            }
        }
        dialog.setOnDismissListener { password.text?.clear(); if (accountDialog === dialog) accountDialog = null }
        dialog.show()
    }
    private fun openUrl(url: String) {
        if (!Presentation.safeExternalUrl(url)) return
        runCatching { startActivity(Intent(Intent.ACTION_VIEW, url.toUri())) }.onFailure {
            MaterialAlertDialogBuilder(this).setMessage("当前设备没有可打开此链接的浏览器。").setPositiveButton("知道了", null).show()
        }
    }
}
