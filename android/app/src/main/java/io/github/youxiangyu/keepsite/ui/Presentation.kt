package io.github.youxiangyu.keepsite.ui

import java.net.URI
import java.util.Locale

object Presentation {
    fun bytes(value: Long): String = if (value < 1024 * 1024) "${value / 1024} KB" else String.format(Locale.ROOT, "%.1f MB", value / (1024.0 * 1024))
    fun processing(status: String): String = when (status) {
        "not_started" -> "尚未开始自动处理"
        "queued" -> "等待处理"
        "ocr" -> "正在识别票面"
        "poster" -> "正在查找海报"
        "ready" -> "处理完成"
        "no_match" -> "未找到匹配海报，保留票面"
        "failed" -> "处理失败，可重试"
        else -> "处理状态：$status"
    }
    fun cloud(status: String): String = when (status) {
        "saved" -> "已保存至私有云存储"
        "not_configured" -> "云存储未配置，仅服务器可用"
        else -> "云存储状态：$status"
    }
    fun media(status: String, source: String): String {
        val location = when (source) { "peer" -> "同账号设备"; "s3" -> "私有云存储"; "server" -> "服务器"; "cache" -> "本机缓存"; else -> "" }
        val label = when (status) {
            "cached" -> "已缓存"
            "temporary" -> "临时查看 · 未写入本机缓存"
            "loading" -> "正在加载"
            "error" -> "图片加载失败"
            else -> status
        }
        return listOf(label, location).filter { it.isNotBlank() }.joinToString(" · ")
    }
    fun transfer(status: String): String = when (status) {
        "active" -> "传输中"; "complete" -> "完成"; "failed" -> "失败"; "cancelled" -> "已取消"; else -> "状态未知"
    }
    fun route(route: String): String = when (route) {
        "direct" -> "设备直连"; "relay" -> "中继"; "mixed" -> "传输路径已变化"; else -> "路径尚未确定"
    }
    fun safeExternalUrl(value: String): Boolean = runCatching {
        val uri = URI(value)
        uri.scheme == "https" && !uri.host.isNullOrBlank() && uri.rawUserInfo == null
    }.getOrDefault(false)
    fun callback(value: String): Boolean = runCatching {
        val uri = URI(value)
        uri.scheme == "io.github.youxiangyu.keepsite" && uri.host == null && uri.path == "/oauth/callback" && uri.rawFragment == null
    }.getOrDefault(false)
}
