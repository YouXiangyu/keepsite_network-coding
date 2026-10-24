package io.github.youxiangyu.keepsite.peer

import io.github.youxiangyu.keepsite.core.MediaRef
import org.json.JSONObject
import java.io.Closeable

data class EventState(val connected: Boolean, val serverId: String, val generation: Long)

/** Messages have already passed the session/user/server envelope guard. */
interface EventPort {
    fun state(): EventState
    fun subscribeConnection(listener: () -> Unit): Closeable
    fun subscribeMessages(listener: (JSONObject) -> Unit): Closeable
    fun send(message: JSONObject): Boolean
}

/** Inventory contains only live refs with validated files; read revalidates the actual file. */
interface PeerCache {
    fun canShare(media: MediaRef): Boolean
    fun inventory(): List<MediaRef>
    fun read(media: MediaRef): ByteArray?
    fun subscribe(listener: () -> Unit): Closeable
}

data class PeerDownload(val bytes: ByteArray, val route: String, val resumedBytes: Long = 0)
