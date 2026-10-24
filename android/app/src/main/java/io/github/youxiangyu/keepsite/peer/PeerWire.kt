package io.github.youxiangyu.keepsite.peer

import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest

/** Web v1 framing: no sequence number or metadata frame; offsets are unsigned big endian. */
internal object PeerWire {
    const val FRAME_BYTES = 16 * 1024
    const val HEADER_BYTES = 8
    const val BUFFER_HIGH = 256 * 1024L
    const val BUFFER_LOW = 64 * 1024L
    const val MAX_IMAGE_BYTES = 12 * 1024 * 1024
    const val MAX_CONTROL_BYTES = 1024
    const val ATTEMPT_MS = 12_000L
    const val CONNECTION_MS = 4_000L
    const val STALL_MS = 2_000L
    private val hash = Regex("[a-f0-9]{64}")
    private val token = Regex("[A-Za-z0-9_-]{1,64}")

    fun isHash(value: String) = hash.matches(value)
    fun isToken(value: String) = token.matches(value)
    fun mime(value: String) = value in setOf("image/jpeg", "image/png", "image/webp")
    fun sha256(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(bytes)
        .joinToString("") { "%02x".format(it.toInt() and 255) }
    fun matchesMime(bytes: ByteArray, mime: String): Boolean = when (mime) {
        "image/png" -> bytes.size >= 8 && bytes.copyOfRange(0, 8).contentEquals(byteArrayOf(-119, 80, 78, 71, 13, 10, 26, 10))
        "image/jpeg" -> bytes.size >= 3 && bytes[0] == (-1).toByte() && bytes[1] == (-40).toByte() && bytes[2] == (-1).toByte()
        "image/webp" -> bytes.size >= 12 && bytes.copyOfRange(0, 4).contentEquals("RIFF".toByteArray(Charsets.US_ASCII))
            && bytes.copyOfRange(8, 12).contentEquals("WEBP".toByteArray(Charsets.US_ASCII))
        else -> false
    }

    fun encode(offset: Int, payload: ByteArray, start: Int = 0, length: Int = payload.size - start): ByteArray {
        require(offset >= 0 && start >= 0 && length > 0 && start <= payload.size - length)
        require(length <= FRAME_BYTES - HEADER_BYTES && offset.toLong() + length <= MAX_IMAGE_BYTES)
        return ByteBuffer.allocate(HEADER_BYTES + length).order(ByteOrder.BIG_ENDIAN)
            .putInt(offset).putInt(length).put(payload, start, length).array()
    }

    fun decode(frame: ByteArray, expectedOffset: Int, totalSize: Int, frameLimit: Int = FRAME_BYTES): ByteArray {
        require(frameLimit in HEADER_BYTES + 1..FRAME_BYTES)
        require(totalSize in 1..MAX_IMAGE_BYTES && expectedOffset in 0..totalSize)
        require(frame.size in HEADER_BYTES + 1..frameLimit) { "Invalid data frame size" }
        val buffer = ByteBuffer.wrap(frame).order(ByteOrder.BIG_ENDIAN)
        val offset = buffer.int.toLong() and 0xffffffffL
        val length = buffer.int.toLong() and 0xffffffffL
        require(offset == expectedOffset.toLong() && length == frame.size.toLong() - HEADER_BYTES
            && offset + length <= totalSize) { "Non-contiguous data frame" }
        return frame.copyOfRange(HEADER_BYTES, frame.size)
    }

    /** RFC 8841, scoped to the active SCTP application m-section. Missing=64 KiB, zero=unlimited. */
    fun remoteFrameLimit(sdp: String): Int {
        require(sdp.toByteArray(Charsets.UTF_8).size in 1..49152)
        var application = false
        var activeSections = 0
        var maximum: Long? = null
        for (raw in sdp.lineSequence()) {
            val line = raw.trimEnd('\r')
            if (line.startsWith("m=")) {
                val fields = line.substring(2).trim().split(Regex("\\s+"))
                application = fields.size >= 4 && fields[0] == "application" && fields[1] != "0"
                    && fields[2] in setOf("UDP/DTLS/SCTP", "TCP/DTLS/SCTP", "DTLS/SCTP")
                if (application) activeSections++
            } else if (application && line.startsWith("a=max-message-size:")) {
                require(maximum == null) { "Duplicate SCTP message limit" }
                val text = line.substringAfter(':')
                require(Regex("0|[1-9][0-9]*").matches(text)) { "Invalid SCTP message limit" }
                maximum = text.toLongOrNull() ?: throw IllegalArgumentException("SCTP message limit overflow")
            }
        }
        require(activeSections == 1) { "Expected one active SCTP application" }
        val peerMaximum = maximum ?: 65536L
        val limit = if (peerMaximum == 0L) FRAME_BYTES else minOf(FRAME_BYTES.toLong(), peerMaximum).toInt()
        require(limit > HEADER_BYTES) { "SCTP message limit cannot hold a chunk" }
        return limit
    }
}

/** Mutable only on the peer executor. A retry cannot extend the original request budget. */
internal class ReceivePrefix(val size: Int) {
    private val bytes = ByteArray(size.also { require(it in 1..PeerWire.MAX_IMAGE_BYTES) })
    var offset: Int = 0
        private set
    var resumed: Boolean = false
        private set

    fun append(frame: ByteArray, limit: Int = PeerWire.FRAME_BYTES) {
        val payload = PeerWire.decode(frame, offset, size, limit)
        payload.copyInto(bytes, offset)
        offset += payload.size
    }

    fun tryResume(now: Long, deadline: Long): Boolean {
        if (resumed || offset == 0 || now >= deadline) return false
        resumed = true
        return true
    }

    fun complete(): ByteArray { check(offset == size); return bytes.copyOf() }
    fun clear() { bytes.fill(0); offset = 0 }
}

internal class SendProgress(val size: Int, start: Int) {
    var sent = start
        private set
    var acknowledged = start
        private set
    init { require(size in 1..PeerWire.MAX_IMAGE_BYTES && start in 0..size) }
    fun sent(count: Int) { require(count > 0 && sent.toLong() + count <= size); sent += count }
    fun acknowledge(offset: Int) { require(offset > acknowledged && offset <= sent); acknowledged = offset }
    fun verified(size: Int): Boolean = size == this.size && sent == size && acknowledged == size
}

/** Native callbacks report every buffer change; resume only at the Web protocol's low watermark. */
internal class SendBackpressure {
    private var paused = false
    fun canSend(buffered: Long, nextFrameBytes: Int): Boolean {
        require(buffered >= 0 && nextFrameBytes in PeerWire.HEADER_BYTES + 1..PeerWire.FRAME_BYTES)
        if (paused && buffered > PeerWire.BUFFER_LOW) return false
        if (buffered > PeerWire.BUFFER_HIGH - nextFrameBytes) { paused = true; return false }
        paused = false
        return true
    }
}
