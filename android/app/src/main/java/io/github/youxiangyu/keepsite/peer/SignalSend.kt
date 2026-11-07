package io.github.youxiangyu.keepsite.peer

import io.github.youxiangyu.keepsite.core.NetworkDiagnostics
import io.github.youxiangyu.keepsite.core.PeerCommand
import io.github.youxiangyu.keepsite.core.MonitorError
import io.github.youxiangyu.keepsite.core.diagnostic
import org.json.JSONObject

/** Original frame/queue budgets apply to the final UTF-8 string. Diagnostics may only be omitted. */
internal fun sendPeerMessage(message: JSONObject, state: EventState, diagnostics: NetworkDiagnostics?, observation: PeerCommand?,
    queued: () -> Long, send: (String) -> Boolean): Boolean {
    val observer = observation?.takeIf { it.matchesSender(diagnostics, state) }
    val original = if (message.has("diagnostics")) JSONObject(message.toString()).also { it.remove("diagnostics") } else message
    val base = original.toString()
    val baseBytes = base.toByteArray(Charsets.UTF_8).size
    val maximum = if (original.optString("type") == "peer.signal" && original.optString("kind") != "ice") 65536 else 4096
    if (baseBytes > maximum) { observer?.failed(MonitorError.CAPACITY); return false }
    val buffered = queued()
    if (buffered !in 0..256 * 1024L - baseBytes) { observer?.failed(MonitorError.CAPACITY); return false }
    val context = observer?.context(diagnostics, state)
    val enriched = diagnostic<String?>(null) {
        context?.let { JSONObject(base).put("diagnostics", JSONObject().put("captureId", it.captureId).put("traceId", it.traceId)).toString() }
    }
    val final = enriched?.takeIf {
        val bytes = it.toByteArray(Charsets.UTF_8).size
        bytes <= maximum && buffered <= 256 * 1024L - bytes
    } ?: base
    // Keep business send outside the observer failure boundary; preserve its exact Boolean/Throwable.
    try {
        val accepted = send(final)
        observer?.sent(final.toByteArray(Charsets.UTF_8).size, accepted)
        return accepted
    } catch (failure: Throwable) { observer?.failed(MonitorError.UNKNOWN); throw failure }
}
