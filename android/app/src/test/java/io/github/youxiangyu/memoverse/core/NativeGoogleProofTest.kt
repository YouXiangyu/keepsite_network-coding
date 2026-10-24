package io.github.youxiangyu.memoverse.core

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class NativeGoogleProofTest {
    private val state = "s".repeat(43)
    private val callback = "io.github.youxiangyu.memoverse:/oauth/callback?ticket=once&state=$state"
    @Test fun restoredProofKeepsLoginAndAccountLinkEndpointsSeparate() {
        for (link in listOf(false, true)) {
            val saved = NativeGoogleProof("v".repeat(43), state, 1000, if (link) "link" else "login", if (link) "account-a" else null)
            val restored = NativeGoogleProof.fromJson(JSONObject(saved.toJson().toString()))
            assertEquals(saved, restored)
            val query = restored.callback(callback, if (link) "account-a" else null, 100)
            assertEquals("once", restored.redeemBody(query).getString("ticket"))
            assertEquals(saved.verifier, restored.redeemBody(query).getString("verifier"))
            assertEquals(link, restored.redeemPath.startsWith("/api/auth/account/"))
        }
    }
    @Test fun wrongAccountExpiredProofAndInvalidStateCannotRedeem() {
        val proof = NativeGoogleProof("v".repeat(43), state, 1000, "link", "account-a")
        for (account in listOf(null, "account-b")) assertThrows(ApiFailure::class.java) { proof.callback(callback, account, 100) }
        assertThrows(ApiFailure::class.java) { proof.callback(callback, "account-a", 1000) }
        for (uri in listOf(callback + "&state=$state", callback.replace(state, "other"), callback + "&purpose=login", callback.replace(":/oauth", "://evil/oauth"), callback + "#fragment")) {
            assertThrows(ApiFailure::class.java) { proof.callback(uri, "account-a", 100) }
        }
        val login = proof.copy(purpose = "login", accountId = null)
        assertThrows(ApiFailure::class.java) { login.callback(callback, "account-a", 100) }
    }
    @Test fun missingOrInconsistentStoredPurposeIsRejected() {
        val proof = NativeGoogleProof("v".repeat(43), state, 1000, "link", "account-a").toJson()
        proof.remove("purpose")
        assertThrows(ApiFailure::class.java) { NativeGoogleProof.fromJson(proof) }
        proof.put("purpose", "login")
        assertThrows(ApiFailure::class.java) { NativeGoogleProof.fromJson(proof) }
    }
}
