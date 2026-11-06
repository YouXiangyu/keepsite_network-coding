package io.github.youxiangyu.keepsite

import android.graphics.Rect
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
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

/** Isolated window for the production layout; callbacks never submit business data. */
class EditorLayoutHostActivity : AppCompatActivity() {
    lateinit var editor: TicketEditorLayout; private set
    lateinit var save: Button; private set
    lateinit var cancel: Button; private set
    lateinit var date: Button; private set
    lateinit var fields: List<EditText>; private set
    var saveClicks = 0; private set
    var cancelClicks = 0; private set
    private var keyboardVisible = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        val binding = ActivityMainBinding.inflate(layoutInflater)
        binding.root.importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS
        setContentView(binding.root)
        WindowCompat.getInsetsController(window, binding.root).isAppearanceLightNavigationBars = true
        binding.brand.isVisible = false; binding.connection.isVisible = false
        binding.notice.isVisible = false; binding.progress.isVisible = false
        listOf("票夹", "录入", "我的").forEach { text ->
            binding.navigation.addView(label(text, Type.Nav).apply {
                gravity = Gravity.CENTER; minimumHeight = dp(Size.NAV_ITEM)
            }, LinearLayout.LayoutParams(0, -2, 1f))
        }
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
        body.add(action("选择或替换票面") {}.apply {
            setIconResource(R.drawable.ic_image); iconGravity = MaterialButton.ICON_GRAVITY_TEXT_START
        }, Space.M)
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
        body.add(label("本地合成预览 · 仅检查布局与系统窗口", Type.Caption, R.color.mv_muted), Space.XL)
        save = action("保存电影票", ButtonKind.Primary) { saveClicks++ }
        cancel = action("取消", ButtonKind.Text) { cancelClicks++ }
        editor.addActions(save, cancel)
        binding.root.isFocusableInTouchMode = true; binding.root.requestFocus()
    }
}
