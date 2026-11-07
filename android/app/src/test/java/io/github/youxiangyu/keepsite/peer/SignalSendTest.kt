package io.github.youxiangyu.keepsite.peer

import io.github.youxiangyu.keepsite.core.*
import org.json.JSONException
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class SignalSendTest {
    private data class Environment(val diagnostics: NetworkDiagnostics, val account: NetworkMonitor.Account, val peer: PeerObservation,
                                   val state: EventState, val socket: SocketObservation)
    private fun ready(detailed: Boolean = true, version: Int? = 1, aligned: Boolean = true, monitor: NetworkMonitor = NetworkMonitor()): Environment {
        val diagnostics = NetworkDiagnostics(monitor); val account = diagnostics.openAccount(3)!!
        diagnostics.setDetailed(detailed)
        val socket = diagnostics.socket(account, 3, 4)!!
        socket.opened(); socket.received("sync.ready", 50, SocketCapabilities(15000, version), "space", 0); socket.aligned(aligned)
        return Environment(diagnostics, account, diagnostics.peer(account, 3)!!, EventState(aligned, "space", 4, 15000, version), socket)
    }
    private fun message(kind: String = "peer.find") = JSONObject().put("v", 1).put("type", kind).put("requestId", "business-request")
        .also { if (kind == "peer.find") it.put("assetId", "a".repeat(64)) }
    private fun newCommand(env: Environment, kind: String = "peer.find") = env.peer.command(kind, 4)!!

    @Test fun supportedCurrentOwnerAddsOnlyCollectorIdsAndCountsActualFinalUtf8() {
        val env = ready(); val command = newCommand(env); var sent = ""
        assertTrue(sendPeerMessage(message(), env.state, env.diagnostics, command, { 0 }) { sent = it; true })
        command.complete(); command.close()
        val wire = JSONObject(sent); val trace = wire.getJSONObject("diagnostics")
        assertEquals(setOf("captureId", "traceId"), trace.keys().asSequence().toSet())
        assertTrue(trace.getString("traceId").matches(Regex("[0-9a-f]{32}")))
        assertEquals("business-request", wire.getString("requestId"))
        val row = env.diagnostics.snapshot()!!.observations.events.last { it.operationId == command.operation.id }
        assertEquals(sent.toByteArray(Charsets.UTF_8).size.toLong(), row.values.controlBytes)
        assertFalse(row.toString().contains("business-request")); assertFalse(row.toString().contains("a".repeat(64)))
    }

    @Test fun absentCapabilityOffPauseUnalignedAndOldGenerationSendUnmodifiedBase() {
        for ((detailed, version, aligned) in listOf(Triple(false, 1, true), Triple(true, null, true), Triple(true, 2, true), Triple(true, 1, false))) {
            val env = ready(detailed, version, aligned); val command = newCommand(env); var sent = ""
            assertTrue(sendPeerMessage(message(), env.state, env.diagnostics, command, { 0 }) { sent = it; true })
            assertFalse(JSONObject(sent).has("diagnostics")); command.close()
        }
        val env = ready(); val paused = newCommand(env); env.diagnostics.pauseHistory(true)
        var sent = ""
        assertTrue(sendPeerMessage(message(), env.state, env.diagnostics, paused, { 0 }) { sent = it; true }); assertFalse(JSONObject(sent).has("diagnostics")); paused.close()
        env.diagnostics.pauseHistory(false)
        val staleGeneration = newCommand(env)
        assertTrue(sendPeerMessage(message(), env.state.copy(generation = 5), env.diagnostics, staleGeneration, { 0 }) { sent = it; true })
        assertFalse(JSONObject(sent).has("diagnostics")); staleGeneration.close()
    }

    @Test fun clearLogoutReplacementAndWrongOwnerCannotReuseAnOldTrace() {
        val env = ready(); val beforeClear = newCommand(env); env.diagnostics.clearHistory(); var sent = ""
        assertTrue(sendPeerMessage(message(), env.state, env.diagnostics, beforeClear, { 0 }) { sent = it; true })
        assertFalse(JSONObject(sent).has("diagnostics")); beforeClear.close()
        assertTrue(env.diagnostics.snapshot()!!.observations.events.isEmpty())
        val old = newCommand(env); env.diagnostics.openAccount(3)
        assertTrue(sendPeerMessage(message(), env.state, env.diagnostics, old, { 0 }) { sent = it; true })
        assertFalse(JSONObject(sent).has("diagnostics")); old.close()
        assertTrue(env.diagnostics.snapshot()!!.observations.events.isEmpty())
        val other = ready(); val own = newCommand(other)
        assertTrue(sendPeerMessage(message(), other.state, env.diagnostics, own, { 0 }) { sent = it; true })
        assertFalse(JSONObject(sent).has("diagnostics")); own.close()
    }

    @Test fun realParentIsUsedInsteadOfTheMostRecentAccountOperation() {
        val env = ready(); val media = env.diagnostics.media(env.account, 3, 10)!!
        val attempt = media.attempt(MonitorRoute.P2P_UNKNOWN, MonitorProtocol.P2P)!!
        val unrelated = env.diagnostics.media(env.account, 3, 20)!!
        val command = env.peer.command("peer.open", 4, attempt.operation)!!; var sent = ""
        assertTrue(sendPeerMessage(message("peer.open"), env.state, env.diagnostics, command, { 0 }) { sent = it; true })
        assertEquals(attempt.operation.traceId, JSONObject(sent).getJSONObject("diagnostics").getString("traceId"))
        assertNotEquals(unrelated.operation.traceId, attempt.operation.traceId)
        command.close(); attempt.close(); media.close(); unrelated.close()
    }

    @Test fun metadataOnlyIsOmittedAtBothFrameAndQueueBoundariesWithoutRelaxingBase() {
        val env = ready()
        for (kind in listOf("peer.find", "peer.signal")) {
            val base = message(kind).also { if (kind == "peer.signal") it.put("kind", "offer") }
            val maximum = if (kind == "peer.signal") 65536 else 4096
            base.put("padding", "") // Budget fixture; does not claim this synthetic payload passes server business validation.
            base.put("padding", "x".repeat(maximum - base.toString().toByteArray().size))
            assertEquals(maximum, base.toString().toByteArray().size)
            val command = newCommand(env, kind); var sent = ""
            assertTrue(sendPeerMessage(base, env.state, env.diagnostics, command, { 0 }) { sent = it; true })
            assertEquals(base.toString(), sent); assertFalse(JSONObject(sent).has("diagnostics")); command.close()
            base.put("padding", base.getString("padding") + "x")
            var calls = 0; val oversized = newCommand(env, kind)
            assertFalse(sendPeerMessage(base, env.state, env.diagnostics, oversized, { 0 }) { calls++; true }); assertEquals(0, calls); oversized.close()
        }
        val base = message(); val size = base.toString().toByteArray().size
        val atQueue = newCommand(env); var sent = ""
        assertTrue(sendPeerMessage(base, env.state, env.diagnostics, atQueue, { 256 * 1024L - size }) { sent = it; true })
        assertFalse(JSONObject(sent).has("diagnostics")); atQueue.close()
        var calls = 0; val overflow = newCommand(env)
        assertFalse(sendPeerMessage(base, env.state, env.diagnostics, overflow, { 256 * 1024L - size + 1 }) { calls++; true })
        assertEquals(0, calls); overflow.close()
    }

    @Test fun unicodeUsesBytesAndBusinessFalseOrThrowableIsNeverChanged() {
        val env = ready(); val base = message().put("padding", "中".repeat(1300))
        val command = newCommand(env); var sent = ""
        assertTrue(sendPeerMessage(base, env.state, env.diagnostics, command, { 0 }) { sent = it; true })
        assertTrue(sent.toByteArray(Charsets.UTF_8).size <= 4096); command.close()
        val failed = newCommand(env)
        assertFalse(sendPeerMessage(message(), env.state, env.diagnostics, failed, { 0 }) { false }); failed.close()
        val original = IllegalStateException("PRIVATE business send")
        val throwing = newCommand(env)
        assertSame(original, assertThrows(IllegalStateException::class.java) {
            sendPeerMessage(message(), env.state, env.diagnostics, throwing, { 0 }) { throw original }
        }); throwing.close()
        assertFalse(env.diagnostics.snapshot()!!.observations.events.toString().contains("PRIVATE"))
    }

    @Test fun observerJsonFailureAfterActualSendCannotChangeItsBooleanOrLeakSlots() {
        var broken = false
        val monitor = NetworkMonitor(eventSize = { if (broken) throw JSONException("PRIVATE observer") else 400 })
        val env = ready(monitor = monitor); val command = newCommand(env)
        assertTrue(sendPeerMessage(message(), env.state, env.diagnostics, command, { 0 }) { broken = true; true })
        command.complete(); command.close()
        broken = false; env.socket.closed(); env.peer.close()
        assertEquals(0, monitor.snapshot().active)
    }
}
