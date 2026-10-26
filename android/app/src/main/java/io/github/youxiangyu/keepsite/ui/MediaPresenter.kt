package io.github.youxiangyu.keepsite.ui

import android.graphics.BitmapFactory
import android.graphics.Bitmap
import android.os.Handler
import android.os.Looper
import android.widget.ImageView
import android.widget.TextView
import io.github.youxiangyu.keepsite.core.AppController
import io.github.youxiangyu.keepsite.core.MediaRef
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
    private val compact: Boolean = false,
    private val maxEdge: Int = 1600,
) : Closeable {
    private val main = Handler(Looper.getMainLooper())
    private var lease: Closeable? = null
    private var decode: Future<*>? = null
    private var generation = 0
    private var key: String? = null
    private var failed = false
    fun bind(ref: MediaRef) {
        if (key == ref.assetId + ref.sha256) return
        close(); key = ref.assetId + ref.sha256
        val expected = generation
        lease = controller.loadMedia(ref) { result ->
            if (generation != expected) return@loadMedia
            failed = result.status == "error"
            status(if (result.status == "error") result.error.ifBlank { Presentation.media(result.status, result.source) }
                else Presentation.media(result.status, result.source), compact && result.status == "cached")
            if (result.file != null) showFile(result.file, expected)
            else result.bytes?.let { bytes -> decodeImage(expected) { options -> BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options) } }
        }
    }
    fun retryFailed(ref: MediaRef) {
        if (!failed) return
        close(); bind(ref)
    }
    fun local(file: File?) {
        if (file == null) { close(); status("尚未选择票面"); return }
        if (key == file.absolutePath) return
        close(); key = file.absolutePath
        status("已选择票面，尚未保存")
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
                while (options.outWidth / sample > maxEdge || options.outHeight / sample > maxEdge) sample *= 2
                read(BitmapFactory.Options().apply { inSampleSize = sample })
            }.getOrNull()
            main.post {
                if (generation != expected) { bitmap?.recycle(); return@post }
                if (bitmap == null) { failed = true; status("图片无法解码") }
                else { failed = false; image.setImageBitmap(bitmap) }
            }
        }
    }
    private fun status(text: String, hidden: Boolean = false) {
        caption.text = text
        caption.visibility = if (hidden) android.view.View.GONE else android.view.View.VISIBLE
    }
    override fun close() {
        generation++; key = null; failed = false; decode?.cancel(true); decode = null
        lease?.close(); lease = null; image.setImageDrawable(null)
    }
}
