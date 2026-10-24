package io.github.youxiangyu.keepsite.peer

import io.github.youxiangyu.keepsite.core.MediaRef
import org.json.JSONArray
import org.json.JSONObject

internal object PeerJson {
    fun text(value: JSONObject, key: String, maximum: Int = 120): String {
        val result = value.opt(key)
        require(result is String && result.length <= maximum) { "Invalid peer field $key" }
        return result
    }
    fun number(value: JSONObject, key: String, minimum: Long = 0, maximum: Long = 9_007_199_254_740_991): Long {
        val raw = value.opt(key)
        require(raw is Int || raw is Long) { "Invalid integer peer field $key" }
        val result = (raw as Number).toLong()
        require(result in minimum..maximum) { "Out-of-range peer field $key" }
        return result
    }
    fun flag(value: JSONObject, key: String): Boolean {
        val raw = value.opt(key); require(raw is Boolean); return raw
    }
    fun token(value: JSONObject, key: String): String = text(value, key, 64).also { require(PeerWire.isToken(it)) }
    fun array(value: JSONObject, key: String, maximum: Int): JSONArray = value.optJSONArray(key)
        ?.also { require(it.length() <= maximum) } ?: throw IllegalArgumentException("Missing peer array $key")
    fun media(value: JSONObject): MediaRef {
        val id = text(value, "assetId", 64)
        val sha = text(value, "sha256", 64)
        val size = number(value, "size", 1, PeerWire.MAX_IMAGE_BYTES.toLong())
        val mime = text(value, "mimeType", 30)
        require(PeerWire.isHash(id) && id == sha && PeerWire.mime(mime))
        return MediaRef(id, sha, size, mime, 0, 0)
    }
    fun same(a: MediaRef, b: MediaRef) = a.assetId == b.assetId && a.sha256 == b.sha256 && a.size == b.size && a.mimeType == b.mimeType
    fun valid(media: MediaRef) = PeerWire.isHash(media.assetId) && media.assetId == media.sha256
        && media.size in 1L..PeerWire.MAX_IMAGE_BYTES.toLong() && PeerWire.mime(media.mimeType)
    fun control(bytes: ByteArray): JSONObject {
        require(bytes.size in 1..PeerWire.MAX_CONTROL_BYTES)
        val decoded = Charsets.UTF_8.newDecoder().decode(java.nio.ByteBuffer.wrap(bytes)).toString()
        return JSONObject(decoded).also { require(number(it, "v", 1, 1) == 1L); text(it, "type", 32) }
    }
    fun keys(value: JSONObject, expected: Set<String>) { require(value.keys().asSequence().toSet() == expected) }
    fun ice(payload: Any?): RtcIce? {
        if (payload == null || payload === JSONObject.NULL) return null
        require(payload is JSONObject && payload.toString().toByteArray(Charsets.UTF_8).size <= 2048)
        val candidate = text(payload, "candidate", 2048)
        val mid = if (payload.isNull("sdpMid")) null else text(payload, "sdpMid", 256)
        val line = if (payload.isNull("sdpMLineIndex")) null else number(payload, "sdpMLineIndex", 0, 65535).toInt()
        val username = if (!payload.has("usernameFragment") || payload.isNull("usernameFragment")) null else text(payload, "usernameFragment", 256)
        return RtcIce(candidate, mid, line, username)
    }
    fun ice(ice: RtcIce?): Any = ice?.let {
        JSONObject().put("candidate", it.candidate).put("sdpMid", it.mid ?: JSONObject.NULL)
            .put("sdpMLineIndex", it.line ?: JSONObject.NULL)
            .apply { if (it.username != null) put("usernameFragment", it.username) }
    } ?: JSONObject.NULL
}
