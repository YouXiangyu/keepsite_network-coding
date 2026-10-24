package io.github.youxiangyu.keepsite.peer

import android.content.Context
import org.webrtc.DataChannel
import org.webrtc.IceCandidate
import org.webrtc.Logging
import org.webrtc.MediaConstraints
import org.webrtc.MediaStream
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.SdpObserver
import org.webrtc.SessionDescription
import java.nio.ByteBuffer
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/** API signatures verified against the resolved WebRTC-SDK 150.7871.01 AAR. No media tracks. */
internal class AndroidRtcFactory(private val context: Context) : RtcFactory {
    companion object {
        private var initialized = false
        @Synchronized private fun initialize(context: Context) {
            if (initialized) return
            PeerConnectionFactory.initialize(PeerConnectionFactory.InitializationOptions.builder(context)
                .setEnableInternalTracer(false)
                .setInjectableLogger({ _, _, _ -> }, Logging.Severity.LS_NONE)
                .createInitializationOptions())
            initialized = true
        }
    }
    private val closed = AtomicBoolean(false)
    private var factory: PeerConnectionFactory? = null
    private val peers = ConcurrentHashMap.newKeySet<AndroidPeer>()
    private val callbacks = ThreadPoolExecutor(1, 1, 0, TimeUnit.MILLISECONDS, ArrayBlockingQueue(256),
        { run -> Thread(run, "keepsite-peer-native-callback").apply { isDaemon = true } }, ThreadPoolExecutor.AbortPolicy())

    override fun create(listener: RtcPeer.Listener): RtcPeer {
        check(!closed.get())
        if (factory == null) { initialize(context); factory = PeerConnectionFactory.builder().createPeerConnectionFactory() }
        return AndroidPeer(factory!!, listener).also { peers.add(it) }
    }

    private inner class AndroidPeer(factory: PeerConnectionFactory, private val listener: RtcPeer.Listener) : RtcPeer {
        private val lock = Any()
        private val disposed = AtomicBoolean(false)
        private val failedOnce = AtomicBoolean(false)
        private val channels = ConcurrentHashMap.newKeySet<AndroidChannel>()
        private val native: PeerConnection
        private fun dispatch(action: () -> Unit) {
            if (disposed.get()) return
            try { callbacks.execute { if (!disposed.get()) action() } }
            catch (_: RejectedExecutionException) { if (!closed.get() && failedOnce.compareAndSet(false, true)) listener.failed() }
        }
        private fun channel(native: DataChannel): AndroidChannel = AndroidChannel(native).also { channels.add(it) }
        init {
            val configuration = PeerConnection.RTCConfiguration(emptyList()).apply {
                sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
                continualGatheringPolicy = PeerConnection.ContinualGatheringPolicy.GATHER_ONCE
            }
            native = factory.createPeerConnection(configuration, object : PeerConnection.Observer {
                override fun onSignalingChange(state: PeerConnection.SignalingState) = Unit
                override fun onIceConnectionChange(state: PeerConnection.IceConnectionState) {
                    if (state in setOf(PeerConnection.IceConnectionState.FAILED, PeerConnection.IceConnectionState.DISCONNECTED)) dispatch { listener.failed() }
                }
                override fun onConnectionChange(state: PeerConnection.PeerConnectionState) {
                    if (state in setOf(PeerConnection.PeerConnectionState.FAILED, PeerConnection.PeerConnectionState.DISCONNECTED)) dispatch { listener.failed() }
                }
                override fun onIceConnectionReceivingChange(receiving: Boolean) = Unit
                override fun onIceGatheringChange(state: PeerConnection.IceGatheringState) {
                    if (state == PeerConnection.IceGatheringState.COMPLETE) dispatch { listener.ice(null) }
                }
                override fun onIceCandidate(candidate: IceCandidate) {
                    val copied = RtcIce(candidate.sdp, candidate.sdpMid, candidate.sdpMLineIndex)
                    dispatch { listener.ice(copied) }
                }
                override fun onIceCandidatesRemoved(candidates: Array<out IceCandidate>) = Unit
                override fun onAddStream(stream: MediaStream) = Unit
                override fun onRemoveStream(stream: MediaStream) = Unit
                override fun onDataChannel(dataChannel: DataChannel) {
                    // Retain every native channel immediately, including one that races close().
                    val wrapped = channel(dataChannel)
                    dispatch { listener.channel(wrapped) }
                }
                override fun onRenegotiationNeeded() = Unit
            }) ?: throw IllegalStateException("Native peer creation failed")
            native.setAudioPlayout(false); native.setAudioRecording(false)
        }

        override fun createChannel(label: String): RtcChannel = synchronized(lock) {
            check(!disposed.get())
            channel(native.createDataChannel(label, DataChannel.Init().apply {
                ordered = true; maxRetransmits = -1; maxRetransmitTimeMs = -1; negotiated = false
            }))
        }

        override fun createLocal(type: String, result: (Result<RtcDescription>) -> Unit) = synchronized(lock) {
            check(!disposed.get()); require(type == "offer" || type == "answer")
            val observer = object : SdpObserver {
                override fun onCreateSuccess(description: SessionDescription) = dispatch {
                    synchronized(lock) {
                        if (disposed.get()) return@synchronized
                        native.setLocalDescription(object : SdpObserver {
                            override fun onSetSuccess() = dispatch { result(Result.success(RtcDescription(description.type.canonicalForm(), description.description))) }
                            override fun onSetFailure(error: String) = dispatch { result(Result.failure(IllegalStateException("Local SDP rejected"))) }
                            override fun onCreateFailure(error: String) = Unit
                            override fun onCreateSuccess(description: SessionDescription) = Unit
                        }, description)
                    }
                }
                override fun onCreateFailure(error: String) = dispatch { result(Result.failure(IllegalStateException("SDP creation failed"))) }
                override fun onSetSuccess() = Unit
                override fun onSetFailure(error: String) = Unit
            }
            if (type == "offer") native.createOffer(observer, MediaConstraints()) else native.createAnswer(observer, MediaConstraints())
        }

        override fun setRemote(description: RtcDescription, result: (Boolean) -> Unit) = synchronized(lock) {
            check(!disposed.get())
            native.setRemoteDescription(object : SdpObserver {
                override fun onSetSuccess() = dispatch { result(true) }
                override fun onSetFailure(error: String) = dispatch { result(false) }
                override fun onCreateFailure(error: String) = Unit
                override fun onCreateSuccess(description: SessionDescription) = Unit
            }, SessionDescription(SessionDescription.Type.fromCanonicalForm(description.type), description.sdp))
        }

        override fun addIce(ice: RtcIce?) = synchronized(lock) {
            check(!disposed.get())
            // Java's API has no end-of-candidates overload; null means gathering completed.
            if (ice != null && ice.candidate.isNotEmpty()) {
                require(ice.mid != null || ice.line != null)
                check(native.addIceCandidate(IceCandidate(ice.mid, ice.line ?: -1, ice.candidate))) { "ICE candidate rejected" }
            }
        }

        override fun route(result: (String) -> Unit) = synchronized(lock) {
            if (disposed.get()) { result("unknown"); return@synchronized }
            native.getStats { report ->
                val all = report.statsMap
                val transport = all.values.firstOrNull { it.type == "transport" && it.members["selectedCandidatePairId"] is String }
                val pair = transport?.members?.get("selectedCandidatePairId")?.let { all[it] }
                val local = pair?.members?.get("localCandidateId")?.let { all[it] }?.members?.get("candidateType")
                val remote = pair?.members?.get("remoteCandidateId")?.let { all[it] }?.members?.get("candidateType")
                val valid = setOf("host", "srflx", "prflx", "relay")
                val route = if (local !in valid || remote !in valid) "unknown" else if (local == "relay" || remote == "relay") "relay" else "direct"
                dispatch { result(route) }
            }
        }

        override fun close() = synchronized(lock) {
            if (!disposed.compareAndSet(false, true)) return@synchronized
            // Never invoked from a native Observer stack: NativePeer owns disposal.
            try { native.close(); channels.toList().forEach { it.close() } }
            finally {
                native.dispose()
                channels.toList().forEach { it.close() }; channels.clear(); peers.remove(this)
            }
        }

        private inner class AndroidChannel(private val native: DataChannel) : RtcChannel {
            private val channelLock = Any()
            private val gone = AtomicBoolean(false)
            private var listener: RtcChannel.Listener? = null
            private val awaiting = ArrayDeque<Pair<Int, (RtcChannel.Listener) -> Unit>>()
            private var awaitingBytes = 0
            override val label: String = native.label()
            override val open: Boolean get() = synchronized(channelLock) { !gone.get() && native.state() == DataChannel.State.OPEN }
            override val bufferedAmount: Long get() = synchronized(channelLock) { if (gone.get()) 0 else native.bufferedAmount() }
            private fun event(bytes: Int = 0, action: (RtcChannel.Listener) -> Unit) = dispatch {
                if (gone.get()) return@dispatch
                val target = listener
                if (target != null) action(target)
                else if (awaiting.size >= 32 || awaitingBytes + bytes > 256 * 1024) listenerFailure()
                else { awaiting.addLast(bytes to action); awaitingBytes += bytes }
            }
            private fun listenerFailure() { if (failedOnce.compareAndSet(false, true)) this@AndroidPeer.listener.failed() }
            init {
                native.registerObserver(object : DataChannel.Observer {
                    override fun onBufferedAmountChange(previousAmount: Long) = event { it.bufferedAmountChanged() }
                    override fun onStateChange() {
                        val state = try { native.state() } catch (_: IllegalStateException) { return }
                        if (state == DataChannel.State.OPEN) event { it.opened() }
                        else if (state == DataChannel.State.CLOSED) event { it.closed() }
                    }
                    override fun onMessage(buffer: DataChannel.Buffer) {
                        // The native ByteBuffer expires when this callback returns.
                        val remaining = buffer.data.remaining()
                        val maximum = if (buffer.binary) PeerWire.FRAME_BYTES else PeerWire.MAX_CONTROL_BYTES
                        val copied = if (remaining in 1..maximum) ByteArray(remaining).also { buffer.data.get(it) } else ByteArray(0)
                        event(copied.size) { it.received(copied, buffer.binary) }
                    }
                })
            }
            override fun observe(listener: RtcChannel.Listener) = dispatch {
                if (gone.get()) return@dispatch
                this.listener = listener
                while (awaiting.isNotEmpty()) { val (bytes, action) = awaiting.removeFirst(); awaitingBytes -= bytes; action(listener) }
            }
            override fun send(bytes: ByteArray, binary: Boolean): Boolean = synchronized(channelLock) {
                !gone.get() && native.state() == DataChannel.State.OPEN && native.send(DataChannel.Buffer(ByteBuffer.wrap(bytes), binary))
            }
            override fun close() = synchronized(channelLock) {
                if (!gone.compareAndSet(false, true)) return@synchronized
                try { native.unregisterObserver(); native.close() } finally { native.dispose(); channels.remove(this) }
            }
        }
    }

    override fun close() {
        if (!closed.compareAndSet(false, true)) return
        peers.toList().forEach { it.close() }
        factory?.dispose(); factory = null
        callbacks.shutdown()
        if (!Thread.currentThread().name.startsWith("keepsite-peer-native-callback")) callbacks.awaitTermination(2, TimeUnit.SECONDS)
    }
}
