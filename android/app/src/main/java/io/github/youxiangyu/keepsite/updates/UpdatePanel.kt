package io.github.youxiangyu.keepsite.updates

import android.content.Context
import android.view.View
import android.widget.LinearLayout
import io.github.youxiangyu.keepsite.R
import io.github.youxiangyu.keepsite.ui.*
import io.github.youxiangyu.keepsite.ui.Design.Radius
import io.github.youxiangyu.keepsite.ui.Design.Space
import io.github.youxiangyu.keepsite.ui.Design.Type

enum class UpdateAction { CHECK, RETRY, CANCEL, MOBILE, INSTALL }

/** Updates only its own text/buttons; never rebuilds the enclosing auth form. */
class UpdatePanel(context: Context, private val action: (UpdateAction, String?) -> Unit) : LinearLayout(context) {
    private val version = context.label("", Type.Caption, R.color.mv_muted)
    private val detail = context.label("", Type.Label)
    private var identity: String? = null
    private var primaryAction = UpdateAction.CHECK
    private var secondaryAction = UpdateAction.MOBILE
    private val primary = context.action("检查更新", ButtonKind.Text) { action(primaryAction, identity) }
    private val secondary = context.action("本次使用移动网络", ButtonKind.Text) { action(secondaryAction, identity) }
    init {
        orientation = VERTICAL; background = context.rounded(R.color.mv_soft, Radius.CONTROL)
        setPadding(context.dp(Space.M), context.dp(Space.S), context.dp(Space.M), context.dp(Space.S))
        addView(version); addView(detail)
        addView(context.row().apply {
            addView(primary, LayoutParams(0, -2, 1f)); addView(secondary, LayoutParams(0, -2, 1f))
        })
    }
    fun render(state: UpdateUiState) {
        identity = state.candidateIdentity
        version.text = "应用更新 · 当前 ${state.version}"
        var alternate = false
        secondaryAction = UpdateAction.MOBILE; secondary.text = "本次使用移动网络"
        val sameHeld = state.held != null && (state.candidateIdentity == null || state.candidateIdentity == state.heldIdentity)
        when {
            state.error.isNotBlank() -> {
                detail.text = state.error; primaryAction = if (state.installationFailure) UpdateAction.INSTALL else UpdateAction.RETRY
                primary.text = if (primaryAction == UpdateAction.INSTALL) "再次安装" else "重新检查"
                if (state.held != null) { alternate = true; secondaryAction = UpdateAction.INSTALL; secondary.text = "安装已下载版本" }
            }
            state.status is UpdateStatus.Ready || sameHeld && state.status !is UpdateStatus.Transferring -> {
                detail.text = if (sameHeld && state.installNotice.isNotBlank()) state.installNotice else "安装包已就绪，可由系统完成安装。"
                primaryAction = UpdateAction.INSTALL; primary.text = "安装更新"
                alternate = true; secondaryAction = UpdateAction.CHECK; secondary.text = "检查更新"
            }
            state.status is UpdateStatus.Transferring -> {
                val task = state.status.task
                detail.text = if (task.phase == DownloadPhase.PAUSED || task.phase == DownloadPhase.PENDING) "等待可用网络，下载可在后台继续。"
                    else if (task.total > 0) "正在下载更新：${(task.downloaded * 100 / task.total).coerceIn(0, 100)}%" else "正在后台下载更新…"
                primaryAction = UpdateAction.CANCEL; primary.text = "取消下载"; alternate = true
            }
            state.status == UpdateStatus.AwaitingOwnership -> {
                detail.text = "正在核对已有下载，请稍后重试。"
                primaryAction = UpdateAction.RETRY; primary.text = "重试"
            }
            state.status is UpdateStatus.Failed -> {
                detail.text = "下载失败，请重试。"
                primaryAction = UpdateAction.RETRY; primary.text = "重试"; alternate = true
            }
            state.status == UpdateStatus.Cancelled -> {
                detail.text = "下载已取消。"
                primaryAction = UpdateAction.RETRY; primary.text = "重试"; alternate = true
            }
            state.status is UpdateStatus.Available -> {
                detail.text = "${if (state.knownUpdate) "发现新版本" else "发现可下载的安装包"}（${state.candidateBytes / (1024 * 1024)} MiB）。"
                primaryAction = UpdateAction.RETRY; primary.text = "下载更新"; alternate = true
            }
            else -> {
                detail.text = if (state.busy) "正在检查应用更新…" else if (state.checked) "当前没有可用更新。" else "检查应用更新。"
                primaryAction = UpdateAction.CHECK; primary.text = "检查更新"
            }
        }
        secondary.visibility = if (alternate && (secondaryAction != UpdateAction.MOBILE || identity != null)) View.VISIBLE else View.GONE
        primary.isEnabled = !state.busy; secondary.isEnabled = !state.busy
    }
}
