package io.github.youxiangyu.keepsite.core

import org.json.JSONObject

/** Existing authorization/403 retry/proxy fallback algorithm, with I/O boundaries supplied by HttpApi. */
internal fun downloadMedia(ref: MediaRef, observation: MediaObservation?, authorize: () -> JSONObject,
    read: (String, Boolean, MediaAttempt?) -> ByteArray, guard: () -> Unit, wall: () -> Long = System::currentTimeMillis): Pair<ByteArray, String> {
    fun attempt(path: String, signed: Boolean): ByteArray {
        val span = observation?.attempt(if (signed) MonitorRoute.DIRECT_S3 else MonitorRoute.SERVER_PROXY, if (signed) MonitorProtocol.S3 else MonitorProtocol.HTTP)
        try { return read(path, signed, span).also { span?.complete() } }
        catch (failure: Throwable) { span?.failed(mediaFailure(failure)); throw failure }
        finally { span?.close() }
    }
    for (index in 0..1) {
        val auth = authorize()
        guard()
        if (Wire.text(auth, "sha256") != ref.sha256 || Wire.number(auth, "size") != ref.size || Wire.text(auth, "mimeType") != ref.mimeType) Wire.invalid()
        val transport = Wire.text(auth, "transport")
        val url = Wire.text(auth, "url", 16000)
        if (transport == "server") {
            if (url != ref.url) Wire.invalid()
        } else if (transport != "s3" || !HttpApi.validS3Url(url)) Wire.invalid()
        if (Wire.number(auth, "expiresAt") <= wall()) continue
        try { return attempt(url, transport == "s3") to transport }
        catch (failure: ApiFailure) {
            if (transport == "server") throw failure
            if (index == 0 && failure.status == 403) continue
            break
        }
    }
    guard()
    return attempt(ref.url, false) to "server"
}
