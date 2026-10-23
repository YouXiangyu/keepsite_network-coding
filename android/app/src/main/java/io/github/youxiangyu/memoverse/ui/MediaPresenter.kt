package io.github.youxiangyu.memoverse.ui

import android.graphics.BitmapFactory
import android.graphics.Bitmap
import android.os.Handler
import android.os.Looper
import android.widget.ImageView
import android.widget.TextView
import io.github.youxiangyu.memoverse.core.AppController
import io.github.youxiangyu.memoverse.core.MediaRef
import java.io.Closeable
import java.io.File
import java.util.concurrent.ExecutorService
import java.util.concurrent.Future

/** Owns one displayed image and its repository lease; a detached screen releases both. */
class MediaPresenter(
    private val controller: AppController,
    private val executor: ExecutorService,
    private val image: ImageView,
    private val caption: TextView,
) : Closeable {
    private val main = Handler(Looper.getMainLooper())
    private var lease: Closeable? = null
    private var decode: Future<*>? = null
    private var generation = 0
    private var key: String? = null
    fun bind(ref: MediaRef) {
        if (key == ref.assetId + ref.sha256) return
        close(); key = ref.assetId + ref.sha256
        val expected = generation
        lease = controller.loadMedia(ref) { result ->
            if (generation != expected) return@loadMedia
            caption.text = if (result.status == "error") result.error.ifBlank { Presentation.media(result.status, result.source) }
                else Presentation.media(result.status, result.source)
            if (result.file != null) showFile(result.file, expected)
            else result.bytes?.let { bytes -> decodeImage(expected) { options -> BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options) } }
        }
    }
    fun local(file: File?) {
        if (file == null) { close(); caption.text = "尚未选择票面"; return }
        if (key == file.absolutePath) return
        close(); key = file.absolutePath
        caption.text = "已选择票面，尚未保存"
        showFile(file, generation)
    }
    private fun showFile(file: File, expected: Int) {
        decodeImage(expected) { options -> BitmapFactory.decodeFile(file.absolutePath, options) }
    }
    private fun decodeImage(expected: Int, read: (BitmapFactory.Options) -> Bitmap?) {
        decode?.cancel(true)
        decode = executor.submit {
            val bitmap = runCatching {
                val options = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                read(options)
                require(options.outWidth > 0 && options.outHeight > 0 && options.outWidth.toLong() * options.outHeight <= 40_000_000)
                var sample = 1
                while (options.outWidth / sample > 1600 || options.outHeight / sample > 1600) sample *= 2
                read(BitmapFactory.Options().apply { inSampleSize = sample })
            }.getOrNull()
            main.post {
                if (generation != expected) { bitmap?.recycle(); return@post }
                if (bitmap == null) caption.text = "图片无法解码" else image.setImageBitmap(bitmap)
            }
        }
    }
    override fun close() {
        generation++; key = null; decode?.cancel(true); decode = null
        lease?.close(); lease = null; image.setImageDrawable(null)
    }
}
