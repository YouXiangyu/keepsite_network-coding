package io.github.youxiangyu.keepsite.updates

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import androidx.activity.result.ActivityResultRegistry
import androidx.activity.result.contract.ActivityResultContract
import androidx.core.app.ActivityOptionsCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

/** Real AndroidX Registry, fake launch hook: no Activity, installer, account or network is opened. */
@RunWith(AndroidJUnit4::class)
class UpdateResultRequestsInstrumentedTest {
    private class Registry : ActivityResultRegistry() {
        val launches = mutableListOf<Pair<Int, String>>()
        override fun <I, O> onLaunch(requestCode: Int, contract: ActivityResultContract<I, O>, input: I, options: ActivityOptionsCompat?) {
            launches += requestCode to requireNotNull((input as Intent).action)
        }
    }
    private fun main(block: () -> Unit) {
        var failure: Throwable? = null
        InstrumentationRegistry.getInstrumentation().runOnMainSync { try { block() } catch (error: Throwable) { failure = error } }
        failure?.let { throw it }
    }
    private fun snapshot(id: Int): UpdateSnapshot {
        val bytes = "synthetic package".toByteArray()
        val asset = JSONObject().put("id", id).put("name", "keepsite.apk").put("state", "uploaded").put("size", bytes.size)
            .put("digest", "sha256:${UpdatePolicy.digest(bytes)}")
            .put("browser_download_url", "https://github.com/${UpdatePolicy.REPOSITORY}/releases/download/v0.1/keepsite.apk")
        return UpdateSnapshot(JSONObject().put("draft", false).put("prerelease", false).put("tag_name", "v0.1")
            .put("assets", JSONArray().put(asset)).toString(), null)
    }

    @Test fun lateAResultKeepsOriginalTokenAndCannotCompleteBOrReleaseEitherReaderFile() = main {
        val owner = object : LifecycleOwner {
            private val events = LifecycleRegistry(this)
            override val lifecycle: Lifecycle get() = events
            fun resume() { events.currentState = Lifecycle.State.RESUMED }
        }
        owner.resume()
        val registry = Registry(); val pending = mutableMapOf<String, HandoffKind>(); val returned = mutableListOf<String>()
        var state = HandoffState()
        val handoffs = UpdateHandoffs(object : HandoffStore { override fun read() = state; override fun write(value: HandoffState) { state = value } })
        val target = UpdatePolicy.PackageIdentity(UpdatePolicy.PACKAGE, 4, setOf("a".repeat(64)))
        val first = requireNotNull(handoffs.prepare(snapshot(1), target, HandoffKind.INSTALL, true)); handoffs.issue(first.token)
        var launching: String? = first.token
        val requests = UpdateResultRequests(registry, pending) { token, _ ->
            if (launching == token) launching = null
            returned += token; handoffs.installerReturned(token)
        }
        requests.launch(first.token, HandoffKind.INSTALL, Intent("test.A"))
        val second = requireNotNull(handoffs.prepare(snapshot(2), target, HandoffKind.INSTALL, true)); handoffs.issue(second.token)
        launching = second.token
        requests.launch(second.token, HandoffKind.INSTALL, Intent("test.B"))
        assertEquals(Lifecycle.State.RESUMED, owner.lifecycle.currentState)
        registry.dispatchResult(registry.launches[0].first, Activity.RESULT_CANCELED, Intent())
        assertEquals(listOf(first.token), returned); assertEquals(second.token, launching)
        assertEquals(second.token, state.active!!.token); assertEquals(HandoffKind.INSTALL, state.active!!.kind)
        assertFalse(pending.containsKey(first.token)); assertTrue(pending.containsKey(second.token))
        assertEquals(2, state.files.count { it.possiblyRead })
        registry.dispatchResult(registry.launches[1].first, Activity.RESULT_CANCELED, Intent())
        assertEquals(listOf(first.token, second.token), returned); assertNull(launching)
        assertEquals(HandoffKind.RETURNED, state.active!!.kind); assertEquals(2, state.files.size)
        requests.close()
    }

    @Test fun restoredSynchronousPendingResultIsConsumedOnceWithoutRevivingOrLaunchingItsKey() = main {
        val token = "00000000-0000-0000-0000-000000000001"
        val registry = Registry(); val pending = mutableMapOf<String, HandoffKind>()
        val original = UpdateResultRequests(registry, pending) { _, _ -> fail("Detached callback must not run") }
        original.launch(token, HandoffKind.PERMISSION, Intent("test.original"))
        val code = registry.launches.single().first
        original.close(); registry.dispatchResult(code, Activity.RESULT_OK, Intent())
        val saved = Bundle(); registry.onSaveInstanceState(saved)
        val restored = Registry().apply { onRestoreInstanceState(saved) }; val restoredPending = pending.toMutableMap()
        val results = mutableListOf<String>()
        val requests = UpdateResultRequests(restored, restoredPending) { value, _ -> results += value }
        requests.restore(); requests.restore()
        assertEquals(listOf(token), results); assertTrue(restoredPending.isEmpty()); assertTrue(restored.launches.isEmpty())
        restored.dispatchResult(code, Activity.RESULT_OK, Intent()); assertEquals(1, results.size)
        requests.close()
        val another = Registry().apply { onRestoreInstanceState(saved) }; val anotherPending = pending.toMutableMap()
        val recovered = mutableListOf<String>()
        val recovery = UpdateResultRequests(another, anotherPending) { value, _ -> recovered += value }
        recovery.launch(token, HandoffKind.PERMISSION, Intent("test.must-not-launch"))
        assertEquals(listOf(token), recovered); assertTrue(anotherPending.isEmpty()); assertTrue(another.launches.isEmpty())
        recovery.close()
    }
}
