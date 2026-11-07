package io.github.youxiangyu.keepsite.updates

import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class GithubUpdateSourceTest {
    private fun asset(id: Int, name: String, bytes: ByteArray) = JSONObject().put("id", id).put("name", name)
        .put("state", "uploaded").put("size", bytes.size).put("digest", "sha256:${UpdatePolicy.digest(bytes)}")
        .put("browser_download_url", "https://github.com/${UpdatePolicy.REPOSITORY}/releases/download/v0.1/$name")

    @Test fun publicMetadataFollowsBoundedTrustedRedirectAndVerifiesExistingRecordWithoutAccountHeaders() {
        val apk = "synthetic APK".toByteArray()
        val record = JSONObject().put("applicationId", UpdatePolicy.PACKAGE).put("versionCode", 4).put("versionName", "0.1.0")
            .put("sha256", UpdatePolicy.digest(apk)).put("bytes", apk.size).put("certificateSha256", "a".repeat(64)).toString()
        val release = JSONObject().put("draft", false).put("prerelease", false).put("tag_name", "v0.1")
            .put("assets", JSONArray().put(asset(1, "keepsite.apk", apk)).put(asset(2, "apk-verification.json", record.toByteArray()))).toString()
        val requests = mutableListOf<String>()
        val client = OkHttpClient.Builder().followRedirects(false).addInterceptor { chain ->
            val request = chain.request(); requests += request.url.toString()
            assertNull(request.header("Cookie")); assertNull(request.header("Authorization")); assertNull(request.header("X-CSRF-Token"))
            val response = Response.Builder().request(request).protocol(Protocol.HTTP_1_1).message("test")
            when (request.url.host) {
                "api.github.com" -> response.code(200).body(release.toResponseBody("application/json".toMediaType())).build()
                "github.com" -> response.code(302).header("Location", "https://release-assets.githubusercontent.com/owned-record")
                    .body("".toResponseBody()).build()
                else -> response.code(200).body(record.toResponseBody("application/json".toMediaType())).build()
            }
        }.build()
        GithubUpdateSource(client).use { source ->
            val snapshot = requireNotNull(source.latest()); assertEquals(4L, snapshot.checked().second!!.code)
        }
        assertEquals(3, requests.size); assertTrue(requests.none { it.endsWith(".apk") })
    }

    @Test fun oversizedMetadataAndForeignRedirectFailBeforeReturningACandidate() {
        val huge = OkHttpClient.Builder().addInterceptor { chain ->
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).message("test").code(200)
                .body("x".repeat(256 * 1024 + 1).toResponseBody()).build()
        }.build()
        GithubUpdateSource(huge).use { source -> assertThrows(IllegalArgumentException::class.java) { source.latest() } }
        var requests = 0
        val redirect = OkHttpClient.Builder().followRedirects(false).addInterceptor { chain ->
            requests++
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).message("test").code(302)
                .header("Location", "https://untrusted.example/record").body("".toResponseBody()).build()
        }.build()
        GithubUpdateSource(redirect).use { source -> assertThrows(IllegalArgumentException::class.java) { source.latest() } }
        assertEquals(1, requests)
    }
}
