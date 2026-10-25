package io.github.youxiangyu.keepsite.core

import org.junit.Assert.*
import org.junit.Test
import java.io.Closeable
import java.util.concurrent.CancellationException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

class PhotoWorkTest {
    private fun eventually(work: PhotoWork): Int {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(3)
        while (true) {
            try { return work.execute { 7 } }
            catch (failure: ApiFailure) { if (System.nanoTime() >= deadline) throw failure; Thread.sleep(5) }
        }
    }
    private fun uninterruptible(latch: CountDownLatch) {
        while (latch.count > 0) try { latch.await() } catch (_: InterruptedException) { }
    }
    @Test fun timeoutClosesProviderAndAllowsAnotherSelection() {
        val released = CountDownLatch(1)
        val closes = AtomicInteger()
        PhotoWork(100).use { work ->
            val failure = assertThrows(ApiFailure::class.java) {
                work.execute { scope ->
                    scope.own(Closeable { closes.incrementAndGet(); released.countDown() })
                    uninterruptible(released)
                    scope.check()
                }
            }
            assertTrue(failure.message!!.contains("超时"))
            assertTrue(released.await(3, TimeUnit.SECONDS))
            assertEquals(7, eventually(work))
            assertEquals(1, closes.get())
        }
    }
    @Test fun blockingCloseDoesNotHoldTheTimeoutCallerOrCreateReplacementThreads() {
        val allowClose = CountDownLatch(1)
        val closing = CountDownLatch(1)
        val work = PhotoWork(100)
        try {
            assertThrows(ApiFailure::class.java) {
                work.execute { scope ->
                    scope.own(Closeable { closing.countDown(); uninterruptible(allowClose) })
                    uninterruptible(allowClose)
                }
            }
            assertTrue(closing.await(3, TimeUnit.SECONDS))
            repeat(10) {
                val failure = assertThrows(ApiFailure::class.java) { work.execute { fail("Must not start a replacement task") } }
                assertTrue(failure.message!!.contains("上一张"))
            }
            work.cancel() // Repeated lifecycle cancellation is also non-blocking.
        } finally { allowClose.countDown(); work.close() }
    }
    @Test fun descriptorReturnedAfterCancellationIsClosedAndCannotReturnAStalePhoto() {
        val opening = CountDownLatch(1)
        val opened = CountDownLatch(1)
        val closed = CountDownLatch(1)
        val caller = Executors.newSingleThreadExecutor()
        val work = PhotoWork(5_000)
        try {
            val result = caller.submit<Int> {
                work.execute { scope ->
                    opening.countDown(); uninterruptible(opened)
                    scope.own(Closeable { closed.countDown() })
                    5
                }
            }
            assertTrue(opening.await(3, TimeUnit.SECONDS))
            work.cancel()
            assertThrows(java.util.concurrent.ExecutionException::class.java) { result.get(3, TimeUnit.SECONDS) }
            opened.countDown()
            assertTrue(closed.await(3, TimeUnit.SECONDS))
            assertEquals(7, eventually(work))
        } finally { opened.countDown(); work.close(); caller.shutdownNow() }
    }
    @Test fun failuresReleaseResourcesAndClosedOwnerCannotStartMoreWork() {
        val count = AtomicInteger()
        val work = PhotoWork()
        val expected = ApiFailure(message = "Invalid image")
        val actual = assertThrows(ApiFailure::class.java) {
            work.execute { scope -> scope.own(Closeable { count.incrementAndGet() }); throw expected }
        }
        assertSame(expected, actual)
        assertEquals(7, eventually(work)); assertEquals(1, count.get())
        work.close()
        assertThrows(CancellationException::class.java) { work.execute { 9 } }
    }
    @Test fun rapidSelectionsDoNotLoseTheSingleWorkerHandoff() {
        PhotoWork().use { work -> repeat(100) { assertEquals(7, eventually(work)) } }
    }
    @Test fun lifecycleCancellationAndCloseCanRaceWithTaskCompletion() {
        val callers = Executors.newFixedThreadPool(2)
        try {
            repeat(30) {
                val work = PhotoWork(5_000)
                val started = CountDownLatch(1)
                val finish = CountDownLatch(1)
                try {
                    val task = callers.submit {
                        try { work.execute { started.countDown(); uninterruptible(finish); 5 } }
                        catch (_: CancellationException) { }
                    }
                    assertTrue(started.await(3, TimeUnit.SECONDS))
                    val cancellation = callers.submit { finish.countDown(); work.cancel(); work.close(); work.cancel() }
                    cancellation.get(3, TimeUnit.SECONDS)
                    task.get(3, TimeUnit.SECONDS)
                } finally { finish.countDown(); work.close() }
            }
        } finally { callers.shutdownNow() }
    }
    @Test fun sourceAndConvertedOutputLimitsAreEnforcedWhileWriting() {
        val bytes = PhotoBytes()
        bytes.write(ByteArray(MAX_IMAGE_BYTES.toInt()))
        assertThrows(ApiFailure::class.java) { bytes.write(1) }
        assertEquals(MAX_IMAGE_BYTES.toInt(), bytes.size())
    }
    @Test fun decodeBudgetIsAppliedBeforeAllocationAndNeverUpscales() {
        assertEquals(64 to 32, PhotoCodec.targetSize(64, 32))
        for ((width, height) in listOf(8000 to 5000, 5000 to 8000, 6000 to 6000, 40_000_000 to 1)) {
            val size = PhotoCodec.targetSize(width, height)
            assertTrue(size.first <= PhotoCodec.MAX_EDGE && size.second <= PhotoCodec.MAX_EDGE)
            assertTrue(size.first.toLong() * size.second <= PhotoCodec.MAX_DECODE_PIXELS)
        }
        assertThrows(ApiFailure::class.java) { PhotoCodec.targetSize(8000, 6000) }
        assertThrows(ApiFailure::class.java) { PhotoCodec.targetSize(0, 6) }
    }
    @Test fun heifContainerSniffingDoesNotTrustAFileExtensionOrAnAvifBrand() {
        fun box(brand: String) = java.nio.ByteBuffer.allocate(20).putInt(20).put("ftyp".toByteArray())
            .put(brand.toByteArray()).putInt(0).put(brand.toByteArray()).array()
        assertTrue(PhotoCodec.isHeif(box("heic")))
        assertTrue(PhotoCodec.isHeif(box("heix")))
        assertFalse(PhotoCodec.isHeif(box("avif")))
        assertFalse(PhotoCodec.isHeif(box("heic").copyOf(12)))
        assertFalse(PhotoCodec.isHeif(byteArrayOf(1, 2, 3)))
    }
}
