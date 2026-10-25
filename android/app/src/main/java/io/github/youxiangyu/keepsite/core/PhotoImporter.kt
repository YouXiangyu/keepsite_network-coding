package io.github.youxiangyu.keepsite.core

import android.content.ContentResolver
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.ColorSpace
import android.graphics.ImageDecoder
import android.graphics.PorterDuff
import android.net.Uri
import android.os.Build
import android.os.CancellationSignal
import androidx.annotation.RequiresApi
import java.io.ByteArrayOutputStream
import java.io.Closeable
import java.nio.ByteBuffer
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

internal data class PreparedPhoto(val bytes: ByteArray, val media: MediaRef, val converted: Boolean)

internal class PhotoImporter(private val resolver: ContentResolver) : Closeable {
    private val work = PhotoWork()
    fun read(uri: Uri): PreparedPhoto = try {
        work.execute { scope ->
            val signal = CancellationSignal()
            scope.own(Closeable { signal.cancel() })
            val descriptor = resolver.openAssetFileDescriptor(uri, "r", signal)
                ?: throw ApiFailure(message = "无法读取所选照片。请先将照片下载到本机，再重新选择。")
            scope.own(descriptor)
            if (descriptor.declaredLength > MAX_IMAGE_BYTES) throw ApiFailure(message = "图片超过 12 MiB。")
            val stream = scope.own(descriptor.createInputStream())
            val output = PhotoBytes()
            val buffer = ByteArray(16 * 1024)
            while (true) {
                scope.check()
                val count = stream.read(buffer)
                if (count < 0) break
                if (count == 0) continue
                output.write(buffer, 0, count)
            }
            scope.check()
            PhotoCodec.prepare(output.toByteArray()).also { scope.check() }
        }
    } catch (_: OutOfMemoryError) {
        throw ApiFailure(message = "本机内存不足，未替换原照片。请缩小图片后重试。")
    }
    fun cancel() = work.cancel()
    override fun close() = work.close()
}

/** Enforces the output limit while writing, rather than after allocating an arbitrary JPEG. */
internal class PhotoBytes : ByteArrayOutputStream(64 * 1024) {
    override fun write(value: Int) {
        if (count.toLong() + 1 > MAX_IMAGE_BYTES) throw ApiFailure(message = "图片超过 12 MiB。")
        super.write(value)
    }
    override fun write(bytes: ByteArray, offset: Int, length: Int) {
        if (count.toLong() + length > MAX_IMAGE_BYTES) throw ApiFailure(message = "图片超过 12 MiB。")
        super.write(bytes, offset, length)
    }
}

internal object PhotoCodec {
    const val JPEG_QUALITY = 90
    const val MAX_EDGE = 3072
    const val MAX_DECODE_PIXELS = 6_000_000
    private val heifBrands = setOf("heic", "heix", "hevc", "hevx", "mif1", "msf1")
    fun isHeif(bytes: ByteArray): Boolean {
        if (bytes.size < 16 || String(bytes, 4, 4, Charsets.US_ASCII) != "ftyp") return false
        val size = ByteBuffer.wrap(bytes, 0, 4).int
        if (size !in 16..min(bytes.size, 4096) || size % 4 != 0) return false
        return (8 until size step 4).any { offset ->
            offset != 12 && String(bytes, offset, 4, Charsets.US_ASCII) in heifBrands
        }
    }
    fun targetSize(width: Int, height: Int): Pair<Int, Int> {
        if (width <= 0 || height <= 0 || width.toLong() * height > 40_000_000) {
            throw ApiFailure(message = "图片尺寸无效或超过 4000 万像素。")
        }
        val scale = min(1.0, min(MAX_EDGE.toDouble() / max(width, height), sqrt(MAX_DECODE_PIXELS.toDouble() / (width.toLong() * height))))
        return max(1, floor(width * scale).toInt()) to max(1, floor(height * scale).toInt())
    }
    fun prepare(bytes: ByteArray): PreparedPhoto {
        if (bytes.isEmpty() || bytes.size > MAX_IMAGE_BYTES) throw ApiFailure(message = "请选择不超过 12 MiB 的图片。")
        if (!isHeif(bytes)) return PreparedPhoto(bytes, ImageFiles.inspect(bytes), false)
        if (Build.VERSION.SDK_INT < 28) throw ApiFailure(message = "此 Android 版本无法转换 HEIC/HEIF。请先在相册导出 JPEG、PNG 或 WebP 后重试。")
        val jpeg = try { decodeJpeg(bytes, heifOnly = true) }
        catch (failure: ApiFailure) { throw failure }
        catch (_: Exception) { throw ApiFailure(message = "此设备无法解码这张 HEIC/HEIF，或文件已损坏。请在相册导出 JPEG 后重试。") }
        return PreparedPhoto(jpeg, ImageFiles.inspect(jpeg), true)
    }
    @RequiresApi(28)
    internal fun decodeJpeg(bytes: ByteArray, heifOnly: Boolean = false): ByteArray {
        val bitmap = ImageDecoder.decodeBitmap(ImageDecoder.createSource(ByteBuffer.wrap(bytes))) { decoder, info, _ ->
            if (heifOnly && info.mimeType !in setOf("image/heif", "image/heic")) {
                throw ApiFailure(message = "请选择 JPEG、PNG、WebP 或可解码的 HEIC/HEIF 静态图片。")
            }
            val (width, height) = targetSize(info.size.width, info.size.height)
            decoder.allocator = ImageDecoder.ALLOCATOR_SOFTWARE
            decoder.isMutableRequired = true
            decoder.setTargetColorSpace(ColorSpace.get(ColorSpace.Named.SRGB))
            decoder.setTargetSize(width, height)
            decoder.setOnPartialImageListener { false }
        }
        try {
            if (bitmap.width.toLong() * bitmap.height > MAX_DECODE_PIXELS || max(bitmap.width, bitmap.height) > MAX_EDGE) {
                throw ApiFailure(message = "图片转换尺寸超过限制，请缩小图片后重试。")
            }
            // ImageDecoder applies encoded orientation/mirroring. Do not rotate a second time.
            if (bitmap.hasAlpha()) {
                Canvas(bitmap).drawColor(Color.WHITE, PorterDuff.Mode.DST_OVER)
                bitmap.setHasAlpha(false)
            }
            val output = PhotoBytes()
            if (!bitmap.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, output)) {
                throw ApiFailure(message = "HEIC/HEIF 转换失败，请在相册导出 JPEG 后重试。")
            }
            return output.toByteArray()
        } finally { bitmap.recycle() }
    }
}
