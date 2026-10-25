package io.github.youxiangyu.keepsite.core

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.ImageDecoder
import android.graphics.Paint
import android.net.Uri
import android.os.Build
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayOutputStream
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder

/** Synthetic images only; does not require an account, network or the user's photo library. */
@RunWith(AndroidJUnit4::class)
class PhotoImportInstrumentedTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context get() = instrumentation.targetContext
    private val red = Color.rgb(240, 20, 20)
    private val green = Color.rgb(20, 220, 20)
    private val blue = Color.rgb(20, 20, 240)
    private val white = Color.rgb(240, 240, 240)
    private fun fixture(name: String) = instrumentation.context.assets.open("photo-import/$name").use { it.readBytes() }
    private fun pattern(format: Bitmap.CompressFormat): ByteArray {
        val bitmap = Bitmap.createBitmap(96, 64, Bitmap.Config.ARGB_8888)
        try {
            val canvas = Canvas(bitmap); val paint = Paint()
            for ((index, color) in listOf(red, green, blue, white).withIndex()) {
                paint.color = color
                val left = (index % 2) * 48f; val top = (index / 2) * 32f
                canvas.drawRect(left, top, left + 48, top + 32, paint)
            }
            return ByteArrayOutputStream().also { assertTrue(bitmap.compress(format, 95, it)) }.toByteArray()
        } finally { bitmap.recycle() }
    }
    private fun assertImage(bytes: ByteArray, width: Int, height: Int, colors: List<Int>) {
        val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size) ?: throw AssertionError("JPEG must decode")
        try {
            assertEquals(width, bitmap.width); assertEquals(height, bitmap.height)
            colors.forEachIndexed { index, expected ->
                val actual = bitmap.getPixel(width * (if (index % 2 == 0) 1 else 3) / 4,
                    height * (if (index / 2 == 0) 1 else 3) / 4)
                for (component in listOf(Color::red, Color::green, Color::blue)) {
                    assertTrue("Quadrant $index must retain its orientation and color", kotlin.math.abs(component(actual) - component(expected)) <= 35)
                }
            }
        } finally { bitmap.recycle() }
    }
    @Test fun existingFormatsKeepTheirOriginalBytes() {
        for (format in listOf(Bitmap.CompressFormat.JPEG, Bitmap.CompressFormat.PNG, Bitmap.CompressFormat.WEBP)) {
            val bytes = pattern(format); val copy = bytes.copyOf(); val photo = PhotoCodec.prepare(bytes)
            assertFalse(photo.converted); assertArrayEquals(copy, photo.bytes); assertArrayEquals(copy, bytes)
            assertEquals(96, photo.media.width); assertEquals(64, photo.media.height)
        }
    }
    @Test fun actualHeifContainerRotationAndMirrorProduceUprightJpeg() {
        assumeTrue("ImageDecoder requires API 28+", Build.VERSION.SDK_INT >= 28)
        assumeTrue("This device must provide a HEIF decoder for the success path", ImageDecoder.isMimeTypeSupported("image/heif"))
        val cases = listOf(
            Triple("quadrants.heic", 96 to 64, listOf(red, green, blue, white)),
            Triple("rotate90-container.heic", 64 to 96, listOf(blue, red, white, green)),
            Triple("mirror-container.heic", 96 to 64, listOf(green, red, white, blue)),
        )
        for ((name, size, colors) in cases) {
            val bytes = fixture(name); val before = bytes.copyOf()
            val photo = PhotoCodec.prepare(bytes)
            assertTrue(photo.converted); assertEquals("image/jpeg", photo.media.mimeType)
            assertTrue(photo.bytes.size <= MAX_IMAGE_BYTES)
            assertImage(photo.bytes, size.first, size.second, colors)
            assertArrayEquals("The selected HEIF source must not be modified", before, bytes)
        }
    }
    @Test fun resolverImportPreservesTheSourceAndRecoversAfterDamagedInput() {
        val file = File.createTempFile("photo-import-", ".heic", context.cacheDir)
        try {
            PhotoImporter(context.contentResolver).use { importer ->
                file.writeBytes(byteArrayOf(1, 2, 3))
                assertThrows(ApiFailure::class.java) { importer.read(Uri.fromFile(file)) }
                val bytes = pattern(Bitmap.CompressFormat.PNG); file.writeBytes(bytes)
                var photo: PreparedPhoto? = null
                val deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(3)
                while (photo == null) {
                    try { photo = importer.read(Uri.fromFile(file)) }
                    catch (failure: ApiFailure) {
                        if (!failure.message.orEmpty().contains("上一张") || System.nanoTime() >= deadline) throw failure
                        Thread.sleep(5)
                    }
                }
                assertArrayEquals(bytes, photo.bytes); assertArrayEquals(bytes, file.readBytes())
            }
        } finally { file.delete() }
    }
    @Test fun malformedTruncatedAndOversizedSourcesFailExplicitly() {
        for (bytes in listOf(byteArrayOf(1, 2, 3), fixture("quadrants.heic").copyOf(80), ByteArray(MAX_IMAGE_BYTES.toInt() + 1))) {
            val failure = assertThrows(ApiFailure::class.java) { PhotoCodec.prepare(bytes) }
            assertFalse(failure.message.isNullOrEmpty())
        }
    }
    private fun withExif(jpeg: ByteArray, orientation: Int): ByteArray {
        val tiff = ByteBuffer.allocate(26).order(ByteOrder.LITTLE_ENDIAN)
            .put('I'.code.toByte()).put('I'.code.toByte()).putShort(42).putInt(8).putShort(1)
            .putShort(0x112).putShort(3).putInt(1).putShort(orientation.toShort()).putShort(0).putInt(0).array()
        val payload = "Exif\u0000\u0000".toByteArray(Charsets.US_ASCII) + tiff
        return jpeg.copyOfRange(0, 2) + byteArrayOf(-1, -31, 0, (payload.size + 2).toByte()) + payload + jpeg.copyOfRange(2, jpeg.size)
    }
    @Test fun decoderAppliesExifRotationAndMirroringExactlyOnce() {
        assumeTrue(Build.VERSION.SDK_INT >= 28)
        val jpeg = pattern(Bitmap.CompressFormat.JPEG)
        assertImage(PhotoCodec.decodeJpeg(withExif(jpeg, 6)), 64, 96, listOf(blue, red, white, green))
        assertImage(PhotoCodec.decodeJpeg(withExif(jpeg, 2)), 96, 64, listOf(green, red, white, blue))
    }
}
