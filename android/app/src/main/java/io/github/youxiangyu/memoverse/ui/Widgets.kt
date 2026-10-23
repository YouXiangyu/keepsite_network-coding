package io.github.youxiangyu.memoverse.ui

import android.content.Context
import android.graphics.Typeface
import android.graphics.Rect
import android.text.InputFilter
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.widget.AppCompatEditText
import io.github.youxiangyu.memoverse.R

fun Context.dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()
fun Context.column(): LinearLayout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
fun Context.label(value: String, size: Float = 16f, heading: Boolean = false): TextView = TextView(this).apply {
    text = value; textSize = size; setTextColor(getColor(R.color.mv_ink)); setPadding(0, dp(6), 0, dp(6))
    if (heading) { setTypeface(typeface, Typeface.BOLD); androidx.core.view.ViewCompat.setAccessibilityHeading(this, true) }
}
fun Context.action(value: String, clicked: () -> Unit): Button = Button(this).apply {
    text = value; isAllCaps = false; minHeight = dp(48); minimumHeight = dp(48)
    setOnClickListener { clicked() }
}
private class FormInput(context: Context) : AppCompatEditText(context) {
    // Keep the entire field above the keyboard when TextView scrolls to its caret.
    override fun requestRectangleOnScreen(rectangle: Rect, immediate: Boolean): Boolean =
        super.requestRectangleOnScreen(Rect(0, 0, width, height + context.dp(8)), immediate)
}

fun Context.input(title: String, max: Int = 200): EditText = FormInput(this).apply {
    hint = title; contentDescription = title; minHeight = dp(48); setSingleLine(true)
    imeOptions = imeOptions or EditorInfo.IME_FLAG_NO_EXTRACT_UI
    filters = arrayOf(InputFilter.LengthFilter(max)); isSaveEnabled = false
}
fun LinearLayout.add(view: View) { addView(view, LinearLayout.LayoutParams(-1, -2)) }
