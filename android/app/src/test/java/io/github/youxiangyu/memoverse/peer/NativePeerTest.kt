package io.github.youxiangyu.memoverse.peer

import io.github.youxiangyu.memoverse.core.MediaRef
import io.github.youxiangyu.memoverse.core.PeerView
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.Closeable
import java.util.concurrent.CancellationException
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.ExecutionException
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import java.util.concurrent.atomic.AtomicLong

class NativePeerTest {
    companion object {
        private const val SDP = "v=0\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\na=max-message-size:65536\r\n"
        private val image = ByteArray(40000) { (it % 251).toByte() }.also {
            byteArrayOf(-119, 80, 78, 71, 13, 10, 26, 10).copyInto(it)
        }
        private val media = MediaRef(PeerWire.sha256(image), PeerWire.sha256(image), image.size.toLong(), "image/png", 256, 384)
        private fun json(vararg values: Pair<String, Any?>) = JSONObject().apply { values.forEach { (key, value) -> put(key, value ?: JSONObject.NULL) } }
        private fun waitUntil(check: () -> Boolean) {
            val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(3)
            while (!check() && System.nanoTime() < deadline) Thread.sleep(10)
            assertTrue("Expected bounded peer state transition", check())
        }
    }

    private class Server(private val available: Boolean = true, initialRevision: Long = 0, private val blockKind: String? = null) : EventPort {
        val listeners = CopyOnWriteArrayList<(JSONObject) -> Unit>()
        val opens = AtomicInteger()
        var revision = initialRevision
        val replacements = AtomicInteger()
        val manifest = mutableSetOf<String>()
        val cancelledIds = CopyOnWriteArrayList<String>()
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        override fun state() = EventState(true, "server", 1)
        override fun subscribeConnection(listener: () -> Unit) = Closeable { }
        override fun subscribeMessages(listener: (JSONObject) -> Unit): Closeable {
            listeners.add(listener); return Closeable { listeners.remove(listener) }
        }
        private fun reply(request: JSONObject, type: String, vararg values: Pair<String, Any?>) {
            val response = json("v" to 1, "type" to type, "userId" to "user", "serverId" to "server", "requestId" to request.getString("requestId"))
            values.forEach { (key, value) -> response.put(key, value ?: JSONObject.NULL) }
            listeners.forEach { it(response) }
        }
        override fun send(message: JSONObject): Boolean {
            if (message.getString("type") == blockKind) { entered.countDown(); check(release.await(3, TimeUnit.SECONDS)) }
            when (message.getString("type")) {
                "device.register" -> reply(message, "device.registered", "enabled" to true, "peerId" to "nativepeer", "reason" to null,
                    "heartbeatMs" to 15000, "leaseMs" to 45000, "inventoryRevision" to revision, "iceServers" to JSONArray(), "turnEnabled" to false,
                    "limits" to json("maxInventory" to 1024, "maxBatchAssets" to 32, "maxControlBytes" to 4096,
                        "maxSignalBytes" to 65536, "maxSdpBytes" to 49152, "maxIceBytes" to 2048, "maxActiveTransfers" to 2))
                "device.list" -> reply(message, "device.peers", "peers" to JSONArray().put(json("peerId" to "webpeer", "name" to "Browser")))
                "device.heartbeat" -> reply(message, "device.alive", "leaseMs" to 45000, "inventoryRevision" to revision)
                "cache.replace" -> {
                    replacements.incrementAndGet(); manifest.clear()
                    val assets = message.getJSONArray("assetIds")
                    for (index in 0 until assets.length()) manifest.add(assets.getString(index))
                    revision = message.getLong("revision")
                    reply(message, "cache.ack", "revision" to revision, "complete" to true, "count" to manifest.size, "nextBatchIndex" to null)
                }
                "cache.patch" -> {
                    for (key in listOf("add", "remove")) {
                        val assets = message.getJSONArray(key)
                        for (index in 0 until assets.length()) if (key == "add") manifest.add(assets.getString(index)) else manifest.remove(assets.getString(index))
                    }
                    revision = message.getLong("revision")
                    reply(message, "cache.ack", "revision" to revision, "complete" to true, "count" to manifest.size, "nextBatchIndex" to null)
                }
                "peer.find" -> reply(message, "peer.sources", "assetId" to media.assetId,
                    "peers" to JSONArray().apply { if (available) put(json("peerId" to "webpeer", "name" to "Browser")) })
                "peer.open" -> {
                    val id = "transfer${opens.incrementAndGet()}"
                    reply(message, "peer.pending", "transferId" to id)
                    reply(message, "peer.authorized", "transferId" to id, "sourcePeerId" to "webpeer", "requesterPeerId" to "nativepeer",
                        "offererPeerId" to "nativepeer", "leaseMs" to 10000,
                        "media" to json("assetId" to media.assetId, "sha256" to media.sha256, "size" to media.size, "mimeType" to media.mimeType))
                }
                "peer.signal" -> {
                    reply(message, "peer.signalled", "transferId" to message.getString("transferId"))
                    if (message.getString("kind") == "offer") reply(message, "peer.signal", "transferId" to message.getString("transferId"),
                        "fromPeerId" to "webpeer", "kind" to "answer", "payload" to json("type" to "answer", "sdp" to SDP))
                }
                "peer.renew" -> reply(message, "peer.lease", "transferId" to message.getString("transferId"), "leaseMs" to 10000)
                "peer.cancel" -> cancelledIds.add(message.getString("transferId"))
                "peer.accept" -> {
                    val id = message.getString("transferId")
                    reply(message, "peer.authorized", "transferId" to id, "sourcePeerId" to "nativepeer", "requesterPeerId" to "webpeer",
                        "offererPeerId" to "webpeer", "leaseMs" to 10000,
                        "media" to json("assetId" to media.assetId, "sha256" to media.sha256, "size" to media.size, "mimeType" to media.mimeType))
                    reply(message, "peer.signal", "transferId" to id, "fromPeerId" to "webpeer", "kind" to "offer", "payload" to json("type" to "offer", "sdp" to SDP))
                }
            }
            return true
        }
        fun incomingSource() {
            val request = json("v" to 1, "type" to "peer.request", "serverId" to "server", "userId" to "user",
                "transferId" to "served", "requesterPeerId" to "webpeer", "leaseMs" to 5000,
                "media" to json("assetId" to media.assetId, "sha256" to media.sha256, "size" to media.size, "mimeType" to media.mimeType))
            listeners.forEach { it(request) }
        }
        fun completeTransfer(id: String) {
            val message = json("v" to 1, "type" to "peer.cancelled", "serverId" to "server", "userId" to "user", "transferId" to id, "reason" to "complete")
            listeners.forEach { it(message) }
        }
    }

    private class Native(private val interruptFirst: Boolean = false, private val stall: Boolean = false, private val delayVerified: Boolean = false) : RtcFactory {
        val created = AtomicInteger()
        val disposed = AtomicInteger()
        val channelDisposed = AtomicInteger()
        val gets = CopyOnWriteArrayList<Int>()
        val verified = AtomicInteger()
        val queuedVerification = AtomicInteger()
        val drainVerification = AtomicReference<() -> Unit>()
        override fun create(listener: RtcPeer.Listener): RtcPeer {
            val ordinal = created.incrementAndGet()
            return object : RtcPeer {
                var gone = false
                lateinit var channel: Channel
                inner class Channel(override val label: String) : RtcChannel {
                    override var open = false
                    @Volatile var buffered = 0L
                    override val bufferedAmount get() = buffered
                    var gone = false
                    lateinit var observer: RtcChannel.Listener
                    override fun observe(listener: RtcChannel.Listener) { observer = listener }
                    override fun send(bytes: ByteArray, binary: Boolean): Boolean {
                        check(!binary && !gone)
                        val value = JSONObject(bytes.toString(Charsets.UTF_8))
                        if (value.getString("type") == "get") {
                            var offset = value.getInt("offset"); gets.add(offset)
                            while (offset < image.size) {
                                val count = minOf(PeerWire.FRAME_BYTES - 8, image.size - offset)
                                observer.received(PeerWire.encode(offset, image, offset, count), true); offset += count
                                if (stall || interruptFirst && ordinal == 1) {
                                    if (!stall) observer.closed()
                                    return true
                                }
                            }
                            observer.received(json("v" to 1, "type" to "end", "size" to image.size).toString().toByteArray(), false)
                        } else if (value.getString("type") == "verified") {
                            if (delayVerified) {
                                buffered = bytes.size.toLong()
                                drainVerification.set { buffered = 0; verified.incrementAndGet(); observer.bufferedAmountChanged() }
                                queuedVerification.incrementAndGet()
                            } else verified.incrementAndGet()
                        }
                        return true
                    }
                    override fun close() { if (!gone) { gone = true; open = false; channelDisposed.incrementAndGet(); observer.closed() } }
                }
                override fun createChannel(label: String): RtcChannel = Channel(label).also { channel = it }
                override fun createLocal(type: String, result: (Result<RtcDescription>) -> Unit) { result(Result.success(RtcDescription(type, SDP))) }
                override fun setRemote(description: RtcDescription, result: (Boolean) -> Unit) { result(true); channel.open = true; channel.observer.opened() }
                override fun addIce(ice: RtcIce?) = Unit
                override fun route(result: (String) -> Unit) { result("direct") }
                override fun close() { if (!gone) { gone = true; channel.close(); disposed.incrementAndGet() } }
            }
        }
        override fun close() = Unit
    }

    private class Cache : PeerCache {
        override fun inventory() = emptyList<MediaRef>()
        override fun read(media: MediaRef): ByteArray? = error("A receiving client must not read source bytes from its local cache")
        override fun canShare(media: MediaRef) = false
        override fun subscribe(listener: () -> Unit) = Closeable { }
    }
    private class SourceCache : PeerCache {
        @Volatile var available = true
        @Volatile var readable = true
        val listeners = CopyOnWriteArrayList<() -> Unit>()
        override fun inventory() = if (available) listOf(media) else emptyList()
        override fun read(media: MediaRef) = if (available && readable) image.copyOf() else null
        override fun canShare(media: MediaRef) = available && PeerJson.same(media, NativePeerTest.media)
        override fun subscribe(listener: () -> Unit): Closeable { listeners.add(listener); return Closeable { listeners.remove(listener) } }
        fun clear() { available = false; listeners.forEach { it() } }
    }
    private class SourceNative : RtcFactory {
        val channelClosed = AtomicInteger()
        val requests = AtomicInteger()
        val binarySent = AtomicInteger()
        @Volatile var blocked = true
        @Volatile var wake: (() -> Unit)? = null
        override fun create(listener: RtcPeer.Listener): RtcPeer = object : RtcPeer {
            var gone = false
            val channel = object : RtcChannel {
                var gone = false
                override val label = "memoverse-served"
                override val open get() = !gone
                override val bufferedAmount get() = if (blocked) PeerWire.BUFFER_HIGH else 0
                override fun observe(listener: RtcChannel.Listener) {
                    wake = { listener.bufferedAmountChanged() }
                    listener.opened(); requests.incrementAndGet()
                    listener.received(json("v" to 1, "type" to "get", "assetId" to media.assetId, "offset" to 0).toString().toByteArray(), false)
                }
                override fun send(bytes: ByteArray, binary: Boolean): Boolean { if (binary) binarySent.incrementAndGet(); return true }
                override fun close() { if (!gone) { gone = true; channelClosed.incrementAndGet() } }
            }
            override fun createChannel(label: String): RtcChannel = error("Source cannot create the offerer's channel")
            override fun createLocal(type: String, result: (Result<RtcDescription>) -> Unit) { result(Result.success(RtcDescription(type, SDP))) }
            override fun setRemote(description: RtcDescription, result: (Boolean) -> Unit) { result(true); listener.channel(channel) }
            override fun addIce(ice: RtcIce?) = Unit
            override fun route(result: (String) -> Unit) { result("direct") }
            override fun close() { if (!gone) { gone = true; channel.close() } }
        }
        override fun close() = Unit
    }
    private fun peer(server: Server, native: Native): NativePeer {
        val view = AtomicReference(PeerView())
        val peer = NativePeer("user", server, Cache(), { view.set(it) }, native)
        waitUntil { view.get().registered }
        return peer
    }

    @Test fun realProtocolSequenceReturnsOnlyFullyVerifiedBytesAndClosesItsConnections() {
        val native = Native(); val peer = peer(Server(), native)
        try {
            val result = peer.fetch(media) { false }!!
            assertArrayEquals(image, result.bytes); assertEquals("direct", result.route)
            assertEquals(1, native.verified.get()); assertEquals(listOf(0), native.gets)
            assertEquals(native.created.get(), native.disposed.get()); assertEquals(1, native.channelDisposed.get())
        } finally { peer.close() }
    }

    @Test fun oneInterruptedChannelRequestsTheRetainedPrefixAndDoesNotRestartAtZero() {
        val server = Server(); val native = Native(interruptFirst = true); val peer = peer(server, native)
        try {
            val result = peer.fetch(media) { false }!!
            assertArrayEquals(image, result.bytes)
            assertEquals(listOf(0, PeerWire.FRAME_BYTES - 8), native.gets)
            assertEquals((PeerWire.FRAME_BYTES - 8).toLong(), result.resumedBytes)
            assertEquals(2, server.opens.get()); assertEquals(2, native.disposed.get()); assertEquals(2, native.channelDisposed.get())
        } finally { peer.close() }
    }

    @Test fun closingLifetimeCancelsBlockedReadsInsteadOfReturningCloudFallback() {
        val native = Native(stall = true); val peer = peer(Server(), native)
        val readers = Executors.newFixedThreadPool(2)
        try {
            val one = readers.submit<PeerDownload?> { peer.fetch(media) { false } }
            val two = readers.submit<PeerDownload?> { peer.fetch(media) { false } }
            waitUntil { native.gets.size == 2 }
            assertNull("The third transfer must not exceed the combined two-transfer limit", peer.fetch(media) { false })
            peer.close()
            for (read in listOf(one, two)) {
                try { read.get(2, TimeUnit.SECONDS); fail("A closed lifetime cannot trigger cloud fallback") }
                catch (error: ExecutionException) { assertTrue(error.cause is CancellationException) }
            }
            assertTrue(peer.awaitClosed())
            assertEquals(2, native.disposed.get()); assertEquals(2, native.channelDisposed.get())
            try { peer.fetch(media) { false }; fail("Closed peer must cancel") } catch (_: CancellationException) { }
        } finally { peer.close(); readers.shutdownNow() }
    }

    @Test fun absentSourceFallsBackWithoutCreatingANativeConnection() {
        val native = Native(); val peer = peer(Server(available = false), native)
        try { assertNull(peer.fetch(media) { false }); assertEquals(0, native.created.get()) }
        finally { peer.close() }
    }

    @Test fun restoredNonzeroRevisionStillPublishesAnEmptyLocalManifest() {
        val server = Server(initialRevision = 7).also { it.manifest.add(media.assetId) }
        val peer = peer(server, Native())
        try { waitUntil { server.replacements.get() > 0 }; assertEquals(8L, server.revision); assertTrue(server.manifest.isEmpty()) }
        finally { peer.close(); assertTrue(peer.awaitClosed()) }
    }

    @Test fun cacheRevocationStopsAnAlreadyReadSourceWithoutWaitingForItsLease() {
        val server = Server(); val native = SourceNative(); val cache = SourceCache(); val view = AtomicReference(PeerView())
        val peer = NativePeer("user", server, cache, { view.set(it) }, native)
        try {
            waitUntil { view.get().registered && server.replacements.get() > 0 }
            server.incomingSource(); waitUntil { native.requests.get() == 1 }
            cache.clear(); waitUntil { native.channelClosed.get() == 1 }
            assertTrue(server.cancelledIds.contains("served"))
            waitUntil { server.manifest.isEmpty() }
        } finally { peer.close(); assertTrue(peer.awaitClosed()) }
    }

    @Test fun pumpRechecksSharingEvenBeforeTheInventoryNotificationArrives() {
        val server = Server(); val native = SourceNative(); val cache = SourceCache(); val view = AtomicReference(PeerView())
        val peer = NativePeer("user", server, cache, { view.set(it) }, native)
        try {
            waitUntil { view.get().registered && server.replacements.get() > 0 }
            server.incomingSource(); waitUntil { native.requests.get() == 1 }
            cache.available = false; native.blocked = false; native.wake!!()
            waitUntil { native.channelClosed.get() == 1 }
            assertEquals(0, native.binarySent.get()); assertTrue(server.cancelledIds.contains("served"))
        } finally { peer.close(); assertTrue(peer.awaitClosed()) }
    }

    @Test fun failedSourceReadWithdrawsTheAdvertisedAsset() {
        val server = Server(); val native = SourceNative(); val cache = SourceCache(); val view = AtomicReference(PeerView())
        val peer = NativePeer("user", server, cache, { view.set(it) }, native)
        try {
            waitUntil { view.get().registered && server.replacements.get() > 0 }
            assertTrue(server.manifest.contains(media.assetId)); cache.readable = false
            server.incomingSource(); waitUntil { server.cancelledIds.contains("served") && server.manifest.isEmpty() }
            assertEquals(0, native.requests.get()); assertEquals(0, native.binarySent.get())
        } finally { peer.close(); assertTrue(peer.awaitClosed()) }
    }

    @Test fun hardDeadlineFallsBackEvenWhenTheSignallingExecutorIsBlocked() {
        val server = Server(blockKind = "peer.find"); val native = Native(); val clock = AtomicLong(0); val view = AtomicReference(PeerView())
        val peer = NativePeer("user", server, Cache(), { view.set(it) }, native) { clock.get() }
        val reader = Executors.newSingleThreadExecutor()
        try {
            waitUntil { view.get().registered }
            val task = reader.submit<PeerDownload?> { peer.fetch(media) { false } }
            assertTrue(server.entered.await(1, TimeUnit.SECONDS)); clock.set(12001)
            assertNull("Budget expiry is a fallback, not user cancellation", task.get(1, TimeUnit.SECONDS))
            server.release.countDown(); waitUntil { view.get().error.contains("超时") }
            assertEquals(0, server.opens.get())
            assertEquals(0, native.created.get())
        } finally { server.release.countDown(); peer.close(); assertTrue(peer.awaitClosed()); reader.shutdownNow() }
    }

    @Test fun closeNeverBlocksTheUiOrAcceptsALateAuthorization() {
        val server = Server(blockKind = "peer.open"); val native = Native(); val peer = peer(server, native)
        val reader = Executors.newSingleThreadExecutor()
        try {
            val task = reader.submit<PeerDownload?> { peer.fetch(media) { false } }
            assertTrue(server.entered.await(1, TimeUnit.SECONDS))
            val start = System.nanoTime(); peer.close()
            assertTrue("UI close must not wait on signalling", System.nanoTime() - start < TimeUnit.MILLISECONDS.toNanos(100))
            try { task.get(1, TimeUnit.SECONDS); fail("Account close must cancel, not fall back") }
            catch (error: ExecutionException) { assertTrue(error.cause is CancellationException) }
            server.release.countDown(); assertTrue(peer.awaitClosed()); assertEquals(0, native.created.get())
        } finally { server.release.countDown(); peer.close(); reader.shutdownNow() }
    }

    @Test fun receiverWaitsForVerifiedBufferDrainEvenAfterRemoteComplete() {
        val server = Server(); val native = Native(delayVerified = true); val peer = peer(server, native)
        val reader = Executors.newSingleThreadExecutor()
        try {
            val task = reader.submit<PeerDownload?> { peer.fetch(media) { false } }
            waitUntil { native.queuedVerification.get() == 1 }
            server.completeTransfer("transfer1")
            assertFalse(task.isDone); assertEquals(0, native.disposed.get())
            native.drainVerification.get().invoke()
            assertArrayEquals(image, task.get(2, TimeUnit.SECONDS)!!.bytes)
            assertEquals(1, native.verified.get()); assertEquals(1, native.disposed.get())
        } finally { peer.close(); assertTrue(peer.awaitClosed()); reader.shutdownNow() }
    }

    @Test fun undrainedVerifiedControlCannotProduceOneSidedTransferSuccess() {
        val server = Server(); val native = Native(delayVerified = true); val peer = peer(server, native)
        val reader = Executors.newSingleThreadExecutor()
        try {
            val task = reader.submit<PeerDownload?> { peer.fetch(media) { false } }
            waitUntil { native.queuedVerification.get() == 1 }
            assertNull(task.get(2, TimeUnit.SECONDS))
            assertEquals(0, native.verified.get()); assertEquals(1, native.disposed.get())
        } finally { peer.close(); assertTrue(peer.awaitClosed()); reader.shutdownNow() }
    }
}
