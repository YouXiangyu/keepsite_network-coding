package io.github.youxiangyu.keepsite.updates

import java.io.File
import java.io.InputStream
import java.util.UUID

fun interface UpdateSource { fun latest(): UpdateSnapshot? }
enum class DownloadPhase { PENDING, RUNNING, PAUSED, SUCCESS, FAILED }
data class UpdateTask(val id: Long, val source: String, val destination: String?, val phase: DownloadPhase,
                      val downloaded: Long = 0, val total: Long = -1, val reason: Int = 0)
interface UpdateDownloads {
    fun destination(token: String): String
    fun enqueue(source: String, destination: String, metered: Boolean): Long
    fun tasks(): List<UpdateTask>
    fun open(id: Long): InputStream
    fun remove(id: Long)
}
interface UpdatePackages {
    val deviceApi: Int
    fun installed(): UpdatePolicy.PackageIdentity
    fun archive(file: File): UpdatePolicy.PackageIdentity
}
sealed class UpdateStatus {
    object Current : UpdateStatus()
    data class Available(val snapshot: UpdateSnapshot) : UpdateStatus()
    data class Transferring(val task: UpdateTask) : UpdateStatus()
    object AwaitingOwnership : UpdateStatus()
    object Cancelled : UpdateStatus()
    data class Failed(val reason: Int) : UpdateStatus()
    data class Ready(val file: File, val identity: UpdatePolicy.PackageIdentity) : UpdateStatus()
}

/** Call from one background worker. Closing a UI subscription must never remove a system download. */
class UpdateController(private val source: UpdateSource, private val journal: UpdateJournalStore, private val downloads: UpdateDownloads,
                       private val files: UpdateFiles, private val packages: UpdatePackages,
                       private val token: () -> String = { UUID.randomUUID().toString() }) {
    @Synchronized fun check(now: Long, manual: Boolean = false): UpdateStatus {
        var saved = journal.read()
        val active = saved.entry
        if (active != null) {
            val status = resume()
            saved = journal.read()
            if (saved.entry != null) return status
        }
        if (!manual && saved.lastSuccess > 0 && now >= saved.lastSuccess && now - saved.lastSuccess < 24 * 60 * 60 * 1000L)
            return if (saved.entry?.cancelled == true) UpdateStatus.Cancelled else saved.pending?.let {
                val (candidate, record) = it.checked()
                if (UpdatePolicy.action(candidate, record, packages.installed()) == UpdatePolicy.Action.CURRENT) UpdateStatus.Current
                else if (saved.dismissed == candidate.identity) UpdateStatus.Cancelled else UpdateStatus.Available(it)
            } ?: UpdateStatus.Current
        val snapshot = source.latest()
        val parsed = snapshot?.checked()
        val action = parsed?.let { UpdatePolicy.action(it.first, it.second, packages.installed()) }
        journal.write(saved.copy(lastSuccess = now, pending = if (action == UpdatePolicy.Action.DOWNLOAD) snapshot else null))
        if (snapshot == null || action == UpdatePolicy.Action.CURRENT) return UpdateStatus.Current
        if (!manual && saved.dismissed == parsed!!.first.identity)
            return UpdateStatus.Cancelled
        return UpdateStatus.Available(snapshot)
    }

    @Synchronized fun download(snapshot: UpdateSnapshot, metered: Boolean = false, manual: Boolean = false): UpdateStatus {
        require(!metered || manual) { "计费网络下载需要本次手动操作。" }
        val (candidate, record) = snapshot.checked()
        if (UpdatePolicy.action(candidate, record, packages.installed()) == UpdatePolicy.Action.CURRENT) return UpdateStatus.Current
        val saved = journal.read()
        val old = saved.entry
        if (old != null) {
            val status = resume()
            if (journal.read().entry != null) return status
            if (!manual && old.cancelled && old.snapshot.checked().first.identity == candidate.identity) return UpdateStatus.Cancelled
        }
        if (!manual && saved.dismissed == candidate.identity) return UpdateStatus.Cancelled
        val nextToken = token()
        val entry = UpdateEntry(snapshot, nextToken, downloads.destination(nextToken), metered = metered)
        journal.write(saved.copy(entry = entry, pending = null, dismissed = null)) // Persist ownership intent before the non-atomic enqueue/ID window.
        val id = downloads.enqueue(candidate.apk.url, entry.destination, entry.metered)
        journal.write(saved.copy(entry = entry.copy(taskId = id), pending = null, dismissed = null))
        return resume()
    }

    @Synchronized fun resume(): UpdateStatus {
        val saved = journal.read()
        val entry = saved.entry ?: return UpdateStatus.Current
        val (candidate, record) = entry.snapshot.checked()
        require(entry.destination == downloads.destination(entry.token)) { "更新任务目标与本应用记录不符。" }
        if (!entry.cancelled && record != null && UpdatePolicy.action(candidate, record, packages.installed()) == UpdatePolicy.Action.CURRENT)
            return retire(saved, candidate, false)
        if (entry.ready && !entry.cancelled) {
            val status = ready(candidate, record)
            return if (status == UpdateStatus.Current) retire(saved, candidate, false) else status
        }
        val tasks = downloads.tasks()
        val matches = tasks.filter {
            trustedUpdateDownloadUri(it.source) && if (entry.taskId == null) it.destination == entry.destination
            else it.id == entry.taskId && (it.destination == null || it.destination == entry.destination)
        }
        if (matches.size != 1) {
            if (entry.taskId != null && tasks.none { it.id == entry.taskId })
                return if (entry.cancelled) retire(saved, candidate, true) else UpdateStatus.Failed(0)
            return UpdateStatus.AwaitingOwnership // A lost ID needs the full token target, never a source-only guess.
        }
        val task = matches.single()
        val owned = entry.copy(taskId = task.id)
        if (owned != entry) journal.write(saved.copy(entry = owned))
        if (owned.cancelled) return retire(saved.copy(entry = owned), candidate, true)
        if (task.phase == DownloadPhase.FAILED) return UpdateStatus.Failed(task.reason)
        if (task.phase != DownloadPhase.SUCCESS) return UpdateStatus.Transferring(task)
        if (task.destination == null) return UpdateStatus.AwaitingOwnership // Success alone cannot authorize opening a file.
        downloads.open(task.id).use { files.stage(it, candidate) }
        val status = ready(candidate, record)
        if (status == UpdateStatus.Current) return retire(saved.copy(entry = owned), candidate, false)
        journal.write(saved.copy(entry = owned.copy(ready = true)))
        return status
    }

    @Synchronized fun cancel(): UpdateStatus {
        val saved = journal.read()
        val entry = saved.entry
        val snapshot = entry?.snapshot ?: saved.pending ?: return UpdateStatus.Cancelled
        journal.write(saved.copy(entry = entry?.copy(cancelled = true), dismissed = snapshot.checked().first.identity)) // Suppress requeue even if remove or the process fails.
        if (entry == null) return UpdateStatus.Cancelled
        return resume()
    }

    /** Explicit retry after failure: retire only proved-owned work, then enqueue a new manual task. */
    @Synchronized fun retry(metered: Boolean = false): UpdateStatus {
        val saved = journal.read()
        val snapshot = saved.entry?.snapshot ?: saved.pending ?: return UpdateStatus.Current
        val status = cancel()
        return if (status == UpdateStatus.Cancelled) download(snapshot, metered, manual = true) else status
    }

    private fun retire(saved: UpdateJournal, candidate: UpdatePolicy.Candidate, cancelled: Boolean): UpdateStatus {
        val entry = requireNotNull(saved.entry)
        require(entry.destination == downloads.destination(entry.token))
        val tasks = downloads.tasks()
        val owned = tasks.filter {
            trustedUpdateDownloadUri(it.source) && if (entry.taskId == null) it.destination == entry.destination
            else it.id == entry.taskId && (it.destination == null || it.destination == entry.destination)
        }
        if (owned.size == 1) {
            val id = owned.single().id
            if (entry.taskId == null) journal.write(saved.copy(entry = entry.copy(taskId = id)))
            downloads.remove(id)
        } else if (entry.taskId == null || tasks.any { it.id == entry.taskId }) return UpdateStatus.AwaitingOwnership
        files.remove(candidate)
        val pending = if (cancelled) saved.pending ?: entry.snapshot else saved.pending?.takeUnless { it.checked().first.identity == candidate.identity }
        journal.write(saved.copy(entry = null, pending = pending))
        return if (cancelled) UpdateStatus.Cancelled else UpdateStatus.Current
    }

    private fun ready(candidate: UpdatePolicy.Candidate, record: UpdatePolicy.Verification?): UpdateStatus {
        val file = files.candidateFile(candidate)
        files.verify(file, candidate)
        val archive = packages.archive(file)
        return if (UpdatePolicy.newerArchive(candidate, record, packages.installed(), archive, packages.deviceApi))
            UpdateStatus.Ready(file, archive) else UpdateStatus.Current
    }
}
