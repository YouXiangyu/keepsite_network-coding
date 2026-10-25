package io.github.youxiangyu.keepsite.core

import java.io.Closeable
import java.util.ArrayDeque
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.CancellationException
import java.util.concurrent.ExecutionException
import java.util.concurrent.FutureTask
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException

/** One import and one cleanup thread; one handoff slot, no backlog or replacement threads on timeout. */
internal class PhotoWork(private val timeoutMs: Long = 20_000) : Closeable {
    private val lock = Any()
    private val worker = ThreadPoolExecutor(1, 1, 0, TimeUnit.MILLISECONDS, ArrayBlockingQueue(1)) { task ->
        Thread(task, "keepsite-photo-import").apply { isDaemon = true }
    }
    private val cleanup = ThreadPoolExecutor(1, 1, 0, TimeUnit.MILLISECONDS, ArrayBlockingQueue(1)) { task ->
        Thread(task, "keepsite-photo-close").apply { isDaemon = true }
    }
    private var active: Job<*>? = null
    private var closed = false

    internal class Scope {
        private val resources = ArrayDeque<Closeable>()
        @Volatile private var cancelled = false
        fun check() { if (cancelled || Thread.currentThread().isInterrupted) throw CancellationException() }
        fun <T : Closeable> own(resource: T): T {
            synchronized(resources) { resources.addLast(resource) }
            // A provider may return its descriptor after cancellation. The worker's finally closes it.
            check()
            return resource
        }
        fun cancel() { cancelled = true }
        fun release() {
            while (true) {
                val resource = synchronized(resources) { resources.pollFirst() } ?: return
                try { resource.close() } catch (_: Exception) { }
            }
        }
    }
    private inner class Job<T>(block: (Scope) -> T) {
        val scope = Scope()
        private var exited = false
        private var cleaning = false
        private var cleaned = false
        val future = object : FutureTask<T>({ block(scope).also { scope.check() } }) {
            override fun run() {
                try { super.run() }
                finally {
                    try { scope.release() }
                    finally { synchronized(lock) { exited = true; releaseSlot() } }
                }
            }
        }
        private fun releaseSlot() { if (exited && (!cleaning || cleaned) && active === this) active = null }
        fun cancel() {
            synchronized(lock) {
                if (active !== this) return
                scope.cancel()
                future.cancel(true)
                if (!cleaning) {
                    cleaning = true
                    cleanup.execute {
                        try { scope.release() }
                        finally { synchronized(lock) { cleaned = true; releaseSlot() } }
                    }
                }
            }
        }
    }
    fun <T> execute(block: (Scope) -> T): T {
        val job = synchronized(lock) {
            if (closed) throw CancellationException()
            if (active != null) throw ApiFailure(message = "上一张照片仍在结束读取，请稍后重新选择；也可以先编辑票根。")
            Job(block).also {
                active = it
                try { worker.execute(it.future) }
                catch (_: RejectedExecutionException) {
                    active = null
                    throw ApiFailure(message = "照片读取暂未结束，请稍后重新选择。")
                }
            }
        }
        try { return job.future.get(timeoutMs, TimeUnit.MILLISECONDS) }
        catch (_: TimeoutException) {
            job.cancel()
            throw ApiFailure(message = "照片读取或转换超时（最多 20 秒），已取消。请先将照片下载到本机，再重新选择。")
        } catch (_: InterruptedException) {
            job.cancel(); Thread.currentThread().interrupt(); throw CancellationException()
        } catch (failure: ExecutionException) { throw failure.cause ?: failure }
    }
    fun cancel() { synchronized(lock) { active?.cancel() } }
    override fun close() {
        val job = synchronized(lock) { if (closed) return; closed = true; active }
        job?.cancel()
        worker.shutdownNow(); cleanup.shutdown()
    }
}
