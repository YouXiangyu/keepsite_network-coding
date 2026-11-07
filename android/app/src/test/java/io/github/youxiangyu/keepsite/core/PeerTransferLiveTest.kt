package io.github.youxiangyu.keepsite.core

import org.junit.Assert.*
import org.junit.Test

class PeerTransferLiveTest {
    @Test fun lightProjectionHasTwoActiveFiftyRecentAndNeverNeedsDetailedHistory() {
        val diagnostics = NetworkDiagnostics(); val account = diagnostics.openAccount(3)!!
        val peer = diagnostics.peer(account, 3)!!
        val first = peer.live("PRIVATE-peer-one", 10, false, null)!!
        val second = peer.live("PRIVATE-peer-two", 20, true, null)!!
        assertNull(peer.live("third", 30, false, null))
        first.publish(MonitorValues(bytes = 4, rttMs = 25.0, pcState = MonitorRtcState.CONNECTED), MonitorStatus.TRANSFERRING)
        diagnostics.clearHistory(); diagnostics.pauseHistory(true)
        first.publish(MonitorValues(bytes = 6, rttMs = 30.0, dtlsState = MonitorRtcState.CONNECTED), MonitorStatus.TRANSFERRING)
        var state = diagnostics.snapshot()!!
        assertEquals(2, state.peerTransfers.size); assertEquals(6L, state.peerTransfers.single { it.id == first.id }.values.bytes)
        assertTrue(state.observations.events.isEmpty()); assertFalse(state.peerTransfers.toString().contains("PRIVATE"))
        first.publish(MonitorValues(bytes = 10, verified = true), MonitorStatus.COMPLETED, terminal = true)
        second.publish(MonitorValues(bytes = 2), MonitorStatus.CANCELLED, terminal = true)
        for (i in 0..59) peer.live("peer-$i", 1, false, null)!!.publish(MonitorValues(bytes = 1, verified = true), MonitorStatus.COMPLETED, terminal = true)
        state = diagnostics.snapshot()!!
        assertEquals(50, state.peerTransfers.size); assertTrue(state.observations.events.isEmpty()); assertTrue(state.observations.aliases <= 32)
        diagnostics.clearHistory(); assertTrue(diagnostics.snapshot()!!.peerTransfers.isEmpty())
    }
    @Test fun oldConnectionEpochAndAccountCannotRepopulateTheNewProjection() {
        val diagnostics = NetworkDiagnostics(); val account = diagnostics.openAccount(3)!!
        val oldPeer = diagnostics.peer(account, 3)!!; val old = oldPeer.live("old", 10, false, null)!!
        val currentPeer = diagnostics.peer(account, 3)!!
        val current = currentPeer.live("current", 10, false, null)!!
        old.publish(MonitorValues(bytes = 9), MonitorStatus.TRANSFERRING)
        assertEquals(listOf(current.id), diagnostics.snapshot()!!.peerTransfers.map { it.id })
        diagnostics.resume(account, 4)
        current.publish(MonitorValues(bytes = 10), MonitorStatus.COMPLETED, terminal = true)
        assertTrue(diagnostics.snapshot()!!.peerTransfers.isEmpty())
        assertNull(currentPeer.live("old-epoch", 1, false, null))
        diagnostics.openAccount(4)
        old.publish(MonitorValues(bytes = 10), MonitorStatus.COMPLETED, terminal = true)
        assertTrue(diagnostics.snapshot()!!.peerTransfers.isEmpty())
    }
}
