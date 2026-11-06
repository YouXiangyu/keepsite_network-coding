package io.github.youxiangyu.keepsite.ui

import android.content.Context
import android.view.Gravity
import android.view.View
import android.widget.FrameLayout
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
    val root: View = scroll

    fun addActions(save: View, cancel: View) {
        body.add(save, Space.M)
        body.add(cancel, Space.S)
    }
}
