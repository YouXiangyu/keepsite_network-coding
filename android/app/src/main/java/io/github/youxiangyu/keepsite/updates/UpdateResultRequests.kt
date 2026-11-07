package io.github.youxiangyu.keepsite.updates

import android.content.Intent
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.ActivityResultRegistry
import androidx.activity.result.contract.ActivityResultContracts
import androidx.annotation.MainThread
import java.io.Closeable

/** Standard owner-free Registry overload supports registration from an already resumed Activity. */
@MainThread
class UpdateResultRequests(private val registry: ActivityResultRegistry, private val pending: MutableMap<String, HandoffKind>,
                           private val returned: (String, HandoffKind) -> Unit) : Closeable {
    private val launchers = mutableMapOf<String, ActivityResultLauncher<Intent>>()
    private val completed = mutableSetOf<String>()

    fun restore() { pending.toMap().forEach { (token, kind) -> if (token in completed) pending.remove(token) else register(token, kind) } }
    fun launch(token: String, kind: HandoffKind, intent: Intent) {
        require(token !in completed)
        pending[token] = kind
        val launcher = register(token, kind)
        if (token !in completed && pending.containsKey(token)) launcher.launch(intent)
    }
    private fun register(token: String, kind: HandoffKind): ActivityResultLauncher<Intent> {
        launchers[token]?.let { return it }
        val launcher = registry.register("keepsite-update-$token", ActivityResultContracts.StartActivityForResult()) {
            completed += token; pending.remove(token); launchers.remove(token)?.unregister()
            returned(token, kind) // Immutable original token; never the latest ViewModel request.
        }
        // register() can synchronously deliver a restored pending result before launcher is assigned.
        if (token in completed || !pending.containsKey(token)) launcher.unregister() else launchers[token] = launcher
        return launcher
    }
    fun failed(token: String) { pending.remove(token); launchers.remove(token)?.unregister(); completed += token }
    override fun close() { launchers.values.toList().forEach { it.unregister() }; launchers.clear() }
}
