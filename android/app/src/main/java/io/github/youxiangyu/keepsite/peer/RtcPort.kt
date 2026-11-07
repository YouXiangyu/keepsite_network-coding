package io.github.youxiangyu.keepsite.peer

import java.io.Closeable
import io.github.youxiangyu.keepsite.core.MonitorRtcState

/** The adapter copies native callback buffers before delivering them to this boundary. */
internal interface RtcChannel : Closeable {
    val label: String
    val open: Boolean
    val bufferedAmount: Long
    fun observe(listener: Listener)
    fun send(bytes: ByteArray, binary: Boolean): Boolean
    interface Listener {
        fun opened()
        fun received(bytes: ByteArray, binary: Boolean)
        fun bufferedAmountChanged()
        fun closed()
    }
}

internal data class RtcDescription(val type: String, val sdp: String)
internal data class RtcIce(val candidate: String, val mid: String?, val line: Int?, val username: String? = null)

internal interface RtcPeer : Closeable {
    fun createChannel(label: String): RtcChannel
    fun createLocal(type: String, result: (Result<RtcDescription>) -> Unit)
    fun setRemote(description: RtcDescription, result: (Boolean) -> Unit)
    fun addIce(ice: RtcIce?)
    fun stats(result: (RtcSample) -> Unit)
    interface Listener {
        fun ice(ice: RtcIce?)
        fun channel(channel: RtcChannel)
        fun failed()
        fun state(ice: MonitorRtcState? = null, pc: MonitorRtcState? = null) = Unit
    }
}

internal interface RtcFactory : Closeable {
    fun create(listener: RtcPeer.Listener): RtcPeer
}
