package io.github.youxiangyu.memoverse.core

import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import java.time.format.ResolverStyle

class ApiFailure(val status: Int = 0, val code: String = "", message: String) : Exception(message)

object Wire {
    private val identifier = Regex("[A-Za-z0-9_-]{1,120}")
    private val digest = Regex("[a-f0-9]{64}")
    private val statuses = setOf("not_started", "queued", "ocr", "poster", "ready", "no_match", "failed")
    fun invalid(): Nothing = throw ApiFailure(message = "服务返回的数据不完整，未更新票夹。")
    fun string(o: JSONObject, key: String, max: Int = 4096): String =
        (o.opt(key) as? String)?.takeIf { it.length <= max } ?: invalid()
    fun text(o: JSONObject, key: String, max: Int = 4096): String = string(o, key, max).takeIf { it.isNotEmpty() } ?: invalid()
    fun number(o: JSONObject, key: String, min: Long = 0, max: Long = 9_007_199_254_740_991): Long {
        val value = o.opt(key)
        if (value !is Int && value !is Long) invalid()
        return (value as Number).toLong().takeIf { it in min..max } ?: invalid()
    }
    fun bool(o: JSONObject, key: String): Boolean = o.opt(key) as? Boolean ?: invalid()
    fun obj(o: JSONObject, key: String): JSONObject = o.opt(key) as? JSONObject ?: invalid()
    fun array(o: JSONObject, key: String): JSONArray = o.opt(key) as? JSONArray ?: invalid()
    fun nullable(o: JSONObject, key: String, max: Int = 4096): String? {
        if (!o.has(key)) invalid()
        return if (o.isNull(key)) null else string(o, key, max)
    }
    fun id(o: JSONObject, key: String) = text(o, key, 120).takeIf(identifier::matches) ?: invalid()
    fun fields(o: JSONObject) = TicketFields(
        string(o, "title", 120), string(o, "cinema", 200), string(o, "hall", 200),
        string(o, "seat", 200), string(o, "showtime", 16),
    ).also(::validateFields)
    fun validateFields(fields: TicketFields) {
        fields.values().forEachIndexed { index, value -> if (value.length > if (index == 0) 120 else 200) invalid() }
        if (fields.showtime.isNotEmpty()) {
            try {
                val formatter = DateTimeFormatter.ofPattern("uuuu-MM-dd'T'HH:mm").withResolverStyle(ResolverStyle.STRICT)
                val parsed = LocalDateTime.parse(fields.showtime, formatter)
                if (parsed.year < 1 || parsed.format(formatter) != fields.showtime) invalid()
            } catch (_: java.time.DateTimeException) {
                throw ApiFailure(message = "放映时间应为 YYYY-MM-DDTHH:mm，例如 2026-10-22T19:30。")
            }
        }
    }
    fun fieldsJson(fields: TicketFields): JSONObject = JSONObject().also { out ->
        FIELD_NAMES.zip(fields.values()).forEach { (key, value) -> out.put(key, value) }
    }
    fun media(o: JSONObject, uploaded: Boolean = false): MediaRef {
        val sha = text(o, "sha256", 64)
        if (!digest.matches(sha) || text(o, "assetId", 64) != sha || text(o, "url") != "/api/media/$sha") invalid()
        val mime = text(o, "mimeType", 50)
        if (mime !in setOf("image/jpeg", "image/png", "image/webp")) invalid()
        val cloud = if (uploaded) "not_configured" else text(o, "cloudStatus", 30)
        if (cloud !in setOf("saved", "not_configured")) invalid()
        val width = number(o, "width", 1, 40_000_000).toInt()
        val height = number(o, "height", 1, 40_000_000).toInt()
        if (width.toLong() * height > 40_000_000) invalid()
        return MediaRef(sha, sha, number(o, "size", 1, MAX_IMAGE_BYTES), mime, width, height, cloud,
            if (o.has("source")) posterSource(obj(o, "source")) else null)
    }
    private fun posterSource(o: JSONObject): PosterSource {
        if (text(o, "provider") != "wikimedia_commons") invalid()
        val file = text(o, "sourceFile", 300)
        if (!file.startsWith("File:") || file.length <= 5 || Regex("[\\u0000-\\u001f\\\\/<>\\[\\]{}|]").containsMatchIn(file)) invalid()
        val encoded = URLEncoder.encode(file.replace(' ', '_'), "UTF-8").replace("+", "%20")
            .replace("%3A", ":").replace("%2C", ",").replace("%28", "(").replace("%29", ")")
            .replace("%21", "!").replace("%7E", "~").replace("*", "%2A")
        val page = text(o, "sourcePage")
        if (page != "https://commons.wikimedia.org/wiki/$encoded") invalid()
        val license = text(o, "licenseName", 100)
        val url = text(o, "licenseUrl")
        val valid = when (license) {
            "Public domain" -> url in setOf("https://creativecommons.org/publicdomain/mark/1.0/", "$page#Licensing")
            "CC0 1.0" -> url == "https://creativecommons.org/publicdomain/zero/1.0/"
            else -> Regex("CC (BY|BY-SA) (2\\.0|2\\.5|3\\.0|4\\.0)").matchEntire(license)?.let {
                url == "https://creativecommons.org/licenses/${it.groupValues[1].lowercase()}/${it.groupValues[2]}/"
            } ?: false
        }
        if (!valid) invalid()
        return PosterSource(page, file, license, url, string(o, "author", 1000), number(o, "fetchedAt"))
    }
    fun ticket(o: JSONObject): Ticket {
        if (!o.has("deletedAt") || !o.isNull("deletedAt")) invalid()
        val source = obj(o, "fieldSources")
        val sources = FIELD_NAMES.associateWith { key -> string(source, key, 20).also {
            if (it !in setOf("user", "ocr", "migration", "empty")) invalid()
        } }
        val mediaData = obj(o, "media")
        val original = media(obj(mediaData, "original"))
        if (!mediaData.has("poster")) invalid()
        val poster = if (mediaData.isNull("poster")) null else media(obj(mediaData, "poster")).also { if (it.source == null) invalid() }
        val processing = obj(o, "processing")
        val status = text(processing, "status", 30)
        if (status !in statuses || status == "ready" && (poster == null || poster.cloudStatus != "saved")) invalid()
        return Ticket(id(o, "id"), fields(obj(o, "fields")), sources, number(o, "createdAt"), number(o, "updatedAt"),
            number(o, "version", 1), number(o, "inputVersion", 1), original, poster, status,
            nullable(processing, "error"), id(o, "operationId"))
    }
    fun session(o: JSONObject): Session {
        val providers = obj(o, "providers")
        val signedIn = bool(o, "authenticated")
        val user = if (signedIn) obj(o, "user") else null
        return Session(user?.let { text(it, "id", 200) }, user?.let { string(it, "email", 254) } ?: "",
            user?.let { string(it, "name", 200) } ?: "", if (signedIn) text(o, "csrfToken", 500) else "",
            bool(o, "configured"), bool(providers, "email"), bool(providers, "google"))
    }
    fun capabilities(o: JSONObject): Capabilities {
        bool(o, "deviceSync")
        return Capabilities(bool(o, "processing"), bool(o, "cloudStorage"), nullable(o, "processingUnavailableReason"))
    }
    fun receipt(o: JSONObject, expected: String): Receipt {
        if (id(o, "operationId") != expected) invalid()
        return Receipt(text(o, "serverId", 200), expected, number(o, "committedCursor"))
    }
}
