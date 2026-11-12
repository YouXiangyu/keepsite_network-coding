package io.github.youxiangyu.keepsite.updates

import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.ByteArrayOutputStream
import java.io.Closeable
import java.io.IOException
import java.net.URI
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.util.concurrent.TimeUnit

/** DownloadManager can persist the HTTPS target of a 301 in COLUMN_URI. */
internal fun trustedUpdateDownloadUri(value: String): Boolean = try {
    val uri = URI(value)
    uri.scheme == "https" && uri.host in setOf("github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com")
        && uri.port in setOf(-1, 443) && uri.rawUserInfo == null && uri.rawFragment == null
} catch (_: java.net.URISyntaxException) { false }

/** Separate public client: never shares HttpApi, session cookies or credentials. */
class GithubUpdateSource internal constructor(private val client: OkHttpClient = OkHttpClient.Builder()
    .connectTimeout(5, TimeUnit.SECONDS).readTimeout(15, TimeUnit.SECONDS).callTimeout(30, TimeUnit.SECONDS)
    .followRedirects(false).followSslRedirects(false).build()) : UpdateSource, Closeable {
    override fun latest(): UpdateSnapshot? {
        val raw = get("https://api.github.com/repos/${UpdatePolicy.REPOSITORY}/releases/latest", 256 * 1024, true) ?: return null
        val release = utf8(raw)
        val candidate = UpdatePolicy.release(release) ?: return null
        val record = candidate.verification?.let { utf8(requireNotNull(get(it.url, 64 * 1024, false))) }
        return UpdateSnapshot(release, record).also { it.checked() }
    }
    private fun get(initial: String, limit: Int, allowMissing: Boolean): ByteArray? {
        var url = initial
        repeat(5) {
            val uri = URI(url)
            require(trustedUpdateDownloadUri(url) || url == "https://api.github.com/repos/${UpdatePolicy.REPOSITORY}/releases/latest") { "版本查询跳转地址不受信任。" }
            val request = Request.Builder().url(url).header("Accept", "application/vnd.github+json")
                .header("Cache-Control", "no-cache").header("User-Agent", "keepsite-android-updater").build()
            client.newCall(request).execute().use { response ->
                if (response.code in setOf(301, 302, 303, 307, 308)) {
                    url = uri.resolve(response.header("Location") ?: throw IOException("版本查询缺少跳转地址。")).toString()
                } else {
                    if (response.code == 404 && allowMissing) return null
                    if (response.code != 200) throw IOException("GitHub 返回 HTTP ${response.code}，请稍后重试。")
                    val body = response.body ?: throw IOException("版本查询正文缺失。")
                    require(body.contentLength() <= limit) { "版本查询正文过大。" }
                    return body.byteStream().use { input ->
                        val bytes = ByteArrayOutputStream(); val block = ByteArray(8192)
                        while (true) {
                            val count = input.read(block)
                            if (count < 0) break
                            require(bytes.size() + count <= limit) { "版本查询正文过大。" }
                            bytes.write(block, 0, count)
                        }
                        bytes.toByteArray()
                    }
                }
            }
        }
        throw IOException("版本查询跳转次数过多。")
    }
    private fun utf8(bytes: ByteArray) = Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
        .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes)).toString()
    override fun close() { client.dispatcher.cancelAll(); client.connectionPool.evictAll(); client.dispatcher.executorService.shutdown() }
}
