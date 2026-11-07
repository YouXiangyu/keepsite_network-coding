package io.github.youxiangyu.keepsite.networkfixture

import android.database.Cursor
import android.database.MatrixCursor
import android.os.CancellationSignal
import android.os.ParcelFileDescriptor
import android.provider.DocumentsContract.Document
import android.provider.DocumentsContract.Root
import android.provider.DocumentsProvider
import java.io.File
import java.util.UUID

/** Dedicated isolated SAF provider; only its own generated files can be created/deleted. */
class NetworkDocuments : DocumentsProvider() {
    companion object { const val AUTHORITY = "io.github.youxiangyu.keepsite.networkverification.documents" }
    private val directory get() = File(context!!.filesDir, "n4-owned-documents").apply { mkdirs() }
    private fun own(id: String): File {
        require(id.matches(Regex("[0-9a-f]{32}")))
        return File(directory, id)
    }
    override fun onCreate() = true
    override fun queryRoots(projection: Array<out String>?): Cursor = MatrixCursor(projection ?: arrayOf(Root.COLUMN_ROOT_ID, Root.COLUMN_FLAGS, Root.COLUMN_TITLE, Root.COLUMN_DOCUMENT_ID, Root.COLUMN_MIME_TYPES, Root.COLUMN_ICON)).apply {
        newRow().apply { add(Root.COLUMN_ROOT_ID, "root"); add(Root.COLUMN_FLAGS, Root.FLAG_SUPPORTS_CREATE or Root.FLAG_LOCAL_ONLY)
            add(Root.COLUMN_TITLE, "Keepsite N4 isolated reports"); add(Root.COLUMN_DOCUMENT_ID, "root"); add(Root.COLUMN_MIME_TYPES, "*/*"); add(Root.COLUMN_ICON, android.R.drawable.ic_menu_save) }
    }
    private fun columns(projection: Array<out String>?) = projection ?: arrayOf(Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME, Document.COLUMN_MIME_TYPE, Document.COLUMN_SIZE, Document.COLUMN_FLAGS, Document.COLUMN_LAST_MODIFIED)
    private fun MatrixCursor.include(id: String) {
        val file = if (id == "root") directory else own(id)
        newRow().apply { add(Document.COLUMN_DOCUMENT_ID, id); add(Document.COLUMN_DISPLAY_NAME, if (id == "root") "Keepsite N4 isolated reports" else "n4-$id.json")
            add(Document.COLUMN_MIME_TYPE, if (id == "root") Document.MIME_TYPE_DIR else "application/json"); add(Document.COLUMN_SIZE, file.length())
            add(Document.COLUMN_FLAGS, if (id == "root") Document.FLAG_DIR_SUPPORTS_CREATE else Document.FLAG_SUPPORTS_WRITE or Document.FLAG_SUPPORTS_DELETE)
            add(Document.COLUMN_LAST_MODIFIED, file.lastModified()) }
    }
    override fun queryDocument(documentId: String, projection: Array<out String>?): Cursor = MatrixCursor(columns(projection)).apply { if (documentId == "root" || own(documentId).exists()) include(documentId) }
    override fun queryChildDocuments(parentDocumentId: String, projection: Array<out String>?, sortOrder: String?): Cursor {
        require(parentDocumentId == "root")
        return MatrixCursor(columns(projection)).apply { directory.listFiles()?.forEach { include(it.name) } }
    }
    override fun createDocument(parentDocumentId: String, mimeType: String, displayName: String): String {
        require(parentDocumentId == "root"); val id = UUID.randomUUID().toString().replace("-", ""); check(own(id).createNewFile()); return id
    }
    override fun openDocument(documentId: String, mode: String, signal: CancellationSignal?) = ParcelFileDescriptor.open(own(documentId), ParcelFileDescriptor.parseMode(mode))
    override fun deleteDocument(documentId: String) { check(own(documentId).delete()) }
    override fun isChildDocument(parentDocumentId: String, documentId: String) = parentDocumentId == "root" && (documentId == "root" || own(documentId).exists())
}
