package io.github.youxiangyu.memoverse.peer

import android.content.Context
import io.github.youxiangyu.memoverse.core.DeviceView
import io.github.youxiangyu.memoverse.core.MediaRef
import io.github.youxiangyu.memoverse.core.PeerView
import io.github.youxiangyu.memoverse.core.TransferView
import org.json.JSONArray
import org.json.JSONObject
import java.io.Closeable
import java.util.UUID
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.CancellationException
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ExecutionException
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ScheduledThreadPoolExecutor
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/** One account/foreground lifetime. All signalling and native objects belong to [serial]. */
class NativePeer internal constructor(
    private val userId: String,
    private val port: EventPort,
    private val cache: PeerCache,
    private val onChanged: (PeerView) -> Unit,
    private val rtc: RtcFactory,
    private val now: () -> Long = { System.nanoTime() / 1_000_000 },
) : Closeable {
    constructor(context: Context, userId: String, port: EventPort, cache: PeerCache, onChanged: (PeerView) -> Unit) :
        this(userId, port, cache, onChanged, AndroidRtcFactory(context.applicationContext))

    private data class Pending(
        val expected: String, val deadline: Long, val owner: String?,
        val success: (JSONObject) -> Unit, val failure: (String) -> Unit,
    )
    private class Download(val media: MediaRef, val cancelled: () -> Boolean, val deadline: Long) {
        val id = UUID.randomUUID().toString()
        val aborted = AtomicBoolean(false)
        val result = CompletableFuture<PeerDownload?>()
        private val prefixStorage = lazy { ReceivePrefix(media.size.toInt()) }
        val prefix get() = prefixStorage.value
        fun clearPrefix() { if (prefixStorage.isInitialized()) prefixStorage.value.clear() }
        var source = ""
        var opening = false
        var resumedBytes = 0L
    }
    private class Transfer(
        val id: String, val media: MediaRef, val source: Boolean, val other: String,
        val deadline: Long, val download: Download?, var leaseUntil: Long,
    ) {
        var pc: RtcPeer? = null
        var channel: RtcChannel? = null
        var authorized = false
        var closed = false
        var channelOpened = false
        var verified = false
        var remoteComplete = false
        var progressAt = 0L
        var connectUntil = Long.MAX_VALUE
        var renewAt = Long.MAX_VALUE
        var renewing = false
        var remoteSet = false
        var settingRemote = false
        var localSet = false
        var frameLimit = PeerWire.FRAME_BYTES
        var candidates = 0
        var signalBytes = 0
        var signalling = false
        val localIce = ArrayDeque<RtcIce?>()
        val remoteIce = ArrayDeque<RtcIce?>()
        val signals = ArrayDeque<Pair<String, Any>>()
        var sourceBytes: ByteArray? = null
        var sending: SendProgress? = null
        val backpressure = SendBackpressure()
        var ended = false
        var verifying = false
        var verifiedBytes: ByteArray? = null
        var route = "unknown"
        var routePending = false
        var completionUntil = Long.MAX_VALUE
        var completionTask: java.util.concurrent.ScheduledFuture<*>? = null
        var payloadBytes = 0L
    }

    private val closed = AtomicBoolean(false)
    private val queuedCallbacks = AtomicInteger(0)
    private val callbackOverflow = AtomicBoolean(false)
    private val closeDone = CompletableFuture<Unit>()
    private val serial = ScheduledThreadPoolExecutor(1) { run -> Thread(run, "memoverse-peer-state").apply { isDaemon = true } }
        .apply { removeOnCancelPolicy = true }
    private val io = ThreadPoolExecutor(2, 2, 0, TimeUnit.MILLISECONDS, ArrayBlockingQueue(8),
        { run -> Thread(run, "memoverse-peer-file").apply { isDaemon = true } }, ThreadPoolExecutor.AbortPolicy())
    private var enabled = true
    private var generation = -1L
    private var serverId = ""
    private var peerId = ""
    private var peerUntil = 0L
    private var registering = false
    private var registerAt = 0L
    private var heartbeatAt = 0L
    private var listAt = 0L
    private var inventoryAt = Long.MAX_VALUE
    private var inventoryBusy = false
    private var inventoryDirty = false
    private var inventoryRevision = 0L
    private var replaceRequired = true
    private var advertised = emptySet<String>()
    private val withdrawn = mutableSetOf<String>()
    private var sourceCheckBusy = false
    private var sourceCheckDirty = false
    private var devices = emptyList<DeviceView>()
    private var error = ""
    private var dirty = true
    private val pending = linkedMapOf<String, Pending>()
    private val downloads = linkedMapOf<String, Download>()
    private val transfers = linkedMapOf<String, Transfer>()
    private val recent = ArrayDeque<TransferView>()
    private val early = ArrayDeque<Pair<Long, JSONObject>>()
    private var earlyBytes = 0
    private val subscriptions: List<Closeable>
    private val ticking: java.util.concurrent.ScheduledFuture<*>

    init {
        subscriptions = listOf(
            port.subscribeConnection { post { connectionChanged() } },
            port.subscribeMessages { incoming ->
                val state = port.state()
                val raw = incoming.toString()
                if (raw.toByteArray(Charsets.UTF_8).size <= 65536) post {
                    if (state.generation == generation && state.serverId == serverId) {
                        try { message(JSONObject(raw)) } catch (_: Exception) { reset("设备协议响应无效", false) }
                    }
                }
            },
            cache.subscribe { post {
                withdrawn.clear(); inventoryDirty = true; inventoryAt = now()
                sourceCheckDirty = true; checkSourceReferences()
            } },
        )
        ticking = serial.scheduleWithFixedDelay({ guarded { tick() } }, 100, 100, TimeUnit.MILLISECONDS)
        post { connectionChanged() }
    }

    private fun post(action: () -> Unit) {
        if (closed.get()) return
        if (queuedCallbacks.incrementAndGet() > 512) {
            queuedCallbacks.decrementAndGet()
            if (callbackOverflow.compareAndSet(false, true)) try { serial.execute {
                if (!closed.get()) reset("设备消息队列超限", false)
                callbackOverflow.set(false)
            } } catch (_: RejectedExecutionException) { }
            return
        }
        try { serial.execute { try { if (!closed.get()) guarded(action) } finally { queuedCallbacks.decrementAndGet() } } }
        catch (_: RejectedExecutionException) { queuedCallbacks.decrementAndGet() }
    }
    private fun guarded(action: () -> Unit) {
        try { action() } catch (_: Exception) { reset("设备传输暂时不可用", false) }
    }
    /** At most one timeout/cancellation cleanup per admitted request, outside the event quota. */
    private fun cleanup(action: () -> Unit) {
        try { serial.execute { guarded(action) } } catch (_: RejectedExecutionException) { }
    }
    private fun obj(vararg fields: Pair<String, Any?>) = JSONObject().apply { fields.forEach { (key, value) -> put(key, value ?: JSONObject.NULL) } }
    private fun errorMessage(code: String): String = when (code) {
        "capacity" -> "设备传输已达到并发上限"
        "rate_limited" -> "设备信令暂时限流，已改用云端"
        "not_found" -> "来源图片已不可用"
        "expired" -> "设备授权已过期"
        "unauthorized" -> "设备授权已撤销"
        "conflict" -> "设备缓存清单需要重新发布"
        else -> "设备服务暂时不可用，图片可从云端读取"
    }

    fun setEnabled(value: Boolean) = post {
        if (enabled != value) {
            enabled = value
            if (!value) { send("device.unregister"); reset("", false) } else { registerAt = 0; connectionChanged() }
            publish()
        }
    }

    /** Only call from a media worker. A closed account lifetime never returns a cloud-fallback result. */
    fun fetch(media: MediaRef, cancelled: () -> Boolean): PeerDownload? {
        if (closed.get() || cancelled()) throw CancellationException("设备读取已取消")
        require(PeerJson.valid(media))
        val request = Download(media, cancelled, now() + PeerWire.ATTEMPT_MS)
        post {
            if (!ready() || occupied() >= 2) { request.clearPrefix(); request.result.complete(null); return@post }
            downloads[request.id] = request; dirty = true
            rpc("peer.find", obj("assetId" to media.assetId), "peer.sources", request.id,
                success = { reply ->
                    if (!active(request)) { finish(request, null); return@rpc }
                    require(PeerJson.text(reply, "assetId", 64) == media.assetId)
                    val peers = PeerJson.array(reply, "peers", 8)
                    for (index in 0 until peers.length()) {
                        val id = PeerJson.token(peers.getJSONObject(index), "peerId")
                        if (id != peerId) { request.source = id; break }
                    }
                    if (request.source.isEmpty()) finish(request, null) else open(request)
                }, failure = { finish(request, null, errorMessage(it)) })
        }
        try {
            while (true) {
                if (closed.get() || cancelled()) {
                    request.aborted.set(true); cleanup { finish(request, null, cancel = true) }
                    throw CancellationException("设备读取已取消")
                }
                try {
                    val result = request.result.get(50, TimeUnit.MILLISECONDS)
                    if (closed.get() || cancelled()) throw CancellationException("设备读取已取消")
                    return result
                } catch (_: TimeoutException) {
                    if (now() >= request.deadline) {
                        // A blocked event executor must not keep the media worker waiting forever.
                        // New pumps, signalling and late results are fenced immediately. Native
                        // teardown follows on its owner thread; already queued wire bytes cannot be recalled.
                        request.aborted.set(true); request.result.complete(null)
                        cleanup { finish(request, null, "设备读取已超时，已改用云端") }
                        if (closed.get() || cancelled()) throw CancellationException("设备读取已取消")
                        return null
                    }
                }
            }
        } catch (failure: ExecutionException) {
            val cause = failure.cause
            if (cause is CancellationException) throw cause
            return null
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt(); request.aborted.set(true); cleanup { finish(request, null, cancel = true) }
            throw CancellationException("设备读取已取消")
        }
    }

    private fun ready() = !closed.get() && enabled && peerId.isNotEmpty() && now() < peerUntil
        && port.state().let { it.connected && it.generation == generation && it.serverId == serverId }
    private fun occupied() = downloads.size + transfers.values.count { it.source }
    private fun active(request: Download) = downloads[request.id] === request && !request.aborted.get()
        && !request.cancelled() && !closed.get() && now() < request.deadline
    private fun connectionChanged() {
        val state = port.state()
        if (state.generation != generation || state.serverId != serverId || !state.connected) {
            val changedSpace = serverId.isNotEmpty() && state.serverId.isNotEmpty() && state.serverId != serverId
            reset(if (state.connected) "" else "设备信令连接已断开", changedSpace)
            generation = state.generation; serverId = state.serverId
            if (state.connected) registerAt = 0
        }
        if (enabled && state.connected && serverId.isNotEmpty() && peerId.isEmpty() && !registering && now() >= registerAt) register()
    }
    private fun register() {
        registering = true
        rpc("device.register", obj("name" to "Android"), "device.registered", success = { reply ->
            registering = false
            require(PeerJson.number(reply, "heartbeatMs", 15000, 15000) == 15000L)
            require(PeerJson.number(reply, "leaseMs", 45000, 45000) == 45000L)
            require(!PeerJson.flag(reply, "turnEnabled") && PeerJson.array(reply, "iceServers", 0).length() == 0)
            val limits = reply.getJSONObject("limits")
            for ((key, maximum) in mapOf("maxInventory" to 1024L, "maxBatchAssets" to 32L, "maxControlBytes" to 4096L,
                "maxSignalBytes" to 65536L, "maxSdpBytes" to 49152L, "maxIceBytes" to 2048L, "maxActiveTransfers" to 2L)) {
                require(PeerJson.number(limits, key, maximum, maximum) == maximum)
            }
            if (!PeerJson.flag(reply, "enabled")) { reset("设备服务暂不可用", false); registerAt = now() + 2000; return@rpc }
            peerId = PeerJson.token(reply, "peerId"); peerUntil = now() + 45000
            inventoryRevision = PeerJson.number(reply, "inventoryRevision")
            advertised = emptySet(); replaceRequired = true; inventoryDirty = true; inventoryAt = now()
            heartbeatAt = now() + 15000; listAt = now(); error = ""; dirty = true
        }, failure = { registering = false; error = errorMessage(it); registerAt = now() + 2000; dirty = true })
    }

    private fun send(kind: String, fields: JSONObject = JSONObject(), requestId: String = UUID.randomUUID().toString()): Boolean {
        val message = obj("v" to 1, "type" to kind, "requestId" to requestId)
        fields.keys().forEach { key -> message.put(key, fields.get(key)) }
        val maximum = if (kind == "peer.signal" && fields.optString("kind") != "ice") 65536 else 4096
        return message.toString().toByteArray(Charsets.UTF_8).size <= maximum && port.send(message)
    }
    private fun rpc(kind: String, fields: JSONObject, expected: String, owner: String? = null,
        success: (JSONObject) -> Unit, failure: (String) -> Unit) {
        if (pending.size >= 64) { failure("capacity"); return }
        val id = UUID.randomUUID().toString()
        pending[id] = Pending(expected, now() + 3000, owner, success, failure)
        if (!send(kind, fields, id)) { pending.remove(id); failure("unavailable") }
    }
    private fun message(body: JSONObject) {
        require(PeerJson.number(body, "v", 1, 1) == 1L)
        require(PeerJson.text(body, "userId") == userId && PeerJson.text(body, "serverId") == serverId)
        val kind = PeerJson.text(body, "type", 40)
        val request = body.optString("requestId")
        val waiting = pending[request]
        if (waiting != null) {
            if (kind == "peer.error") { pending.remove(request); waiting.failure(PeerJson.text(body, "code", 40)); return }
            if (kind == waiting.expected) { pending.remove(request); waiting.success(body); return }
        }
        if (kind == "device.registered" && !body.optBoolean("enabled", false)) { reset("设备服务暂不可用", false); return }
        if (kind == "peer.request") { sourceRequest(body); return }
        if (kind !in setOf("peer.authorized", "peer.signal", "peer.cancelled", "peer.error")) return
        val id = body.optString("transferId")
        if (!PeerWire.isToken(id)) return
        val transfer = transfers[id]
        if (transfer == null) {
            if (downloads.values.any { it.opening } && kind in setOf("peer.authorized", "peer.signal", "peer.cancelled")) {
                val size = body.toString().toByteArray(Charsets.UTF_8).size
                if (early.size >= 16 || earlyBytes + size > 256 * 1024) { reset("设备信令缓存超限", false); return }
                early.addLast(now() to body); earlyBytes += size
            }
            return
        }
        if (!live(transfer)) return
        when (kind) {
            "peer.authorized" -> authorize(transfer, body)
            "peer.signal" -> signal(transfer, body)
            "peer.cancelled" -> {
                if (body.optString("reason") == "complete" && transfer.verified) {
                    transfer.remoteComplete = true; completeWhenFlushed(transfer)
                }
                else fail(transfer, "设备传输已撤销")
            }
            "peer.error" -> fail(transfer, errorMessage(body.optString("code")))
        }
    }

    /** Revocation does not wait for a cache-publish RPC or transfer renewal. */
    private fun checkSourceReferences() {
        if (sourceCheckBusy || !sourceCheckDirty || !ready()) return
        if (transfers.values.none { it.source }) { sourceCheckDirty = false; return }
        sourceCheckBusy = true; sourceCheckDirty = false
        val epoch = generation; val identity = peerId
        fileWork({ cache.inventory() }, { actual ->
            if (epoch != generation || identity != peerId) return@fileWork
            sourceCheckBusy = false
            transfers.values.filter { transfer -> transfer.source && actual.none { PeerJson.same(it, transfer.media) } }
                .toList().forEach { fail(it, "来源缓存或票根引用已撤销") }
            if (sourceCheckDirty) checkSourceReferences()
        }, {
            if (epoch == generation && identity == peerId) {
                sourceCheckBusy = false
                transfers.values.filter { it.source }.toList().forEach { fail(it, "来源缓存状态无法确认") }
            }
        })
    }

    private fun inventory() {
        if (!ready() || inventoryBusy || !inventoryDirty) return
        inventoryBusy = true; inventoryDirty = false
        val epoch = generation; val identity = peerId
        fileWork({ cache.inventory() }, { items ->
            if (!ready() || epoch != generation || identity != peerId) return@fileWork
            require(items.size <= 1024 && items.all(PeerJson::valid) && items.map { it.assetId }.distinct().size == items.size)
            val actual = items.filter { it.assetId !in withdrawn }
            val identifiers = actual.map { it.assetId }.sorted()
            val wanted = identifiers.toSet()
            transfers.values.filter { it.source && it.media.assetId !in wanted }.toList()
                .forEach { fail(it, "来源缓存或票根引用已撤销") }
            val added = wanted - advertised; val removed = advertised - wanted
            if (!replaceRequired && added.isEmpty() && removed.isEmpty()) { inventoryBusy = false; return@fileWork }
            val revision = inventoryRevision + 1
            fun failed(code: String) {
                inventoryBusy = false; inventoryDirty = true; inventoryAt = now() + 1000
                if (code == "conflict") { replaceRequired = true; heartbeatAt = now() }
                error = errorMessage(code); dirty = true
            }
            if (!replaceRequired && inventoryRevision > 0 && added.size + removed.size <= 32) {
                rpc("cache.patch", obj("revision" to revision, "add" to JSONArray(added.sorted()), "remove" to JSONArray(removed.sorted())), "cache.ack",
                    success = { ack ->
                        require(PeerJson.number(ack, "revision") == revision && PeerJson.flag(ack, "complete") && PeerJson.number(ack, "count") == wanted.size.toLong())
                        advertised = wanted; inventoryRevision = revision; replaceRequired = false; inventoryBusy = false
                    }, failure = ::failed)
            } else {
                val batches = identifiers.chunked(32).ifEmpty { listOf(emptyList()) }
                fun batch(index: Int) {
                    rpc("cache.replace", obj("revision" to revision, "batchIndex" to index, "batchCount" to batches.size, "assetIds" to JSONArray(batches[index])), "cache.ack",
                        success = { ack ->
                            require(PeerJson.number(ack, "revision") == revision)
                            if (index == batches.lastIndex) {
                                require(PeerJson.flag(ack, "complete") && PeerJson.number(ack, "count") == wanted.size.toLong())
                                advertised = wanted; inventoryRevision = revision; replaceRequired = false; inventoryBusy = false
                            } else {
                                require(!PeerJson.flag(ack, "complete") && PeerJson.number(ack, "nextBatchIndex") == (index + 1).toLong())
                                batch(index + 1)
                            }
                        }, failure = ::failed)
                }
                batch(0)
            }
        }, { if (epoch == generation && identity == peerId) { inventoryBusy = false; inventoryDirty = true; inventoryAt = now() + 1000; error = "本机缓存清单暂不可用"; dirty = true } })
    }

    private fun open(request: Download) {
        if (!active(request)) { finish(request, null); return }
        if (!ready() || now() >= request.deadline) { finish(request, null); return }
        request.opening = true
        rpc("peer.open", obj("assetId" to request.media.assetId, "targetPeerId" to request.source), "peer.pending", request.id,
            success = { reply ->
                if (!active(request)) { finish(request, null); return@rpc }
                request.opening = false
                val id = PeerJson.token(reply, "transferId")
                val transfer = Transfer(id, request.media, false, request.source, request.deadline, request, now() + 5000)
                transfers[id] = transfer; installPeer(transfer)
                val channel = transfer.pc!!.createChannel("memoverse-$id"); attach(transfer, channel)
                val queued = early.filter { it.second.optString("transferId") == id }.map { it.second }
                early.removeAll { it.second.optString("transferId") == id }; earlyBytes = early.sumOf { it.second.toString().toByteArray(Charsets.UTF_8).size }
                queued.forEach { message(it) }; dirty = true
            }, failure = { request.opening = false; finish(request, null, errorMessage(it)) })
    }
    private fun sourceRequest(body: JSONObject) {
        val id = PeerJson.token(body, "transferId")
        if (!ready() || occupied() >= 2 || transfers.containsKey(id)) { send("peer.cancel", obj("transferId" to id, "reason" to "capacity")); return }
        val media = PeerJson.media(body.getJSONObject("media"))
        val transfer = Transfer(id, media, true, PeerJson.token(body, "requesterPeerId"), now() + 12000, null,
            now() + PeerJson.number(body, "leaseMs", 1, 5000))
        transfers[id] = transfer; installPeer(transfer); dirty = true
        fileWork({
            val reference = cache.inventory().firstOrNull { PeerJson.same(it, media) } ?: throw IllegalStateException()
            val bytes = cache.read(reference) ?: throw IllegalStateException()
            require(bytes.size.toLong() == media.size && PeerWire.sha256(bytes) == media.sha256 && PeerWire.matchesMime(bytes, media.mimeType))
            bytes
        }, { bytes ->
            if (!live(transfer)) return@fileWork
            transfer.sourceBytes = bytes
            rpc("peer.accept", obj("transferId" to id), "peer.authorized", id,
                success = { authorize(transfer, it) }, failure = { fail(transfer, errorMessage(it)) })
        }, { if (live(transfer)) {
            withdrawn.add(transfer.media.assetId); inventoryDirty = true; inventoryAt = now()
            fail(transfer, "来源缓存不存在或校验失败")
        } })
    }
    private fun live(transfer: Transfer): Boolean {
        if (transfer.closed || transfers[transfer.id] !== transfer || !ready() || transfer.download?.aborted?.get() == true) return false
        if (transfer.source && !runCatching { cache.canShare(transfer.media) }.getOrDefault(false)) {
            withdrawn.add(transfer.media.assetId); inventoryDirty = true; inventoryAt = now()
            fail(transfer, "来源缓存或票根引用已撤销")
            return false
        }
        return true
    }
    private fun installPeer(transfer: Transfer) {
        transfer.progressAt = now()
        transfer.pc = rtc.create(object : RtcPeer.Listener {
            override fun ice(ice: RtcIce?) = post {
                if (live(transfer)) {
                    if (transfer.localSet) queueSignal(transfer, "ice", PeerJson.ice(ice)) else {
                        if (transfer.localIce.size >= 128) fail(transfer, "ICE 候选超限") else transfer.localIce.addLast(ice)
                    }
                }
            }
            override fun channel(channel: RtcChannel) = post {
                if (!live(transfer) || !transfer.source || transfer.channel != null || channel.label != "memoverse-${transfer.id}") { channel.close(); if (live(transfer)) fail(transfer, "设备通道配置无效") }
                else attach(transfer, channel)
            }
            override fun failed() = post {
                if (live(transfer)) { if (transfer.verified) completeWhenFlushed(transfer) else fail(transfer, "设备连接中断", true) }
            }
        })
    }
    private fun authorize(transfer: Transfer, body: JSONObject) {
        if (!live(transfer)) return
        require(PeerJson.token(body, "transferId") == transfer.id && PeerJson.same(PeerJson.media(body.getJSONObject("media")), transfer.media))
        require(PeerJson.token(body, "sourcePeerId") == if (transfer.source) peerId else transfer.other)
        require(PeerJson.token(body, "requesterPeerId") == if (transfer.source) transfer.other else peerId)
        require(PeerJson.token(body, "offererPeerId") == if (transfer.source) transfer.other else peerId)
        require(!transfer.authorized)
        val lease = PeerJson.number(body, "leaseMs", 1, 10000)
        transfer.authorized = true; transfer.leaseUntil = now() + lease; transfer.renewAt = now() + minOf(5000, lease / 2)
        transfer.connectUntil = minOf(transfer.deadline, now() + 4000)
        if (!transfer.source) localDescription(transfer, "offer")
    }
    private fun localDescription(transfer: Transfer, type: String) {
        transfer.pc!!.createLocal(type) { result -> post {
            if (!live(transfer)) return@post
            result.fold(onSuccess = { description ->
                require(description.type == type && description.sdp.toByteArray(Charsets.UTF_8).size in 1..49152)
                queueSignal(transfer, type, obj("type" to type, "sdp" to description.sdp))
                transfer.localSet = true
                while (transfer.localIce.isNotEmpty() && live(transfer)) queueSignal(transfer, "ice", PeerJson.ice(transfer.localIce.removeFirst()))
            }, onFailure = { fail(transfer, "设备会话描述不可用") })
        } }
    }
    private fun queueSignal(transfer: Transfer, kind: String, payload: Any) {
        if (!live(transfer)) return
        val bytes = payload.toString().toByteArray(Charsets.UTF_8).size
        transfer.signalBytes += bytes
        if (kind == "ice") transfer.candidates++
        if (transfer.signalBytes > 512 * 1024 || transfer.candidates > 128 || kind == "ice" && bytes > 2048) { fail(transfer, "设备信令预算超限"); return }
        transfer.signals.addLast(kind to payload); nextSignal(transfer)
    }
    private fun nextSignal(transfer: Transfer) {
        if (!live(transfer) || transfer.signalling || transfer.signals.isEmpty()) return
        val (kind, payload) = transfer.signals.removeFirst(); transfer.signalling = true
        rpc("peer.signal", obj("transferId" to transfer.id, "kind" to kind, "payload" to payload), "peer.signalled", transfer.id,
            success = { require(PeerJson.token(it, "transferId") == transfer.id); transfer.signalling = false; nextSignal(transfer) },
            failure = { fail(transfer, errorMessage(it)) })
    }
    private fun signal(transfer: Transfer, body: JSONObject) {
        require(transfer.authorized && PeerJson.token(body, "fromPeerId") == transfer.other)
        val kind = PeerJson.text(body, "kind", 10)
        val payload = body.opt("payload")
        val bytes = (payload?.toString() ?: "null").toByteArray(Charsets.UTF_8).size
        transfer.signalBytes += bytes; require(transfer.signalBytes <= 512 * 1024)
        if (kind == "ice") {
            transfer.candidates++; require(transfer.candidates <= 128)
            val ice = PeerJson.ice(payload)
            if (transfer.remoteSet) transfer.pc!!.addIce(ice) else transfer.remoteIce.addLast(ice)
            return
        }
        require(kind == if (transfer.source) "offer" else "answer")
        require(payload is JSONObject && !transfer.remoteSet && !transfer.settingRemote)
        require(PeerJson.text(payload, "type", 10) == kind)
        val sdp = PeerJson.text(payload, "sdp", 49152)
        transfer.frameLimit = PeerWire.remoteFrameLimit(sdp); transfer.settingRemote = true
        transfer.pc!!.setRemote(RtcDescription(kind, sdp)) { success -> post {
            if (!live(transfer)) return@post
            if (!success) { fail(transfer, "远端会话描述无效"); return@post }
            transfer.settingRemote = false; transfer.remoteSet = true
            while (transfer.remoteIce.isNotEmpty()) transfer.pc!!.addIce(transfer.remoteIce.removeFirst())
            if (transfer.source) localDescription(transfer, "answer")
        } }
    }

    private fun attach(transfer: Transfer, channel: RtcChannel) {
        transfer.channel = channel
        channel.observe(object : RtcChannel.Listener {
            override fun opened() = post { if (live(transfer)) channelOpened(transfer) }
            override fun received(bytes: ByteArray, binary: Boolean) = post {
                if (live(transfer)) try { receive(transfer, bytes, binary) } catch (_: Exception) { fail(transfer, "设备分块或确认消息无效") }
            }
            override fun bufferedAmountChanged() = post {
                if (live(transfer)) { if (transfer.source) pump(transfer) else if (transfer.verified) completeWhenFlushed(transfer) }
            }
            override fun closed() = post {
                if (live(transfer)) { if (transfer.verified) completeWhenFlushed(transfer) else fail(transfer, "设备传输中断", true) }
            }
        })
        if (channel.open) channelOpened(transfer)
    }
    private fun channelOpened(transfer: Transfer) {
        if (transfer.channelOpened) return
        transfer.channelOpened = true
        transfer.connectUntil = Long.MAX_VALUE; transfer.progressAt = now()
        if (!transfer.source) {
            val prefix = transfer.download!!.prefix
            control(transfer, obj("v" to 1, "type" to "get", "assetId" to transfer.media.assetId, "offset" to prefix.offset))
        }
    }
    private fun control(transfer: Transfer, value: JSONObject) {
        val bytes = value.toString().toByteArray(Charsets.UTF_8)
        require(bytes.size <= 1024 && transfer.channel?.open == true && transfer.channel!!.send(bytes, false))
    }
    private fun receive(transfer: Transfer, bytes: ByteArray, binary: Boolean) {
        require(transfer.authorized)
        if (binary) {
            require(!transfer.source && !transfer.verifying && transfer.verifiedBytes == null)
            val prefix = transfer.download!!.prefix
            prefix.append(bytes, transfer.frameLimit); transfer.payloadBytes += bytes.size - 8; transfer.progressAt = now(); dirty = true
            control(transfer, obj("v" to 1, "type" to "ack", "offset" to prefix.offset)); return
        }
        val value = PeerJson.control(bytes)
        when (PeerJson.text(value, "type", 20)) {
            "get" -> {
                require(transfer.source && transfer.sending == null && transfer.sourceBytes != null)
                PeerJson.keys(value, setOf("v", "type", "assetId", "offset"))
                require(PeerJson.text(value, "assetId", 64) == transfer.media.assetId)
                transfer.sending = SendProgress(transfer.media.size.toInt(), PeerJson.number(value, "offset", 0, transfer.media.size).toInt())
                transfer.progressAt = now(); pump(transfer)
            }
            "ack" -> {
                require(transfer.source); PeerJson.keys(value, setOf("v", "type", "offset"))
                transfer.sending!!.acknowledge(PeerJson.number(value, "offset", 0, transfer.media.size).toInt()); transfer.progressAt = now(); pump(transfer)
            }
            "end" -> {
                require(!transfer.source && !transfer.verifying && transfer.verifiedBytes == null)
                PeerJson.keys(value, setOf("v", "type", "size")); require(PeerJson.number(value, "size") == transfer.media.size)
                val data = transfer.download!!.prefix.complete(); transfer.verifying = true; transfer.progressAt = now()
                fileWork({ require(PeerWire.sha256(data) == transfer.media.sha256 && PeerWire.matchesMime(data, transfer.media.mimeType)); data }, { verified ->
                    if (!live(transfer)) return@fileWork
                    transfer.verifying = false; transfer.verifiedBytes = verified; transfer.verified = true
                    control(transfer, obj("v" to 1, "type" to "verified", "size" to transfer.media.size))
                    completionWindow(transfer)
                    route(transfer) { if (live(transfer)) completeWhenFlushed(transfer) }
                }, { if (live(transfer)) fail(transfer, "设备图片完整性校验失败") })
            }
            "verified" -> {
                require(transfer.source && transfer.ended)
                PeerJson.keys(value, setOf("v", "type", "size"))
                require(transfer.sending!!.verified(PeerJson.number(value, "size", 1, transfer.media.size).toInt()))
                transfer.verified = true; transfer.progressAt = now(); completionWindow(transfer)
                route(transfer) { if (live(transfer)) complete(transfer) }
            }
            else -> throw IllegalArgumentException("Unknown peer control")
        }
    }
    private fun pump(transfer: Transfer) {
        val channel = transfer.channel ?: return
        val progress = transfer.sending ?: return
        val bytes = transfer.sourceBytes ?: return
        if (!live(transfer) || !channel.open || transfer.ended) return
        var batches = 0
        while (progress.sent < bytes.size && batches++ < 16) {
            val count = minOf(bytes.size - progress.sent, transfer.frameLimit - 8)
            if (!transfer.backpressure.canSend(channel.bufferedAmount, count + 8)) return
            if (!channel.send(PeerWire.encode(progress.sent, bytes, progress.sent, count), true)) { fail(transfer, "设备通道发送失败", true); return }
            progress.sent(count); transfer.payloadBytes += count; dirty = true
        }
        if (progress.sent == bytes.size) { transfer.ended = true; control(transfer, obj("v" to 1, "type" to "end", "size" to bytes.size)) }
        else serial.execute { if (live(transfer)) guarded { pump(transfer) } }
    }
    private fun route(transfer: Transfer, finished: () -> Unit) {
        if (transfer.routePending) return
        transfer.routePending = true
        transfer.pc!!.route { route -> post {
            if (live(transfer)) {
                transfer.routePending = false
                if (route in setOf("direct", "relay")) transfer.route = if (transfer.route in setOf("unknown", route)) route else "mixed"
                finished()
            }
        } }
    }
    private fun complete(transfer: Transfer) {
        if (transfer.closed) return
        val bytes = transfer.verifiedBytes
        if (!transfer.source && bytes == null) { fail(transfer, "设备文件尚未完成校验"); return }
        detach(transfer, "complete", "", true)
        transfer.download?.let { finish(it, PeerDownload(bytes!!, transfer.route, it.resumedBytes)) }
    }
    private fun completeWhenFlushed(transfer: Transfer) {
        if (!live(transfer) || !transfer.verified) return
        if (transfer.source) { complete(transfer); return }
        val elapsed = now() >= transfer.completionUntil
        // send(true) only accepts the verified frame into the native buffer. Do
        // not tear down that SCTP association until it drains and the source
        // confirms completion, or the bounded acknowledgement grace elapses.
        if (transfer.channel?.bufferedAmount != 0L) {
            if (elapsed) fail(transfer, "设备校验确认未能发送完成")
            return
        }
        if (transfer.remoteComplete || elapsed) complete(transfer)
    }
    private fun completionWindow(transfer: Transfer) {
        transfer.completionUntil = minOf(transfer.deadline, now() + 250)
        transfer.completionTask = serial.schedule({ guarded { completeWhenFlushed(transfer) } },
            (transfer.completionUntil - now()).coerceAtLeast(0), TimeUnit.MILLISECONDS)
    }
    private fun fail(transfer: Transfer, reason: String, retryable: Boolean = false) {
        if (transfer.closed) return
        detach(transfer, "failed", reason, true)
        val request = transfer.download ?: return
        if (retryable && ready() && !request.cancelled() && request.prefix.tryResume(now(), request.deadline)) {
            request.resumedBytes = request.prefix.offset.toLong(); open(request)
        } else finish(request, null, reason)
    }
    private fun detach(transfer: Transfer, status: String, reason: String, notify: Boolean) {
        if (transfer.closed) return
        transfer.closed = true; transfers.remove(transfer.id)
        transfer.completionTask?.cancel(false)
        pending.entries.removeAll { it.value.owner == transfer.id }
        if (notify) send("peer.cancel", obj("transferId" to transfer.id, "reason" to if (status == "complete") "complete" else "cancelled"))
        try { transfer.channel?.close() } catch (_: Exception) { }
        try { transfer.pc?.close() } catch (_: Exception) { }
        transfer.sourceBytes = null; transfer.signals.clear(); transfer.localIce.clear(); transfer.remoteIce.clear()
        recent.addFirst(TransferView(transfer.media.assetId, if (transfer.source) "send" else "receive", status, transfer.route, transfer.payloadBytes, reason))
        while (recent.size > 50) recent.removeLast()
        if (reason.isNotEmpty()) error = reason
        dirty = true
    }
    private fun finish(request: Download, result: PeerDownload?, reason: String = "", cancel: Boolean = false) {
        request.aborted.set(true)
        transfers.values.filter { it.download === request }.toList().forEach { detach(it, "cancelled", reason, true) }
        downloads.remove(request.id); pending.entries.removeAll { it.value.owner == request.id }
        request.clearPrefix()
        if (reason.isNotEmpty()) error = reason
        if (cancel || closed.get() || request.cancelled()) request.result.completeExceptionally(CancellationException("设备读取已取消"))
        else request.result.complete(result)
        dirty = true
    }
    private fun reset(reason: String, cancel: Boolean) {
        pending.clear(); registering = false
        transfers.values.toList().forEach { detach(it, "cancelled", reason, false) }
        downloads.values.toList().forEach { finish(it, null, reason, cancel) }
        peerId = ""; peerUntil = 0; advertised = emptySet(); inventoryRevision = 0; replaceRequired = true; withdrawn.clear()
        sourceCheckBusy = false; sourceCheckDirty = false
        inventoryBusy = false; inventoryDirty = true; inventoryAt = now(); registerAt = now() + 2000
        devices = emptyList(); early.clear(); earlyBytes = 0; error = reason; dirty = true
    }
    private fun <T> fileWork(work: () -> T, success: (T) -> Unit, failure: () -> Unit) {
        try { io.execute { val result = runCatching(work); post { result.fold(success, { failure() }) } } }
        catch (_: RejectedExecutionException) { failure() }
    }
    private fun tick() {
        if (closed.get()) return
        connectionChanged()
        val instant = now()
        pending.filterValues { it.deadline <= instant }.toList().forEach { (id, waiting) -> if (pending.remove(id) === waiting) waiting.failure("expired") }
        downloads.values.toList().forEach { if (it.cancelled()) finish(it, null, cancel = true) else if (instant >= it.deadline - 100) finish(it, null, "设备读取已超时，已改用云端") }
        transfers.values.toList().forEach { transfer ->
            if (!live(transfer)) return@forEach
            when {
                instant >= transfer.completionUntil -> completeWhenFlushed(transfer)
                instant >= transfer.deadline || instant >= transfer.leaseUntil -> fail(transfer, "设备授权或读取预算已到期")
                instant >= transfer.connectUntil -> fail(transfer, "设备连接超时", true)
                transfer.channel?.open == true && instant - transfer.progressAt > PeerWire.STALL_MS -> fail(transfer, "设备传输停滞", true)
                transfer.authorized && !transfer.renewing && instant >= transfer.renewAt -> {
                    transfer.renewing = true
                    rpc("peer.renew", obj("transferId" to transfer.id), "peer.lease", transfer.id,
                        success = { reply ->
                            require(PeerJson.token(reply, "transferId") == transfer.id)
                            val lease = PeerJson.number(reply, "leaseMs", 1, 10000)
                            transfer.renewing = false; transfer.leaseUntil = now() + lease; transfer.renewAt = now() + minOf(5000, lease / 2)
                        }, failure = { fail(transfer, errorMessage(it)) })
                }
            }
        }
        while (early.isNotEmpty() && instant - early.first().first > 5000) { earlyBytes -= early.removeFirst().second.toString().toByteArray(Charsets.UTF_8).size }
        if (ready()) {
            if (instant >= heartbeatAt) {
                heartbeatAt = instant + 15000; val revision = inventoryRevision
                rpc("device.heartbeat", JSONObject(), "device.alive", success = { reply ->
                    require(PeerJson.number(reply, "leaseMs", 45000, 45000) == 45000L); peerUntil = now() + 45000
                    val remote = PeerJson.number(reply, "inventoryRevision")
                    if (!inventoryBusy && inventoryRevision == revision && remote != revision) { inventoryRevision = remote; advertised = emptySet(); replaceRequired = true; inventoryDirty = true; inventoryAt = now() }
                }, failure = { reset(errorMessage(it), false) })
            }
            if (instant >= listAt) {
                listAt = instant + 15000
                rpc("device.list", JSONObject(), "device.peers", success = { reply ->
                    val peers = PeerJson.array(reply, "peers", 8)
                    devices = (0 until peers.length()).map { index -> peers.getJSONObject(index).let { DeviceView(PeerJson.token(it, "peerId"), PeerJson.text(it, "name", 64), null, null) } }
                    dirty = true
                }, failure = { error = errorMessage(it); dirty = true })
            }
            if (instant >= inventoryAt) inventory()
        } else if (peerId.isNotEmpty()) reset("设备在线租期已到期", false)
        if (dirty) publish()
    }
    private fun publish() {
        dirty = false
        val active = transfers.values.map { TransferView(it.media.assetId, if (it.source) "send" else "receive", "active", it.route, it.payloadBytes) }
        val view = PeerView(enabled, ready(), peerId, devices, (active + recent).take(50), transfers.values.count { it.source }, downloads.size, error)
        try { onChanged(view) } catch (_: Exception) { }
    }
    override fun close() {
        if (!closed.compareAndSet(false, true)) return
        subscriptions.forEach { try { it.close() } catch (_: Exception) { } }
        ticking.cancel(false)
        try { serial.execute {
            try { send("device.unregister"); reset("", true); rtc.close() }
            finally { io.shutdownNow(); closeDone.complete(Unit); serial.shutdown() }
        } } catch (_: RejectedExecutionException) { closeDone.complete(Unit) }
    }

    /** Verification only; UI lifecycle close() never waits on native work. */
    internal fun awaitClosed(timeoutMs: Long = 3000): Boolean = try { closeDone.get(timeoutMs, TimeUnit.MILLISECONDS); true } catch (_: Exception) { false }
}
