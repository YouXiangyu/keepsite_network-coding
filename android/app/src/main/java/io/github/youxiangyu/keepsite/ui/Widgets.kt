package io.github.youxiangyu.keepsite.ui

import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Rect
import android.graphics.drawable.Drawable
import android.graphics.drawable.GradientDrawable
import android.text.InputFilter
import android.text.TextUtils
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.content.res.AppCompatResources
import androidx.appcompat.widget.AppCompatEditText
import androidx.core.view.AccessibilityDelegateCompat
import androidx.core.view.ViewCompat
import androidx.core.view.accessibility.AccessibilityNodeInfoCompat
import com.google.android.material.button.MaterialButton
import io.github.youxiangyu.keepsite.R
import io.github.youxiangyu.keepsite.ui.Design.Radius
import io.github.youxiangyu.keepsite.ui.Design.Size
import io.github.youxiangyu.keepsite.ui.Design.Space
import io.github.youxiangyu.keepsite.ui.Design.Type

fun Context.dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()
fun Context.column(): LinearLayout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
fun Context.row(): LinearLayout = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
fun Context.rounded(color: Int, radius: Int, stroke: Int? = null): GradientDrawable = GradientDrawable().apply {
    setColor(getColor(color)); cornerRadius = dp(radius).toFloat()
    if (stroke != null) setStroke(dp(1), getColor(stroke))
}
fun Context.pill(color: Int): GradientDrawable = GradientDrawable().apply { setColor(getColor(color)); cornerRadius = Float.MAX_VALUE }
fun Context.ripple(borderless: Boolean = false): Drawable? {
    val value = TypedValue()
    theme.resolveAttribute(if (borderless) android.R.attr.selectableItemBackgroundBorderless else android.R.attr.selectableItemBackground, value, true)
    return AppCompatResources.getDrawable(this, value.resourceId)
}
fun TextView.style(type: Type) {
    textSize = type.size; typeface = type.weight.typeface; setLineSpacing(0f, type.lineSpacing)
}
fun Context.label(value: String, type: Type = Type.Body, color: Int = R.color.mv_ink, heading: Boolean = false): TextView = TextView(this).apply {
    text = value; style(type); setTextColor(getColor(color))
    if (heading) ViewCompat.setAccessibilityHeading(this, true)
}

enum class ButtonKind { Primary, Secondary, Text, Quiet, Danger }

private class ActionButton(context: Context) : MaterialButton(context) {
    override fun setEnabled(enabled: Boolean) { super.setEnabled(enabled); alpha = if (enabled) 1f else Size.DISABLED_ALPHA }
}
fun Context.action(value: String, kind: ButtonKind = ButtonKind.Secondary, clicked: () -> Unit): MaterialButton = ActionButton(this).apply {
    val (fill, ink) = when (kind) {
        ButtonKind.Primary -> R.color.mv_ink to R.color.mv_background
        ButtonKind.Secondary -> R.color.mv_soft to R.color.mv_ink
        ButtonKind.Text -> android.R.color.transparent to R.color.mv_ink
        ButtonKind.Quiet -> android.R.color.transparent to R.color.mv_muted
        ButtonKind.Danger -> R.color.mv_error_soft to R.color.mv_error
    }
    val textual = kind == ButtonKind.Text || kind == ButtonKind.Quiet
    text = value; isAllCaps = false; letterSpacing = 0f; style(if (textual) Type.TextButton else Type.Button)
    insetTop = 0; insetBottom = 0; elevation = 0f; stateListAnimator = null
    cornerRadius = dp(Radius.CONTROL)
    backgroundTintList = ColorStateList.valueOf(getColor(fill)); setTextColor(getColor(ink))
    rippleColor = ColorStateList.valueOf(getColor(if (kind == ButtonKind.Primary) R.color.mv_muted else R.color.mv_line))
    iconTint = ColorStateList.valueOf(getColor(ink)); iconPadding = dp(Space.S)
    minHeight = dp(if (textual) Size.TOUCH else Size.BUTTON); minimumHeight = minHeight
    minWidth = dp(Size.TOUCH); minimumWidth = minWidth
    setPadding(dp(Space.L), 0, dp(Space.L), 0)
    setOnClickListener { clicked() }
}
fun Context.iconAction(icon: Int, description: String, clicked: () -> Unit): ImageButton = ImageButton(this).apply {
    setImageResource(icon); contentDescription = description; background = ripple(borderless = true)
    setColorFilter(getColor(R.color.mv_ink)); scaleType = ImageView.ScaleType.CENTER
    setOnClickListener { clicked() }
}

private class FormInput(context: Context) : AppCompatEditText(context) {
    // View subtracts its own scroll offset while mapping this rect to ancestors.
    // Include TextView's internal offset so the whole field, not just its caret, stays visible.
    override fun requestRectangleOnScreen(rectangle: Rect, immediate: Boolean): Boolean =
        super.requestRectangleOnScreen(Rect(scrollX, scrollY, scrollX + width, scrollY + height + context.dp(8)), immediate)
    override fun setEnabled(enabled: Boolean) { super.setEnabled(enabled); alpha = if (enabled) 1f else Size.DISABLED_ALPHA }
}

fun Context.input(title: String, max: Int = 200): EditText = FormInput(this).apply {
    hint = title; contentDescription = title; setSingleLine(true); style(Type.Body)
    setTextColor(getColor(R.color.mv_ink)); setHintTextColor(getColor(R.color.mv_muted))
    background = rounded(R.color.mv_soft, Radius.CONTROL); backgroundTintList = null
    setPadding(dp(Space.L), 0, dp(Space.L), 0); minHeight = dp(Size.INPUT); minimumHeight = minHeight
    imeOptions = imeOptions or EditorInfo.IME_FLAG_NO_EXTRACT_UI
    filters = arrayOf(InputFilter.LengthFilter(max)); isSaveEnabled = false
}

/** Adds a full-width child, separated from the previous one by [top] dp. */
fun LinearLayout.add(view: View, top: Int = 0) {
    addView(view, LinearLayout.LayoutParams(-1, -2).apply { topMargin = context.dp(top) })
}
fun Context.panel(): LinearLayout = column().apply {
    background = rounded(R.color.mv_background, Radius.PANEL, R.color.mv_line)
    setPadding(dp(Space.L), dp(Space.S), dp(Space.L), dp(Space.S))
}
fun Context.divider(): View = View(this).apply {
    setBackgroundColor(getColor(R.color.mv_line)); importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
}
fun LinearLayout.addDivider(top: Int = 0) {
    addView(context.divider(), LinearLayout.LayoutParams(-1, 1).apply { topMargin = context.dp(top) })
}
fun Context.infoRow(name: String, value: String, valueColor: Int = R.color.mv_ink): LinearLayout = row().apply {
    gravity = Gravity.TOP; minimumHeight = dp(Size.TOUCH); setPadding(0, dp(Space.M), 0, dp(Space.M))
    addView(label(name, Type.Label, R.color.mv_muted).apply { setPadding(0, dp(1), 0, 0) }, LinearLayout.LayoutParams(dp(Size.INFO_LABEL), -2))
    addView(label(value, color = valueColor), LinearLayout.LayoutParams(0, -2, 1f))
}
fun Context.listRow(icon: Int, title: String, danger: Boolean = false, clicked: () -> Unit): LinearLayout = row().apply {
    val ink = if (danger) R.color.mv_error else R.color.mv_ink
    minimumHeight = dp(Size.BUTTON); isClickable = true; isFocusable = true; background = ripple()
    setOnClickListener { clicked() }
    ViewCompat.setAccessibilityDelegate(this, object : AccessibilityDelegateCompat() {
        override fun onInitializeAccessibilityNodeInfo(host: View, info: AccessibilityNodeInfoCompat) {
            super.onInitializeAccessibilityNodeInfo(host, info); info.className = Button::class.java.name
        }
    })
    addView(ImageView(context).apply {
        setImageResource(icon); setColorFilter(getColor(ink)); importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
    }, LinearLayout.LayoutParams(dp(Size.ICON), dp(Size.ICON)))
    addView(label(title, Type.Subtitle, ink).apply { setPadding(dp(Space.L), 0, dp(Space.S), 0) }, LinearLayout.LayoutParams(0, -2, 1f))
    addView(ImageView(context).apply {
        setImageResource(R.drawable.ic_chevron_right); setColorFilter(getColor(R.color.mv_muted))
        importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
    }, LinearLayout.LayoutParams(dp(Size.INDICATOR), dp(Size.INDICATOR)))
}
fun Context.avatar(name: String): TextView = label(name.trim().take(1).uppercase().ifBlank { "?" }, Type.Title).apply {
    gravity = Gravity.CENTER; background = pill(R.color.mv_soft); importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
}
fun TextView.singleLine() { maxLines = 1; ellipsize = TextUtils.TruncateAt.END }

class BoundedColumn(context: Context) : LinearLayout(context) {
    var maxWidth = Int.MAX_VALUE
    init { orientation = VERTICAL }
    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val width = View.MeasureSpec.getSize(widthMeasureSpec)
        val bounded = if (width > maxWidth) View.MeasureSpec.makeMeasureSpec(maxWidth, View.MeasureSpec.EXACTLY) else widthMeasureSpec
        super.onMeasure(bounded, heightMeasureSpec)
    }
}

/** Reserves the cover's proportions before async decoding, without cropping ticket text. */
class TicketCoverView(context: Context) : androidx.appcompat.widget.AppCompatImageView(context) {
    private var ratio = 1f
    fun setRatio(width: Int, height: Int) {
        ratio = width.coerceAtLeast(1).toFloat() / height.coerceAtLeast(1)
        requestLayout()
    }
    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val width = View.MeasureSpec.getSize(widthMeasureSpec)
        val height = (width / ratio).toInt().coerceIn(context.dp(80), context.dp(420))
        super.onMeasure(widthMeasureSpec, View.MeasureSpec.makeMeasureSpec(height, View.MeasureSpec.EXACTLY))
    }
}
