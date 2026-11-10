package io.github.youxiangyu.keepsite

import android.content.Context
import android.content.res.Configuration
import android.graphics.Rect
import android.os.Bundle
import android.os.SystemClock
import android.view.ContextThemeWrapper
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.isVisible
import com.google.android.material.button.MaterialButton
import io.github.youxiangyu.keepsite.databinding.ActivityMainBinding
import io.github.youxiangyu.keepsite.ui.*
import io.github.youxiangyu.keepsite.ui.Design.Size
import io.github.youxiangyu.keepsite.ui.Design.Space
import io.github.youxiangyu.keepsite.ui.Design.Type
import io.github.youxiangyu.keepsite.ui.Design.Radius

/** Isolated window for the production layout; callbacks never submit business data. */
class EditorLayoutHostActivity : AppCompatActivity() {
    lateinit var editor: TicketEditorLayout; private set
    lateinit var save: Button; private set
    lateinit var cancel: Button; private set
    lateinit var date: Button; private set
    lateinit var fields: List<EditText>; private set
    lateinit var status: TextView; private set
    lateinit var notice: TextView; private set
    lateinit var photo: Button; private set
    lateinit var progress: ProgressBar; private set
    var busyState = ""; private set
    var pendingState = false; private set
    var noticeSource = "none"; private set
    var saveClicks = 0; private set
    var cancelClicks = 0; private set
    private var keyboardVisible = false
    private var feedbackError = ""
    val diagnosticHostId: String = java.util.UUID.randomUUID().toString()
    data class TouchButton(val x: Int, val y: Int, val width: Int, val height: Int, val enabled: Boolean, val clicks: Int)
    data class TouchFact(
        val phase: String, val action: Int, val downTime: Long, val eventTime: Long, val uptime: Long,
        val rawX: Float, val rawY: Float, val localX: Float, val localY: Float,
        val save: TouchButton, val cancel: TouchButton, val focusedField: Int, val focusedClass: String?,
        val imeVisible: Boolean, val imeBottom: Int, val windowFocus: Boolean,
        val handled: Boolean?, val dispatchFailure: String?,
    )
    private val touchFacts = java.util.ArrayDeque<TouchFact>()
    var touchRecordsEvicted = 0; private set
    var touchRecordingErrors = 0; private set
    fun touchDiagnosticSnapshot(): List<TouchFact> = touchFacts.toList()

    override fun dispatchTouchEvent(event: MotionEvent): Boolean {
        val tracked = event.actionMasked == MotionEvent.ACTION_DOWN || event.actionMasked == MotionEvent.ACTION_UP || event.actionMasked == MotionEvent.ACTION_CANCEL
        if (tracked) recordTouch("before", event)
        try {
            val handled = super.dispatchTouchEvent(event)
            if (tracked) recordTouch("after", event, handled)
            return handled
        } catch (failure: Throwable) {
            if (tracked) recordTouch("after", event, dispatchFailure = failure.javaClass.name)
            throw failure
        }
    }

    private fun recordTouch(phase: String, event: MotionEvent, handled: Boolean? = null, dispatchFailure: String? = null) {
        try {
            if (!::save.isInitialized || !::cancel.isInitialized || !::fields.isInitialized) {
                touchRecordingErrors++
                return
            }
            fun button(view: Button, clicks: Int): TouchButton {
                val location = IntArray(2); view.getLocationOnScreen(location)
                return TouchButton(location[0], location[1], view.width, view.height, view.isEnabled, clicks)
            }
            val insets = ViewCompat.getRootWindowInsets(window.decorView)
            val fact = TouchFact(phase, event.actionMasked, event.downTime, event.eventTime, SystemClock.uptimeMillis(),
                event.rawX, event.rawY, event.x, event.y, button(save, saveClicks), button(cancel, cancelClicks),
                fields.indexOfFirst { it === currentFocus }, currentFocus?.javaClass?.name,
                insets?.isVisible(WindowInsetsCompat.Type.ime()) == true,
                insets?.getInsets(WindowInsetsCompat.Type.ime())?.bottom ?: 0, hasWindowFocus(), handled, dispatchFailure)
            if (touchFacts.size == 64) { touchFacts.removeFirst(); touchRecordsEvicted++ }
            touchFacts.addLast(fact)
        } catch (_: Throwable) {
            // Diagnostic recording must not consume input or hide a dispatch failure.
            touchRecordingErrors++
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        val uiContext = if (intent.getBooleanExtra("largeText", false)) ContextThemeWrapper(
            createConfigurationContext(Configuration(resources.configuration).apply { fontScale = 2f }), R.style.Theme_keepsite
        ) else this
        val binding = ActivityMainBinding.inflate(layoutInflater.cloneInContext(uiContext))
        binding.root.importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS
        setContentView(binding.root)
        WindowCompat.getInsetsController(window, binding.root).isAppearanceLightNavigationBars = true
        binding.brand.isVisible = false; binding.connection.isVisible = false
        binding.notice.isVisible = false; binding.progress.isVisible = false
        progress = binding.progress
        notice = binding.notice.apply {
            style(Type.Label)
            setPadding(dp(Space.M), dp(Space.S), dp(Space.M), dp(Space.S))
            (layoutParams as ViewGroup.MarginLayoutParams).setMargins(dp(Space.PAGE), dp(Space.S), dp(Space.PAGE), 0)
        }
        with(uiContext) { listOf("票夹", "录入", "我的").forEach { text ->
            binding.navigation.addView(label(text, Type.Nav).apply {
                gravity = Gravity.CENTER; minimumHeight = dp(Size.NAV_ITEM)
            }, LinearLayout.LayoutParams(0, -2, 1f))
        } }
        binding.content.addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ ->
            if (keyboardVisible) currentFocus?.takeIf { it is EditText }?.let { focused ->
                focused.requestRectangleOnScreen(Rect(0, 0, focused.width, focused.height + dp(8)), true)
            }
        }
        ViewCompat.setOnApplyWindowInsetsListener(binding.root) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            keyboardVisible = insets.isVisible(WindowInsetsCompat.Type.ime())
            binding.navigation.isVisible = !keyboardVisible
            insets
        }
        buildEditor(binding, uiContext)
        renderState(error = savedInstanceState?.getString("feedbackError") ?: if (intent.getBooleanExtra("longHint", false))
            "本地合成错误提示：保存未完成，草稿仍保留；检查末尾状态完整滚达操作栏上方。".repeat(2) else "")
        binding.root.isFocusableInTouchMode = true; binding.root.requestFocus()
    }

    private fun buildEditor(binding: ActivityMainBinding, context: Context) = with(context) {
        editor = TicketEditorLayout(this)
        binding.content.addView(editor.root, ViewGroup.LayoutParams(-1, -1))
        val body = editor.body
        body.add(label("录入电影票", Type.Display, heading = true), Space.S)
        body.add(label("隔离布局Host · 本地合成票面，不上传或保存业务数据。", color = R.color.mv_muted), Space.S)
        body.addView(ImageView(this).apply {
            contentDescription = "本地合成票面预览"; setImageResource(R.drawable.ic_ticket)
            setBackgroundColor(getColor(R.color.mv_soft)); scaleType = ImageView.ScaleType.FIT_CENTER
        }, LinearLayout.LayoutParams(-1, dp(Size.PREVIEW)).apply { topMargin = dp(Space.XL) })
        body.add(label("", Type.Caption, R.color.mv_muted), Space.XS)
        photo = action("选择或替换票面") {}.apply {
            setIconResource(R.drawable.ic_image); iconGravity = MaterialButton.ICON_GRAVITY_TEXT_START
        }.also { body.add(it, Space.M) }
        fields = listOf("电影名称", "影院", "影厅", "座位").mapIndexed { index, name ->
            body.add(label(name, Type.Label, R.color.mv_muted), if (index == 0) Space.XL else Space.L)
            input(name, if (index == 0) 120 else 200).also { body.add(it, Space.S) }
        }
        body.add(row().apply {
            addView(label("放映时间", Type.Label, R.color.mv_muted), LinearLayout.LayoutParams(0, -2, 1f))
            addView(action("清除放映时间", ButtonKind.Quiet) {}.apply { style(Type.Label); setPadding(dp(Space.S), 0, 0, 0) })
        }, Space.S)
        date = action("选择日期和时间") {}.apply {
            style(Type.Body); gravity = Gravity.START or Gravity.CENTER_VERTICAL; textAlignment = View.TEXT_ALIGNMENT_VIEW_START
            setIconResource(R.drawable.ic_calendar); iconGravity = MaterialButton.ICON_GRAVITY_END
            minHeight = dp(Size.INPUT)
        }.also { body.add(it) }
        status = label("", Type.Caption, R.color.mv_muted).also { body.add(it, Space.XL) }
        save = action("保存电影票", ButtonKind.Primary) { saveClicks++ }
        cancel = action("取消", ButtonKind.Text) { cancelClicks++ }
        editor.addActions(save, cancel)
    }

    /** Mirrors MainActivity.render/updateEditor without a repository, account or network. */
    fun renderState(error: String = feedbackError, busy: String = "", message: String = "",
                    pending: Boolean = false, hasPhoto: Boolean = true, ticketId: String? = null) {
        feedbackError = error
        busyState = busy; pendingState = pending
        noticeSource = when { error.isNotBlank() -> "synthetic_error"; busy.isNotBlank() -> "busy"; message.isNotBlank() -> "message"; else -> "none" }
        progress.isVisible = busy.isNotBlank()
        notice.text = error.ifBlank { busy.ifBlank { message } }
        notice.isVisible = notice.text.isNotBlank()
        notice.setTextColor(getColor(if (error.isBlank()) R.color.mv_muted else R.color.mv_error))
        notice.background = rounded(if (error.isBlank()) R.color.mv_soft else R.color.mv_error_soft, Radius.CONTROL)
        fields.forEach { it.isEnabled = !pending && busy.isBlank() }
        date.isEnabled = !pending && busy.isBlank()
        photo.isEnabled = !pending && busy.isBlank()
        save.isEnabled = busy.isBlank() && (hasPhoto || ticketId != null)
        save.text = if (pending) "核对并重试保存" else "保存电影票"
        status.text = when {
            pending -> "保存结果尚未确认。请核对后重试，系统会先检查之前的提交，避免重复保存。"
            hasPhoto -> "选中：本地合成票面"
            ticketId != null -> "保留原始票面"
            else -> "请先选择一张票面"
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putString("feedbackError", feedbackError)
        super.onSaveInstanceState(outState)
    }
}
