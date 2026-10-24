package io.github.youxiangyu.keepsite.core

import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.util.concurrent.CancellationException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

class FileCacheTest {
    @get:Rule val temporary = TemporaryFolder()
    private val bytes = byteArrayOf(1, 2, 3, 4)
    private val ref = MediaRef(sha256(bytes), sha256(bytes), 4, "image/png", 1, 1)
    private fun cache(directory: File, budget: Long = 8) = FileCache(directory, budget) { raw, media ->
        require(raw.size.toLong() == media.size && sha256(raw) == media.sha256)
    }
    @Test fun privateAccountCacheOnlyPublishesVerifiedLiveBytes() {
        val first = cache(temporary.newFolder("alice")); val second = cache(temporary.newFolder("bob"))
        first.setRefs(listOf(ref)); second.setRefs(listOf(ref))
        assertTrue(first.commit(ref, bytes, first.token()))
        assertEquals(listOf(ref), first.inventory()); assertArrayEquals(bytes, first.read(ref))
        assertTrue(second.inventory().isEmpty()); assertNull(second.read(ref))
        first.setRefs(emptyList()); assertTrue(first.inventory().isEmpty()); assertEquals(0L, first.info().bytes)
    }
    @Test fun diskCorruptionAndSystemEvictionWithdrawInventory() {
        val directory = temporary.newFolder(); val cache = cache(directory)
        cache.setRefs(listOf(ref)); cache.commit(ref, bytes, cache.token())
        File(directory, ref.assetId).writeBytes(byteArrayOf(8, 8, 8, 8))
        assertNull(cache.read(ref)); assertTrue(cache.inventory().isEmpty())
        cache.commit(ref, bytes, cache.token()); File(directory, ref.assetId).delete()
        assertTrue(cache.inventory().isEmpty()); assertNull(cache.read(ref))
    }
    @Test fun quotaCannotClaimCommitAndClearCancelsInFlightCommit() {
        val cache = cache(temporary.newFolder(), 3); cache.setRefs(listOf(ref))
        assertFalse(cache.commit(ref, bytes, cache.token())); assertTrue(cache.inventory().isEmpty())
        val other = cache(temporary.newFolder()); other.setRefs(listOf(ref)); val token = other.token(); other.clear()
        assertThrows(CancellationException::class.java) { other.commit(ref, bytes, token) }
        assertEquals(0L, other.info().bytes)
    }
    @Test fun restoredEmptyAccountPrunesOldFilesAndDisposedCacheCannotLeak() {
        val directory = temporary.newFolder(); File(directory, ref.assetId).writeBytes(bytes)
        val cache = cache(directory); cache.setRefs(emptyList()); assertFalse(File(directory, ref.assetId).exists())
        cache.setRefs(listOf(ref)); cache.commit(ref, bytes, cache.token()); cache.close()
        assertNull(cache.read(ref)); assertTrue(cache.inventory().isEmpty())
        assertTrue(File(directory, ref.assetId).exists()) // Logout preserves owned expendable files.
    }
    @Test fun directoryCreationFailureReturnsTemporaryDisplayOutcome() {
        val directory = temporary.newFolder()
        val cache = cache(directory); cache.setRefs(listOf(ref))
        assertTrue(directory.delete()); directory.writeText("blocks directory creation")
        assertFalse(cache.commit(ref, bytes, cache.token()))
        assertFalse(cache.info().readable); assertTrue(cache.info().error.isNotEmpty())
        assertTrue(cache.inventory().isEmpty())
    }
    @Test fun closeRevokesAnActiveReadWithoutWaitingForDiskValidation() {
        val directory = temporary.newFolder()
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val workers = Executors.newFixedThreadPool(2)
        val cache = FileCache(directory) { _, _ ->
            entered.countDown()
            assertTrue(release.await(5, TimeUnit.SECONDS))
        }
        cache.setRefs(listOf(ref))
        File(directory, ref.assetId).writeBytes(bytes)
        val token = cache.token()
        try {
            val reading = workers.submit<ByteArray?> { cache.read(ref) }
            assertTrue(entered.await(2, TimeUnit.SECONDS))
            workers.submit { cache.close() }.get(2, TimeUnit.SECONDS)
            assertTrue(cache.token() > token)
            assertFalse(reading.isDone)
            release.countDown()
            assertNull(reading.get(2, TimeUnit.SECONDS))
            assertTrue(cache.inventory().isEmpty())
            assertTrue(File(directory, ref.assetId).exists())
        } finally { release.countDown(); cache.close(); workers.shutdownNow() }
    }
}
