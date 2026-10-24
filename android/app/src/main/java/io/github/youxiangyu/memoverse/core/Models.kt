package io.github.youxiangyu.memoverse.core

import android.net.Uri
import java.io.Closeable
import java.io.File

const val MAX_IMAGE_BYTES = 12L * 1024 * 1024
const val CACHE_BYTES = 128L * 1024 * 1024
val FIELD_NAMES = listOf("title", "cinema", "hall", "seat", "showtime")

data class TicketFields(
    val title: String = "", val cinema: String = "", val hall: String = "",
    val seat: String = "", val showtime: String = "",
) {
    fun values() = listOf(title, cinema, hall, seat, showtime)
}

sealed interface PosterSource { val sourcePage: String; val sourceFile: String; val fetchedAt: Long }
data class CommonsPosterSource(
    override val sourcePage: String, override val sourceFile: String, val licenseName: String,
    val licenseUrl: String, val author: String, override val fetchedAt: Long,
) : PosterSource
data class TmdbPosterSource(override val sourcePage: String, override val sourceFile: String,
    val termsUrl: String, override val fetchedAt: Long) : PosterSource
data class LoginMethod(val linked: Boolean, val email: String?)
data class AccountMethods(val password: LoginMethod, val google: LoginMethod)

data class MediaRef(
    val assetId: String, val sha256: String, val size: Long, val mimeType: String,
    val width: Int, val height: Int, val cloudStatus: String = "not_configured",
    val source: PosterSource? = null,
) {
    val url get() = "/api/media/$assetId"
}

data class Ticket(
    val id: String, val fields: TicketFields, val fieldSources: Map<String, String>,
    val createdAt: Long, val updatedAt: Long, val version: Long, val inputVersion: Long,
    val original: MediaRef, val poster: MediaRef?, val processingStatus: String,
    val processingError: String?, val operationId: String,
) {
    val cover get() = poster ?: original
}

data class Session(
    val userId: String? = null, val email: String = "", val name: String = "", val csrfToken: String = "",
    val configured: Boolean = false, val emailEnabled: Boolean = false, val googleEnabled: Boolean = false,
) { val authenticated get() = userId != null }

data class Capabilities(val processing: Boolean, val cloudStorage: Boolean, val processingReason: String?)
data class Receipt(val serverId: String, val operationId: String, val committedCursor: Long)
data class CacheInfo(val bytes: Long = 0, val files: Int = 0, val limit: Long = CACHE_BYTES, val error: String = "", val readable: Boolean = true)
data class DeviceView(val peerId: String, val label: String, val platform: String? = null, val cacheCount: Int? = null)
data class TransferView(
    val assetId: String, val direction: String, val status: String, val route: String,
    val bytes: Long, val error: String = "",
)
data class PeerView(
    val enabled: Boolean = true, val registered: Boolean = false, val peerId: String = "",
    val devices: List<DeviceView> = emptyList(), val transfers: List<TransferView> = emptyList(),
    val sending: Int = 0, val receiving: Int = 0, val error: String = "",
)
data class MediaLoad(val file: File? = null, val status: String, val source: String = "", val error: String = "", val bytes: ByteArray? = null)
data class DraftView(
    val ticketId: String?, val fields: TicketFields, val hasPhoto: Boolean, val photoName: String,
    val operationId: String, val uploadId: String, val pendingConfirmation: Boolean = false,
    val previewFile: File? = null,
)
data class AppState(
    val session: Session? = null, val tickets: List<Ticket> = emptyList(), val ready: Boolean = false,
    val connected: Boolean = false, val cursor: Long = 0, val busy: String = "", val error: String = "",
    val message: String = "", val capabilities: Capabilities? = null, val cache: CacheInfo = CacheInfo(),
    val peer: PeerView = PeerView(), val draft: DraftView? = null,
    val accountMethods: AccountMethods? = null, val accountPasswordEmail: String? = null,
)

/** UI calls are non-blocking. Listener/media callbacks are delivered on the main thread. */
interface AppController : Closeable {
    fun snapshot(): AppState
    fun subscribe(listener: (AppState) -> Unit): Closeable
    fun setForeground(foreground: Boolean)
    fun refresh()
    fun login(email: String, password: String)
    fun register(email: String, password: String)
    fun confirmEmail(email: String, code: String)
    fun resendEmail(email: String)
    fun resetPassword(email: String)
    fun confirmPassword(email: String, code: String, password: String)
    fun startGoogle(openBrowser: (String) -> Unit)
    fun handleGoogleCallback(uri: Uri)
    fun refreshAccountMethods()
    fun addAccountPassword(action: String, email: String, password: String, code: String = "")
    fun linkGoogle(openBrowser: (String) -> Unit)
    fun logout()
    fun clearNotice()
    fun newDraft()
    fun editDraft(ticketId: String)
    fun setDraftFields(fields: TicketFields)
    fun selectPhoto(uri: Uri)
    fun saveDraft()
    fun discardDraft()
    fun deleteTicket(ticketId: String)
    fun retryProcessing(ticketId: String)
    fun clearCache()
    fun setPeerEnabled(enabled: Boolean)
    fun loadMedia(media: MediaRef, listener: (MediaLoad) -> Unit): Closeable
}
