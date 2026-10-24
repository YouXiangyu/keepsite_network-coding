package io.github.youxiangyu.keepsite.core

import android.graphics.BitmapFactory
import io.github.youxiangyu.keepsite.peer.PeerCache
import java.io.Closeable
import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.util.concurrent.CancellationException
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicLong

fun sha256(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

object ImageFiles {
    fun inspect(bytes: ByteArray): MediaRef {
        if (bytes.isEmpty() || bytes.size > MAX_IMAGE_BYTES) throw ApiFailure(message = "请选择不超过 12 MiB 的 JPEG、PNG 或 WebP 图片。")
        val options = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options)
        val mime = options.outMimeType
        if (mime !in setOf("image/jpeg", "image/png", "image/webp") || options.outWidth <= 0 || options.outHeight <= 0
            || options.outWidth.toLong() * options.outHeight > 40_000_000) throw ApiFailure(message = "图片格式或尺寸不受支持。")
        val sha = sha256(bytes)
        return MediaRef(sha, sha, bytes.size.toLong(), mime, options.outWidth, options.outHeight)
    }
    fun validate(bytes: ByteArray, ref: MediaRef) {
        if (bytes.size.toLong() != ref.size || sha256(bytes) != ref.sha256) throw ApiFailure(message = "图片完整性校验未通过。")
        val actual = inspect(bytes)
        if (actual.mimeType != ref.mimeType || actual.width != ref.width || actual.height != ref.height) throw ApiFailure(message = "图片内容与记录不一致。")
    }
}

/** Files are expendable; metadata authority and live references remain on the server. */
class FileCache(
    directory: File, private val budget: Long = CACHE_BYTES,
    private val validate: (ByteArray, MediaRef) -> Unit = ImageFiles::validate,
) : PeerCache, Closeable {
    private val directory = directory.canonicalFile.apply { mkdirs() }
    private val lock = Any()
    private val listeners = CopyOnWriteArrayList<() -> Unit>()
    private var refs = emptyMap<String, MediaRef>()
    private val validated = HashSet<String>()
    @Volatile private var active = true
    private val epoch = AtomicLong()
    private var cleanupPending = true
    private var lastError = ""
    init {
        this.directory.listFiles()?.filter { it.isFile && it.name.startsWith("incoming-") && it.name.endsWith(".tmp") }?.forEach { it.delete() }
    }
    fun token(): Long = epoch.get()
    private fun same(left: MediaRef, right: MediaRef?) = right != null && left.assetId == right.assetId && left.sha256 == right.sha256
        && left.size == right.size && left.mimeType == right.mimeType && left.width == right.width && left.height == right.height
    private fun path(id: String): File {
        require(id.matches(Regex("[a-f0-9]{64}")))
        return File(directory, id).also { require(it.canonicalFile.parentFile == directory) }
    }
    fun live(ref: MediaRef): Boolean = synchronized(lock) { active && same(ref, refs[ref.assetId]) }
    override fun canShare(media: MediaRef): Boolean = synchronized(lock) {
        val current = refs[media.assetId]
        if (!active || current == null || media.assetId !in validated || current.sha256 != media.sha256
            || current.size != media.size || current.mimeType != media.mimeType) return@synchronized false
        val file = path(media.assetId)
        file.isFile && file.length() == media.size
    }
    private fun guard(ref: MediaRef, expected: Long) {
        if (!active || expected != epoch.get() || !same(ref, refs[ref.assetId])) throw CancellationException("图片引用已撤销。")
    }
    private fun notifyChanged() { listeners.forEach { it() } }
    override fun subscribe(listener: () -> Unit): Closeable { listeners.add(listener); return Closeable { listeners.remove(listener) } }
    fun setRefs(media: List<MediaRef>) {
        synchronized(lock) {
            if (!active) return
            val next = media.associateBy { it.assetId }
            if (next != refs) cleanupPending = true
            refs = next
            validated.retainAll(next.keys)
            prune()
        }
        notifyChanged()
    }
    private fun prune() {
        if (!cleanupPending) return
        var failed = false
        val files = directory.listFiles()
        if (files == null && directory.exists()) { lastError = "本机缓存目录暂不可读，重新检查时重试。"; return }
        files?.forEach { file ->
            if (file.name.matches(Regex("[a-f0-9]{64}")) && file.name !in refs) {
                if (!file.delete() && file.exists()) failed = true
            }
        }
        cleanupPending = failed
        lastError = if (failed) "无引用缓存清理未完成，重新检查时重试。" else ""
    }
    fun refresh() { synchronized(lock) { prune() }; notifyChanged() }
    override fun read(media: MediaRef): ByteArray? {
        var changed = false
        val bytes = synchronized(lock) {
            if (!active || !same(media, refs[media.assetId])) return@synchronized null
            val file = path(media.assetId)
            try {
                if (!file.isFile || file.length() != media.size || file.length() > MAX_IMAGE_BYTES) throw java.io.IOException()
                val raw = file.readBytes()
                validate(raw, media)
                if (!active) return@synchronized null
                validated.add(media.assetId)
                raw
            } catch (_: Exception) {
                changed = validated.remove(media.assetId)
                if (file.exists()) file.delete()
                null
            }
        }
        if (changed) notifyChanged()
        return bytes
    }
    fun file(media: MediaRef): File? = if (read(media) == null) null else path(media.assetId)
    override fun inventory(): List<MediaRef> {
        val candidates = synchronized(lock) { if (!active) return emptyList(); refs.values.sortedBy { it.assetId } }
        var removed = false
        val result = candidates.filter { ref ->
            val known = synchronized(lock) {
                val file = path(ref.assetId)
                if (!file.isFile || file.length() != ref.size) removed = validated.remove(ref.assetId) || removed
                ref.assetId in validated
            }
            known || read(ref) != null
        }.take(1024)
        if (removed) notifyChanged()
        return if (active) result else emptyList()
    }
    fun info(): CacheInfo = synchronized(lock) {
        val entries = directory.listFiles()
        if (entries == null && directory.exists()) return@synchronized CacheInfo(limit = budget, error = "本机缓存目录暂不可读。", readable = false)
        val files = entries?.filter { it.isFile && it.name.matches(Regex("[a-f0-9]{64}")) } ?: emptyList()
        CacheInfo(files.sumOf { it.length() }, files.size, budget, lastError)
    }
    /** False means quota/disk failure: caller may show a separately leased temporary image. */
    fun commit(ref: MediaRef, bytes: ByteArray, expected: Long): Boolean {
        validate(bytes, ref)
        val committed = synchronized(lock) {
            guard(ref, expected)
            var temp: File? = null
            try {
                if (!directory.isDirectory && !directory.mkdirs()) throw java.io.IOException()
                val destination = path(ref.assetId)
                val info = info()
                if (!info.readable) throw java.io.IOException()
                val used = info.bytes - if (destination.exists()) destination.length() else 0
                if (used + bytes.size > budget) { lastError = "本机缓存空间不足，图片仍可临时查看。"; return@synchronized false }
                temp = File.createTempFile("incoming-", ".tmp", directory)
                FileOutputStream(temp).use { out -> out.write(bytes); out.fd.sync() }
                guard(ref, expected)
                Files.move(temp.toPath(), destination.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
                validated.add(ref.assetId)
                if (!cleanupPending) lastError = ""
                true
            } catch (failure: CancellationException) { throw failure }
            catch (_: java.io.IOException) { lastError = "本机缓存写入失败，图片仍可临时查看。"; false }
            catch (_: SecurityException) { lastError = "本机缓存写入失败，图片仍可临时查看。"; false }
            finally { temp?.delete() }
        }
        notifyChanged()
        return committed
    }
    fun clear() {
        synchronized(lock) {
            epoch.incrementAndGet(); validated.clear()
            var failed = false
            directory.listFiles()?.filter { it.isFile }?.forEach { if (!it.delete() && it.exists()) failed = true }
            lastError = if (failed) "部分本机缓存未能清除，请重新检查。" else ""
            cleanupPending = failed
        }
        notifyChanged()
    }
    override fun close() {
        // UI logout must revoke access without waiting for a worker's disk read or checksum.
        active = false
        epoch.incrementAndGet()
        listeners.clear()
    }
}
