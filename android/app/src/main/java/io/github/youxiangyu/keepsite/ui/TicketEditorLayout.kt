package io.github.youxiangyu.keepsite.ui

import android.content.Context
import android.view.Gravity
import android.view.View
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import io.github.youxiangyu.keepsite.ui.Design.Size
import io.github.youxiangyu.keepsite.ui.Design.Space

class TicketEditorLayout(context: Context) {
    val body = BoundedColumn(context).apply {
        maxWidth = context.dp(Size.CONTENT_MAX)
        setPadding(context.dp(Space.PAGE), context.dp(Space.S), context.dp(Space.PAGE), context.dp(Space.XXL))
    }
    val scroll = ScrollView(context).apply {
        isFillViewport = true
        addView(body, FrameLayout.LayoutParams(-1, -2, Gravity.CENTER_HORIZONTAL))
    }
    private val actions = BoundedColumn(context).apply {
        maxWidth = context.dp(Size.CONTENT_MAX)
        setPadding(context.dp(Space.PAGE), context.dp(Space.XS), context.dp(Space.PAGE), context.dp(Space.XS))
    }
    val footer = context.column().apply {
        visibility = View.GONE; setBackgroundColor(context.getColor(io.github.youxiangyu.keepsite.R.color.mv_background))
        addView(context.divider(), LinearLayout.LayoutParams(-1, 1))
        addView(actions, LinearLayout.LayoutParams(-1, -2).apply { gravity = Gravity.CENTER_HORIZONTAL })
    }
    val root: View = context.column().apply {
        addView(scroll, LinearLayout.LayoutParams(-1, 0, 1f))
        addView(footer, LinearLayout.LayoutParams(-1, -2))
    }

    fun addActions(save: View, cancel: View) {
        actions.addView(rowForActions(save, cancel))
        footer.visibility = View.VISIBLE
    }

    private fun rowForActions(save: View, cancel: View) = actions.context.row().apply {
        addView(cancel, LinearLayout.LayoutParams(0, -2, 1f).apply { marginEnd = context.dp(Space.S) })
        addView(save, LinearLayout.LayoutParams(0, -2, 2f))
    }
}
