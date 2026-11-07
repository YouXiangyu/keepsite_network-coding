package io.github.youxiangyu.keepsite.updates

import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.UUID

data class UpdateSnapshot(val release: String, val verification: String?) {
    fun checked(): Pair<UpdatePolicy.Candidate, UpdatePolicy.Verification?> {
        val candidate = requireNotNull(UpdatePolicy.release(release)) { "保存的更新候选不再有效。" }
        val record = verification?.let { UpdatePolicy.verification(candidate, it.toByteArray(Charsets.UTF_8)) }
        require((candidate.verification == null) == (record == null)) { "保存的版本验证记录缺失。" }
        return candidate to record
    }
}

data class UpdateEntry(val snapshot: UpdateSnapshot, val token: String, val destination: String, val taskId: Long? = null,
                       val metered: Boolean = false, val cancelled: Boolean = false, val ready: Boolean = false) {
    init {
        require(UUID.fromString(token).toString() == token && destination.isNotBlank())
        require(taskId == null || taskId > 0)
    }
}
data class UpdateJournal(val lastSuccess: Long = 0, val entry: UpdateEntry? = null, val pending: UpdateSnapshot? = null, val dismissed: String? = null)
interface UpdateJournalStore {
    fun read(): UpdateJournal
    fun write(value: UpdateJournal)
}

/** Only update state, not account credentials. A bad journal is retained rather than silently reset. */
class FileUpdateJournal(file: File) : UpdateJournalStore {
    private val file = file.canonicalFile
    private val directory = requireNotNull(this.file.parentFile)
    override fun read(): UpdateJournal {
        if (!file.exists()) return UpdateJournal()
        require(file.length() <= 1024 * 1024) { "更新任务记录过大。" }
        val value = JSONObject(file.readText(Charsets.UTF_8))
        require(number(value, "schema") == 1L)
        val lastSuccess = number(value, "lastSuccess").also { require(it >= 0) }
        val pendingRaw = value.get("pending")
        require(pendingRaw == JSONObject.NULL || pendingRaw is JSONObject)
        val pending = (pendingRaw as? JSONObject)?.let(::snapshot)
        val dismissed = if (value.get("dismissed") == JSONObject.NULL) null else text(value, "dismissed").also { require(it.length in 1..500) }
        val raw = value.opt("entry")
        if (raw == JSONObject.NULL) return UpdateJournal(lastSuccess, pending = pending, dismissed = dismissed)
        require(raw is JSONObject)
        val snapshot = UpdateSnapshot(text(raw, "release"), if (raw.opt("verification") == JSONObject.NULL) null else text(raw, "verification"))
        snapshot.checked()
        val entry = UpdateEntry(snapshot, text(raw, "token"), text(raw, "destination"),
            if (raw.opt("taskId") == JSONObject.NULL) null else number(raw, "taskId"),
            boolean(raw, "metered"), boolean(raw, "cancelled"), boolean(raw, "ready"))
        return UpdateJournal(lastSuccess, entry, pending, dismissed)
    }
    override fun write(value: UpdateJournal) {
        require(value.lastSuccess >= 0)
        val entry = value.entry?.let {
            it.snapshot.checked()
            JSONObject().put("release", it.snapshot.release).put("verification", it.snapshot.verification ?: JSONObject.NULL)
                .put("token", it.token).put("destination", it.destination).put("taskId", it.taskId ?: JSONObject.NULL)
                .put("metered", it.metered).put("cancelled", it.cancelled).put("ready", it.ready)
        }
        val pending = value.pending?.let {
            it.checked()
            JSONObject().put("release", it.release).put("verification", it.verification ?: JSONObject.NULL)
        }
        val bytes = JSONObject().put("schema", 1).put("lastSuccess", value.lastSuccess).put("entry", entry ?: JSONObject.NULL).put("pending", pending ?: JSONObject.NULL)
            .put("dismissed", value.dismissed ?: JSONObject.NULL)
            .toString().toByteArray(Charsets.UTF_8)
        require(bytes.size <= 1024 * 1024)
        require(directory.isDirectory || directory.mkdirs())
        val temporary = File(directory, file.name + ".tmp")
        FileOutputStream(temporary).use { it.write(bytes); it.fd.sync() }
        Files.move(temporary.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
    }
    private fun snapshot(value: JSONObject) = UpdateSnapshot(text(value, "release"),
        if (value.opt("verification") == JSONObject.NULL) null else text(value, "verification")).also { it.checked() }
    private fun text(value: JSONObject, key: String) = requireNotNull(value.opt(key) as? String)
    private fun boolean(value: JSONObject, key: String) = requireNotNull(value.opt(key) as? Boolean)
    private fun number(value: JSONObject, key: String): Long = when (val raw = value.opt(key)) {
        is Int -> raw.toLong()
        is Long -> raw
        else -> throw IllegalArgumentException("更新任务数字字段无效。")
    }
}
