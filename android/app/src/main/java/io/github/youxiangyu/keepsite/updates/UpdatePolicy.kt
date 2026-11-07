package io.github.youxiangyu.keepsite.updates

import org.json.JSONObject
import java.net.URI
import java.security.MessageDigest

/** Public release data only. The Android bridge supplies identities read by PackageManager. */
object UpdatePolicy {
    const val PACKAGE = "io.github.youxiangyu.keepsite"
    const val REPOSITORY = "YouXiangyu/keepsite_network-coding"
    const val MAX_APK_BYTES = 256L * 1024 * 1024
    private const val MAX_METADATA_BYTES = 64 * 1024L
    private const val PREFIX = "/$REPOSITORY/releases/download/"

    data class Asset(val id: Long, val name: String, val bytes: Long, val sha256: String, val url: String)
    data class Candidate(val tag: String, val notes: String, val apk: Asset, val verification: Asset?) {
        val identity get() = "${apk.id}:${apk.sha256}:${apk.bytes}:" +
            (verification?.let { "${it.id}:${it.sha256}:${it.bytes}" } ?: "no-record")
    }
    data class PackageIdentity(val name: String, val code: Long, val signers: Set<String>, val minSdk: Int = 26) {
        init {
            require(code > 0 && minSdk > 0 && signers.isNotEmpty())
            signers.forEach(::requireSha256)
        }
    }
    class Verification internal constructor(val candidateIdentity: String, val code: Long, val versionName: String, val certificate: String)
    enum class Action { CURRENT, DOWNLOAD }

    fun release(raw: String): Candidate? {
        require(raw.toByteArray(Charsets.UTF_8).size <= 256 * 1024) { "版本信息过大。" }
        val value = JSONObject(raw)
        if (flag(value, "draft") || flag(value, "prerelease")) return null
        val tag = text(value, "tag_name", 200)
        val notes = value.opt("body").let { if (it is String) it.take(20_000) else "" }
        val assets = value.getJSONArray("assets")
        require(assets.length() <= 100) { "版本附件过多。" }
        val uploaded = (0 until assets.length()).map { assets.getJSONObject(it) }
            .filter { it.opt("state") == "uploaded" }
        val apks = uploaded.filter { (it.opt("name") as? String)?.endsWith(".apk", ignoreCase = true) == true }
        if (apks.isEmpty()) return null
        require(apks.size == 1) { "版本包含多个 APK，无法确定适用安装包。" }
        val records = uploaded.filter { it.opt("name") == "apk-verification.json" }
        require(records.size <= 1) { "版本验证记录不唯一。" }
        return Candidate(tag, notes, asset(apks.single(), tag, MAX_APK_BYTES),
            records.singleOrNull()?.let { asset(it, tag, MAX_METADATA_BYTES) })
    }

    /** A present but invalid record must fail; only an absent asset permits APK probing. */
    fun verification(candidate: Candidate, bytes: ByteArray): Verification {
        val record = requireNotNull(candidate.verification) { "此版本没有验证记录。" }
        require(bytes.size.toLong() == record.bytes && digest(bytes) == record.sha256) { "版本验证记录的大小或摘要不符。" }
        val value = JSONObject(bytes.toString(Charsets.UTF_8))
        require(text(value, "applicationId", 200) == PACKAGE) { "版本验证记录属于其他应用。" }
        require(number(value, "bytes") == candidate.apk.bytes && sha(value, "sha256") == candidate.apk.sha256) { "版本验证记录与 APK 不匹配。" }
        val code = number(value, "versionCode")
        require(code > 0) { "版本编号无效。" }
        return Verification(candidate.identity, code, text(value, "versionName", 100), sha(value, "certificateSha256"))
    }

    fun action(candidate: Candidate, verification: Verification?, installed: PackageIdentity): Action {
        binding(candidate, verification)
        require(installed.name == PACKAGE) { "当前应用身份不符。" }
        if (verification == null) return Action.DOWNLOAD
        if (verification.code <= installed.code) return Action.CURRENT
        require(installed.signers == setOf(verification.certificate)) { "此安装包与当前应用签名不同，不能覆盖更新。" }
        return Action.DOWNLOAD
    }

    /** Call after byte verification, and repeat against the currently installed identity before handing to the OS. */
    fun newerArchive(candidate: Candidate, verification: Verification?, installed: PackageIdentity, archive: PackageIdentity, deviceApi: Int): Boolean {
        binding(candidate, verification)
        require(installed.name == PACKAGE && archive.name == PACKAGE) { "安装包属于其他应用。" }
        require(archive.signers == installed.signers) { "安装包与当前应用签名不同。" }
        require(archive.minSdk <= deviceApi) { "此版本不支持当前 Android 系统。" }
        if (verification != null) {
            require(archive.code == verification.code && archive.signers == setOf(verification.certificate)) { "安装包身份与版本验证记录不符。" }
        }
        return archive.code > installed.code
    }

    private fun binding(candidate: Candidate, verification: Verification?) {
        require((candidate.verification == null) == (verification == null)) { "必须先核验本版本提供的验证记录。" }
        require(verification == null || verification.candidateIdentity == candidate.identity) { "候选版本已变化，请重新检查。" }
    }

    private fun asset(value: JSONObject, tag: String, limit: Long): Asset {
        val name = text(value, "name", 240)
        require('/' !in name && '\\' !in name) { "版本附件名称无效。" }
        val id = number(value, "id")
        val size = number(value, "size")
        require(id > 0 && size in 1..limit) { "版本附件大小或标识无效。" }
        val digest = text(value, "digest", 71)
        require(digest.startsWith("sha256:")) { "版本附件缺少可核验的 SHA-256 摘要。" }
        val hash = digest.removePrefix("sha256:").lowercase().also(::requireSha256)
        val url = text(value, "browser_download_url", 2048)
        val uri = URI(url)
        require(uri.scheme == "https" && uri.host == "github.com" && uri.port == -1 && uri.rawUserInfo == null
            && uri.rawQuery == null && uri.rawFragment == null && uri.path == "$PREFIX$tag/$name"
            && uri.path.split('/').none { it == "." || it == ".." }) { "版本附件地址不属于官方仓库。" }
        return Asset(id, name, size, hash, url)
    }

    private fun flag(value: JSONObject, key: String): Boolean = (value.opt(key) as? Boolean)
        ?: throw IllegalArgumentException("版本状态信息缺失。")
    private fun text(value: JSONObject, key: String, max: Int): String = (value.opt(key) as? String)
        ?.takeIf { it.isNotBlank() && it.length <= max } ?: throw IllegalArgumentException("版本字段 $key 无效。")
    private fun number(value: JSONObject, key: String): Long = when (val raw = value.opt(key)) {
        is Int -> raw.toLong()
        is Long -> raw
        else -> throw IllegalArgumentException("版本数字字段 $key 无效。")
    }
    private fun sha(value: JSONObject, key: String) = text(value, key, 64).lowercase().also(::requireSha256)
    internal fun requireSha256(value: String) { require(Regex("[0-9a-f]{64}").matches(value)) { "SHA-256 摘要格式无效。" } }
    internal fun hex(bytes: ByteArray) = bytes.joinToString("") { "%02x".format(it) }
    internal fun digest(bytes: ByteArray) = hex(MessageDigest.getInstance("SHA-256").digest(bytes))
}
