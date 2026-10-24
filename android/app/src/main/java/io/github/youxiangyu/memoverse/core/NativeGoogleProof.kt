package io.github.youxiangyu.memoverse.core

import org.json.JSONObject
import java.net.URI
import java.net.URLDecoder

/** Stored with the verifier in SecretStore; never derived from a callback parameter. */
data class NativeGoogleProof(val verifier: String, val state: String, val expiresAt: Long, val purpose: String, val accountId: String?) {
    val link get() = purpose == "link"
    val redeemPath get() = if (link) "/api/auth/account/google/native/redeem" else "/api/auth/native/google/redeem"
    fun toJson(): JSONObject = JSONObject().put("verifier", verifier).put("state", state).put("expiresAt", expiresAt)
        .put("purpose", purpose).put("accountId", accountId ?: JSONObject.NULL)

    fun callback(value: String, currentAccount: String?, now: Long): Map<String, String> {
        if (expiresAt <= now || link && (accountId == null || accountId != currentAccount) || !link && currentAccount != null) {
            throw ApiFailure(message = "授权所属账号已改变或流程已过期，请重新发起。")
        }
        try {
            val uri = URI(value)
            if (uri.scheme != "io.github.youxiangyu.memoverse" || uri.rawAuthority != null || uri.rawPath != "/oauth/callback" || uri.rawFragment != null) Wire.invalid()
            val pairs = (uri.rawQuery ?: "").split('&').map { part ->
                val split = part.split('=', limit = 2)
                if (split.size != 2) Wire.invalid()
                URLDecoder.decode(split[0], "UTF-8") to URLDecoder.decode(split[1], "UTF-8")
            }
            val query = pairs.toMap()
            if (pairs.size != query.size || query.keys !in setOf(setOf("ticket", "state"), setOf("error", "state"))
                || query["state"] != state || query.values.any(String::isEmpty)) Wire.invalid()
            return query
        } catch (_: java.net.URISyntaxException) { Wire.invalid() }
        catch (_: IllegalArgumentException) { Wire.invalid() }
    }
    fun redeemBody(query: Map<String, String>): JSONObject = JSONObject().put("ticket", query["ticket"] ?: Wire.invalid())
        .put("verifier", verifier).put("state", state)

    companion object {
        fun fromJson(value: JSONObject): NativeGoogleProof {
            val purpose = Wire.text(value, "purpose", 10)
            val accountId = Wire.nullable(value, "accountId", 200)
            val verifier = Wire.text(value, "verifier", 43)
            val state = Wire.text(value, "state", 43)
            if (purpose !in setOf("link", "login") || purpose == "link" && accountId.isNullOrBlank()
                || purpose == "login" && accountId != null || !Regex("[A-Za-z0-9_-]{43}").matches(verifier)
                || !Regex("[A-Za-z0-9_-]{43}").matches(state)) Wire.invalid()
            return NativeGoogleProof(verifier, state, Wire.number(value, "expiresAt"), purpose, accountId)
        }
    }
}
