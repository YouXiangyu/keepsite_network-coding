package io.github.youxiangyu.memoverse.peer

import org.junit.Assert.*
import org.junit.Test

class PeerWireTest {
    private fun rejects(action: () -> Unit) {
        try { action(); fail("Expected malformed peer data to be rejected") } catch (_: IllegalArgumentException) { }
    }

    @Test fun headerMatchesWebBigEndianVectorAndRejectsWrongOffset() {
        val frame = PeerWire.encode(0x010203, byteArrayOf(0x7f, 0x11))
        assertArrayEquals(byteArrayOf(0, 1, 2, 3, 0, 0, 0, 2, 0x7f, 0x11), frame)
        assertArrayEquals(byteArrayOf(0x7f, 0x11), PeerWire.decode(frame, 0x010203, 0x010205))
        rejects { PeerWire.decode(frame, 0, 0x010205) }
        rejects { PeerWire.decode(frame.copyOf().also { it[7] = 3 }, 0x010203, 0x010205) }
        rejects { PeerWire.decode(frame.copyOf().also { it[0] = -1 }, 0x010203, 0x010205) }
    }

    @Test fun sixteenKIncludesTheHeaderAndBoundsTheWholeFile() {
        val payload = ByteArray(PeerWire.FRAME_BYTES - PeerWire.HEADER_BYTES) { (it % 251).toByte() }
        val frame = PeerWire.encode(0, payload)
        assertEquals(16 * 1024, frame.size)
        assertArrayEquals(payload, PeerWire.decode(frame, 0, payload.size))
        rejects { PeerWire.encode(0, ByteArray(16 * 1024)) }
        rejects { PeerWire.encode(PeerWire.MAX_IMAGE_BYTES, byteArrayOf(1)) }
        rejects { PeerWire.decode(frame, 0, payload.size, 1024) }
    }

    @Test fun sctpLimitIsScopedAndNeverExpandsTheProjectFrame() {
        val base = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=max-message-size:1\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
        assertEquals(16384, PeerWire.remoteFrameLimit(base))
        assertEquals(16384, PeerWire.remoteFrameLimit(base + "a=max-message-size:0\r\n"))
        assertEquals(1024, PeerWire.remoteFrameLimit(base + "a=max-message-size:1024\r\n"))
        rejects { PeerWire.remoteFrameLimit(base + "a=max-message-size:8\r\n") }
        rejects { PeerWire.remoteFrameLimit(base + "a=max-message-size:3.0\r\n") }
        rejects { PeerWire.remoteFrameLimit(base + "a=max-message-size:12\r\na=max-message-size:12\r\n") }
        rejects { PeerWire.remoteFrameLimit(base.replace("application 9", "application 0")) }
    }

    @Test fun resumeRetainsOnlyOneContiguousPrefixWithinTheOriginalDeadline() {
        val prefix = ReceivePrefix(4)
        assertFalse(prefix.tryResume(0, 12000))
        prefix.append(PeerWire.encode(0, byteArrayOf(1, 2)))
        assertFalse(prefix.tryResume(12000, 12000))
        assertTrue(prefix.tryResume(1000, 12000))
        assertFalse(prefix.tryResume(1001, 12000))
        rejects { prefix.append(PeerWire.encode(1, byteArrayOf(3, 4))) }
        prefix.append(PeerWire.encode(2, byteArrayOf(3, 4)))
        assertArrayEquals(byteArrayOf(1, 2, 3, 4), prefix.complete())
        prefix.clear(); assertEquals(0, prefix.offset)
    }

    @Test fun senderAcceptsOnlyMonotonicAcksForActuallyAcceptedBytes() {
        val progress = SendProgress(5, 2)
        rejects { progress.acknowledge(3) }
        progress.sent(2); progress.acknowledge(4)
        rejects { progress.acknowledge(4) }
        assertFalse(progress.verified(5))
        progress.sent(1); assertFalse(progress.verified(5))
        progress.acknowledge(5); assertTrue(progress.verified(5))
        assertTrue(SendProgress(5, 5).verified(5))
    }

    @Test fun bufferWaitUsesLowWatermarkBeforeResumingAfterHighWatermark() {
        val gate = SendBackpressure()
        assertTrue(gate.canSend(PeerWire.BUFFER_HIGH - PeerWire.FRAME_BYTES, PeerWire.FRAME_BYTES))
        assertFalse(gate.canSend(PeerWire.BUFFER_HIGH - PeerWire.FRAME_BYTES + 1, PeerWire.FRAME_BYTES))
        assertFalse("Enough room for one frame does not release a paused sender", gate.canSend(PeerWire.BUFFER_LOW + 1, PeerWire.FRAME_BYTES))
        assertTrue(gate.canSend(PeerWire.BUFFER_LOW, PeerWire.FRAME_BYTES))
        assertTrue(gate.canSend(PeerWire.BUFFER_LOW + 1, PeerWire.FRAME_BYTES))
        rejects { gate.canSend(-1, PeerWire.FRAME_BYTES) }
        rejects { gate.canSend(0, PeerWire.FRAME_BYTES + 1) }
    }
}
