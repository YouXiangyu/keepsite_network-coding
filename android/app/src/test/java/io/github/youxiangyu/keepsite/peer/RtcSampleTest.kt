package io.github.youxiangyu.keepsite.peer

import io.github.youxiangyu.keepsite.core.MonitorCandidate
import io.github.youxiangyu.keepsite.core.MonitorRoute
import io.github.youxiangyu.keepsite.core.MonitorRtcState
import org.junit.Assert.*
import org.junit.Test

class RtcSampleTest {
    private fun rows(): MutableMap<String, MutableMap<String, Any?>> = mutableMapOf(
        "transport" to mutableMapOf<String, Any?>("type" to "transport", "selectedCandidatePairId" to "selected", "dtlsState" to "connected"),
        "selected" to mutableMapOf("type" to "candidate-pair", "state" to "succeeded", "localCandidateId" to "local", "remoteCandidateId" to "remote", "currentRoundTripTime" to 0.025),
        "local" to mutableMapOf("type" to "local-candidate", "candidateType" to "host", "address" to "PRIVATE-IP"),
        "remote" to mutableMapOf("type" to "remote-candidate", "candidateType" to "srflx", "address" to "PRIVATE-IP"),
        "other" to mutableMapOf("type" to "candidate-pair", "state" to "succeeded", "currentRoundTripTime" to 999)
    )
    @Test fun onlyActualSelectedPairProducesSafeRouteRttAndDtls() {
        val rows = rows(); val result = selectedRtcSample(listOf("transport")) { id, field -> rows[id]?.get(field) }
        assertEquals(MonitorRoute.P2P_DIRECT, result.route); assertEquals(25.0, result.rttMs!!, 0.0001)
        assertEquals(MonitorRtcState.CONNECTED, result.dtls); assertEquals(MonitorCandidate.SRFLX, result.remote)
        assertFalse(result.toString().contains("PRIVATE")); assertFalse(result.values().toString().contains("selected"))
        rows["remote"]!!["candidateType"] = "relay"
        assertEquals(MonitorRoute.P2P_RELAY, selectedRtcSample(listOf("transport")) { id, key -> rows[id]?.get(key) }.route)
    }
    @Test fun missingAmbiguousUnselectedOrUnsuccessfulPairIsUnknown() {
        assertEquals(RtcSample(), selectedRtcSample(emptyList()) { _, _ -> error("Must not inspect any raw fields") })
        assertEquals(RtcSample(), selectedRtcSample(listOf("one", "two")) { _, _ -> error("Ambiguous transport") })
        for ((key, value) in listOf("selectedCandidatePairId" to "missing", "selectedCandidatePairId" to 123)) {
            val rows = rows(); rows["transport"]!![key] = value
            val result = selectedRtcSample(listOf("transport")) { id, field -> rows[id]?.get(field) }
            assertEquals(MonitorRoute.P2P_UNKNOWN, result.route); assertNull(result.rttMs)
        }
        for (field in listOf("type", "state")) {
            val rows = rows(); rows["selected"]!![field] = "not-proven"
            val result = selectedRtcSample(listOf("transport")) { id, key -> rows[id]?.get(key) }
            assertEquals(MonitorRoute.P2P_UNKNOWN, result.route); assertNull(result.rttMs)
        }
    }
    @Test fun invalidOrMissingFieldsNeverBecomeInventedStats() {
        for (value in listOf(null, -1, Double.NaN, Double.POSITIVE_INFINITY, "0.05", 1e20)) {
            val rows = rows(); rows["selected"]!!["currentRoundTripTime"] = value
            assertNull(selectedRtcSample(listOf("transport")) { id, key -> rows[id]?.get(key) }.rttMs)
        }
        val rows = rows(); rows["local"]!!["candidateType"] = "unknown-private"; rows["transport"]!!["dtlsState"] = "private-error"
        val result = selectedRtcSample(listOf("transport")) { id, field -> rows[id]?.get(field) }
        assertEquals(MonitorRoute.P2P_UNKNOWN, result.route); assertEquals(MonitorRtcState.UNKNOWN, result.dtls)
        rows["local"]!!["candidateType"] = "relay"; rows["local"]!!["type"] = "remote-candidate"
        assertEquals(MonitorCandidate.UNKNOWN, selectedRtcSample(listOf("transport")) { id, key -> rows[id]?.get(key) }.local)
    }
    @Test fun statsProjectionFailureHasNoRawErrorAndDoesNotEscapeToBusiness() {
        assertEquals(RtcSample(), selectedRtcSample(listOf("transport")) { _, _ -> throw IllegalStateException("PRIVATE raw stats") })
    }
}
