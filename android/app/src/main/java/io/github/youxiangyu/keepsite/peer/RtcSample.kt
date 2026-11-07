package io.github.youxiangyu.keepsite.peer

import io.github.youxiangyu.keepsite.core.MonitorCandidate
import io.github.youxiangyu.keepsite.core.MonitorRoute
import io.github.youxiangyu.keepsite.core.MonitorRtcState
import io.github.youxiangyu.keepsite.core.MonitorValues
import io.github.youxiangyu.keepsite.core.diagnostic

/** Only fixed selected-pair facts leave the adapter; candidate IDs/addresses/maps never enter this value. */
internal data class RtcSample(val local: MonitorCandidate = MonitorCandidate.UNKNOWN, val remote: MonitorCandidate = MonitorCandidate.UNKNOWN,
    val rttMs: Double? = null, val dtls: MonitorRtcState = MonitorRtcState.UNKNOWN) {
    val route: MonitorRoute get() = MonitorValues.selectedRoute(local.name.lowercase(), remote.name.lowercase())
    fun values() = MonitorValues(localCandidate = local, remoteCandidate = remote, rttMs = rttMs, dtlsState = dtls, route = route)
}

/** The lookup is used synchronously inside getStats, for these fixed keys only; no raw object is retained. */
internal fun selectedRtcSample(transports: List<String>, field: (String, String) -> Any?): RtcSample = diagnostic(RtcSample()) {
    if (transports.size != 1) return@diagnostic RtcSample()
    val transport = transports.single()
    val dtls = when (field(transport, "dtlsState")) {
        "new" -> MonitorRtcState.NEW; "connecting" -> MonitorRtcState.CONNECTING; "connected" -> MonitorRtcState.CONNECTED
        "closed" -> MonitorRtcState.CLOSED; "failed" -> MonitorRtcState.FAILED; else -> MonitorRtcState.UNKNOWN
    }
    val pair = field(transport, "selectedCandidatePairId") as? String ?: return@diagnostic RtcSample(dtls = dtls)
    if (field(pair, "type") != "candidate-pair" || field(pair, "state") != "succeeded") return@diagnostic RtcSample(dtls = dtls)
    val local = field(pair, "localCandidateId") as? String
    val remote = field(pair, "remoteCandidateId") as? String
    fun candidate(id: String?, type: String): MonitorCandidate {
        if (id == null || field(id, "type") != type) return MonitorCandidate.UNKNOWN
        return when (field(id, "candidateType")) { "host" -> MonitorCandidate.HOST; "srflx" -> MonitorCandidate.SRFLX
            "prflx" -> MonitorCandidate.PRFLX; "relay" -> MonitorCandidate.RELAY; else -> MonitorCandidate.UNKNOWN }
    }
    val rtt = (field(pair, "currentRoundTripTime") as? Number)?.toDouble()?.takeIf { it.isFinite() && it >= 0 && it <= 9_007_199_254_740.991 }?.times(1000)
    RtcSample(candidate(local, "local-candidate"), candidate(remote, "remote-candidate"), rtt, dtls)
}
