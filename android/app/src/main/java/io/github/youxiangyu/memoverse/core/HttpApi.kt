package io.github.youxiangyu.memoverse.core

import okhttp3.Cookie
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.Closeable
import java.io.File
import java.io.IOException
import java.util.concurrent.CancellationException
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

class HttpApi(val origin: String, private val secrets: SecretStore) : Closeable {
    private val base = origin.toHttpUrl()
    private val serial = AtomicLong()
    private val requestScope = ThreadLocal<Long>()
    @Volatile private var closed = false
    private val cookieLock = Any()
    private var cookie: Cookie? = null
    @Volatile var session: Session? = null
    private val client = OkHttpClient.Builder().connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS).callTimeout(30, TimeUnit.SECONDS)
        .followRedirects(false).followSslRedirects(false).retryOnConnectionFailure(false).build()
    // A server may close an idle pooled socket while the app is in the background.
    // Only idempotent reads may recover that connection within the existing call timeout.
    private val readClient = client.newBuilder().retryOnConnectionFailure(true).build()
    private val mediaClient = readClient.newBuilder().build()

    init {
        require(base.encodedPath == "/" && base.query == null && base.fragment == null && base.username.isEmpty() && base.password.isEmpty())
        require(base.isHttps || base.host in setOf("localhost", "127.0.0.1", "::1"))
        try {
            val saved = secrets.read("cookie")?.let(::JSONObject)
            if (saved?.optString("origin") == origin) {
                cookie = Cookie.parse(base, saved.getString("cookie"))?.takeIf(::allowedCookie)
            }
        } catch (_: Exception) { cookie = null }
    }
    fun epoch() = serial.get()
    fun check(expected: Long) { if (closed || expected != epoch()) throw CancellationException("请求所属会话已结束。") }
    fun <T> withScope(expected: Long, block: () -> T): T {
        check(expected)
        val previous = requestScope.get()
        requestScope.set(expected)
        try { return block().also { check(expected) } }
        finally { if (previous == null) requestScope.remove() else requestScope.set(previous) }
    }
    private fun requestEpoch() = (requestScope.get() ?: epoch()).also(::check)
    fun cancelRequests() { serial.incrementAndGet(); client.dispatcher.cancelAll() }
    fun clearSession() {
        cancelRequests()
        synchronized(cookieLock) { cookie = null; session = null; secrets.clear("cookie") }
    }
    private fun allowedCookie(value: Cookie) = value.name == (if (base.isHttps) "__Host-memoverse_session" else "memoverse_session")
        && value.hostOnly && value.domain == base.host && value.path == "/" && value.secure == base.isHttps
        && value.httpOnly && value.expiresAt > System.currentTimeMillis()

    private fun builder(path: String, expected: Long): Request.Builder {
        check(expected)
        require(path.startsWith("/api/") && !path.contains('\\') && !path.contains('#') && !path.contains("://"))
        val url = base.resolve(path) ?: throw ApiFailure(message = "请求地址无效。")
        require(url.host == base.host && url.port == base.port && url.scheme == base.scheme)
        return Request.Builder().url(url).header("Origin", origin).header("X-MemoVerse-Client", "android").also {
            synchronized(cookieLock) {
                check(expected)
                cookie?.takeIf { item -> item.matches(url) && item.expiresAt > System.currentTimeMillis() }
                    ?.let { item -> it.header("Cookie", "${item.name}=${item.value}") }
            }
        }
    }
    private fun saveCookies(response: Response, expected: Long) {
        synchronized(cookieLock) {
            check(expected)
            for (raw in response.headers.values("Set-Cookie")) {
                val parsed = Cookie.parse(base, raw) ?: continue
                if (parsed.name != (if (base.isHttps) "__Host-memoverse_session" else "memoverse_session")) continue
                if (parsed.expiresAt <= System.currentTimeMillis()) {
                    cookie = null; secrets.clear("cookie")
                } else if (allowedCookie(parsed)) {
                    secrets.write("cookie", JSONObject().put("origin", origin).put("cookie", parsed.toString()).toString())
                    cookie = parsed
                }
            }
        }
    }
    private fun bounded(response: Response, limit: Long, expected: Long, clientMedia: Boolean = false): ByteArray {
        val body = response.body ?: throw ApiFailure(message = "服务响应正文缺失。")
        if (body.contentLength() > limit) throw ApiFailure(message = "服务响应超过大小限制。")
        return body.byteStream().use { stream ->
            val output = ByteArrayOutputStream()
            val block = ByteArray(16 * 1024)
            while (true) {
                check(expected)
                val count = stream.read(block)
                if (count == -1) break
                if (output.size().toLong() + count > limit) throw ApiFailure(message = if (clientMedia) "图片大小与记录不一致。" else "服务响应超过大小限制。")
                output.write(block, 0, count)
            }
            check(expected); output.toByteArray()
        }
    }
    private fun failure(response: Response, expected: Long): ApiFailure {
        var message = "请求未成功（${response.code}），请重试。"
        var code = ""
        try {
            val body = JSONObject(bounded(response, 65536, expected).toString(Charsets.UTF_8))
            when (val detail = body.opt("detail")) {
                is String -> message = detail.take(500)
                is JSONObject -> { message = detail.optString("message", message).take(500); code = detail.optString("code").take(100) }
            }
            if (body.opt("code") is String) code = body.getString("code").take(100)
        } catch (_: IOException) { } catch (_: org.json.JSONException) { } catch (_: ApiFailure) { }
        return ApiFailure(response.code, code, message)
    }
    fun json(path: String, method: String = "GET", body: JSONObject? = null, authenticated: Boolean = true): JSONObject {
        val requestBody = body?.toString()?.toRequestBody("application/json; charset=utf-8".toMediaType())
        return executeJson(path, method, requestBody, authenticated)
    }
    private fun executeJson(path: String, method: String, body: RequestBody?, authenticated: Boolean): JSONObject {
        val expected = requestEpoch()
        if (authenticated && session?.authenticated != true) throw ApiFailure(401, message = "请先登录。")
        val request = builder(path, expected).method(method, body).also {
            if (method != "GET") session?.csrfToken?.takeIf(String::isNotEmpty)?.let { token -> it.header("X-CSRF-Token", token) }
        }.build()
        try {
            val transport = when {
                method == "GET" -> readClient
                path == "/api/media" -> client.newBuilder().callTimeout(120, TimeUnit.SECONDS).build()
                else -> client
            }
            return transport.newCall(request).execute().use { response ->
                check(expected); saveCookies(response, expected)
                if (!response.isSuccessful) throw failure(response, expected)
                val raw = bounded(response, 1024 * 1024, expected)
                val decoder = Charsets.UTF_8.newDecoder().onMalformedInput(java.nio.charset.CodingErrorAction.REPORT)
                val result = JSONObject(decoder.decode(java.nio.ByteBuffer.wrap(raw)).toString())
                check(expected); result
            }
        } catch (error: IOException) {
            check(expected)
            throw ApiFailure(message = if (method == "GET") "暂时无法读取服务数据，请重试。"
                else "暂时无法连接服务，提交结果尚未确认，请核对后重试。")
        } catch (_: org.json.JSONException) { throw ApiFailure(message = "服务响应格式异常，请重试。") }
    }
    fun upload(file: File, mime: String, uploadId: String): MediaRef {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM).addFormDataPart("uploadId", uploadId)
            .addFormDataPart("file", "ticket", file.asRequestBody(mime.toMediaType())).build()
        val value = executeJson("/api/media", "POST", body, true)
        if (Wire.id(value, "uploadId") != uploadId) Wire.invalid()
        return Wire.media(value, true)
    }
    fun events(listener: WebSocketListener, expected: Long): WebSocket = client.newWebSocket(builder("/api/sync/events", expected).build(), listener)

    private fun media(path: String, signed: Boolean, ref: MediaRef, expected: Long): ByteArray {
        check(expected)
        val request = if (signed) Request.Builder().url(path).get().build() else builder(path, expected).get().build()
        val transport = if (signed) mediaClient else readClient
        try {
            return transport.newCall(request).execute().use { response ->
                check(expected)
                if (!response.isSuccessful) throw if (signed) ApiFailure(response.code, message = "云端图片读取失败。") else failure(response, expected)
                if (response.header("Content-Type")?.substringBefore(';')?.trim()?.lowercase() != ref.mimeType) {
                    throw ApiFailure(message = "图片格式与记录不一致。")
                }
                val bytes = bounded(response, ref.size, expected, true)
                if (bytes.size.toLong() != ref.size) throw ApiFailure(message = "图片下载不完整。")
                bytes
            }
        } catch (_: IOException) { check(expected); throw ApiFailure(message = "图片下载失败，请重新检查。") }
    }
    fun download(ref: MediaRef): Pair<ByteArray, String> {
        val expected = requestEpoch()
        for (attempt in 0..1) {
            val auth = json("${ref.url}/download")
            check(expected)
            if (Wire.text(auth, "sha256") != ref.sha256 || Wire.number(auth, "size") != ref.size || Wire.text(auth, "mimeType") != ref.mimeType) Wire.invalid()
            val transport = Wire.text(auth, "transport")
            val url = Wire.text(auth, "url", 16000)
            if (transport == "server") {
                if (url != ref.url) Wire.invalid()
            } else if (transport != "s3" || !validS3Url(url)) Wire.invalid()
            if (Wire.number(auth, "expiresAt") <= System.currentTimeMillis()) continue
            try { return media(url, transport == "s3", ref, expected) to transport }
            catch (failure: ApiFailure) {
                if (transport == "server") throw failure
                if (attempt == 0 && failure.status == 403) continue
                break
            }
        }
        check(expected)
        return media(ref.url, false, ref, expected) to "server"
    }
    override fun close() {
        closed = true
        cancelRequests(); client.connectionPool.evictAll(); mediaClient.connectionPool.evictAll()
        client.dispatcher.executorService.shutdown()
    }
    companion object {
        fun validS3Url(value: String): Boolean = try {
            val url = value.toHttpUrl()
            url.isHttps && url.username.isEmpty() && url.password.isEmpty() && url.port == 443 && url.fragment == null
                && Regex("(?:[a-z0-9][a-z0-9.-]*\\.)?s3(?:[.-][a-z0-9-]+)?\\.amazonaws\\.com(?:\\.cn)?").matches(url.host)
                && !Regex("[\\u0000-\\u0020\\\\]").containsMatchIn(value)
        } catch (_: IllegalArgumentException) { false }
    }
}
