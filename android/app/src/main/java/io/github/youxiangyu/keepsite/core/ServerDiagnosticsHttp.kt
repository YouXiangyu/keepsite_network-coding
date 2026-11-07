package io.github.youxiangyu.keepsite.core

import okhttp3.HttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.util.concurrent.CancellationException

/** Explicit calls only; never enters the business HTTP observer, retries, or follows a redirect. */
internal object ServerDiagnosticsHttp {
    private val clearAck = Regex("""\A[ \t\r\n]*\{[ \t\r\n]*"cleared"[ \t\r\n]*:[ \t\r\n]*true[ \t\r\n]*\}[ \t\r\n]*\z""")
    fun execute(origin: HttpUrl, client: OkHttpClient, action: NetworkDiagnostics.ServerAction,
                authenticated: () -> Boolean, csrf: () -> String?, builder: (String, Long) -> Request.Builder,
                checkEpoch: (Long) -> Unit): Boolean {
        fun guard() {
            checkEpoch(action.epoch)
            if (!action.current()) throw CancellationException()
            if (!authenticated()) throw ApiFailure(401, message = "请先登录。")
        }
        try {
            guard()
            val path = "/api/diagnostics/events" + if (action.clearing) "" else "?after=0"
            val request = builder(path, action.epoch).removeHeader("X-Keepsite-Trace-Id")
                .header("X-Keepsite-Diagnostics", action.captureId).method(if (action.clearing) "DELETE" else "GET", null).also {
                    if (action.clearing) it.header("X-CSRF-Token", csrf()?.takeIf(String::isNotEmpty) ?: throw ApiFailure(403, message = "认证校验不可用。"))
                }.build()
            require(NetworkDiagnostics.sameOrigin(request.url, origin) && request.url.encodedPath == "/api/diagnostics/events" &&
                request.url.encodedQuery == if (action.clearing) null else "after=0")
            guard()
            // The existing non-retrying client is used. No GET retry recovery, polling, or opt-in rotation.
            val call = client.newBuilder().followRedirects(false).followSslRedirects(false).retryOnConnectionFailure(false).build().newCall(request)
            if (!action.attach(call::cancel)) return false
            call.execute().use { response ->
                guard()
                if (response.code != 200) {
                    val error = when (response.code) { 401 -> MonitorError.AUTH_REQUIRED; 403 -> MonitorError.AUTH_DENIED; 404, 503 -> MonitorError.UNAVAILABLE; else -> MonitorError.PROTOCOL }
                    action.finished(error, response.code); return false
                }
                val body = response.body ?: throw IOException()
                val limit = if (action.clearing) 1024 else ServerTraceBatch.MAX_BODY
                require(body.contentLength() <= limit)
                val raw = body.byteStream().use { stream ->
                    val output = ByteArrayOutputStream(); val block = ByteArray(8192)
                    while (true) {
                        guard(); val count = stream.read(block); if (count == -1) break
                        require(output.size() + count <= limit); output.write(block, 0, count)
                    }
                    output.toByteArray()
                }
                guard()
                if (action.clearing) {
                    // Exact fixed acknowledgment; it has no event body and cannot install a new capture.
                    val text = Charsets.UTF_8.newDecoder().onMalformedInput(java.nio.charset.CodingErrorAction.REPORT)
                        .decode(java.nio.ByteBuffer.wrap(raw))
                    require(clearAck.matches(text)) // Fixed shallow grammar: structural whitespace only, no string normalization or trailing value.
                    return action.finished(MonitorError.NONE, 200)
                }
                val batch = ServerTraceBatch.parse(raw, action.captureId) ?: run { action.finished(MonitorError.PROTOCOL, 200); return false }
                guard()
                return action.received(batch).also { if (!it) action.finished(MonitorError.PROTOCOL, 200) }
            }
        } catch (_: CancellationException) { action.finished(MonitorError.CANCELLED); return false }
        catch (failure: ApiFailure) { action.finished(if (failure.status == 401) MonitorError.AUTH_REQUIRED else MonitorError.AUTH_DENIED); return false }
        catch (failure: IOException) { action.finished(diagnosticIoError(failure)); return false }
        catch (_: Exception) { action.finished(MonitorError.PROTOCOL); return false }
    }
}
