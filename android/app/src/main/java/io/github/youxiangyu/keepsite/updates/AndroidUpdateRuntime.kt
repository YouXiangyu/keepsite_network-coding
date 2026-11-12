package io.github.youxiangyu.keepsite.updates

import android.content.Context
import android.os.Handler
import android.os.Looper
import androidx.core.content.FileProvider
import io.github.youxiangyu.keepsite.BuildConfig
import java.io.Closeable
import java.io.File
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

data class UpdateCommand(val token: String, val kind: HandoffKind, val contentUri: String, val manual: Boolean)
data class UpdateUiState(val status: UpdateStatus = UpdateStatus.Current, val busy: Boolean = false, val error: String = "",
                         val candidateIdentity: String? = null, val held: ProtectedUpdate? = null, val continuePermission: Boolean = false,
                         val command: UpdateCommand? = null, val version: String = BuildConfig.VERSION_NAME,
                         val automaticAllowed: Boolean = false, val installationFailure: Boolean = false, val checked: Boolean = false,
                         val installNotice: String = "", val heldIdentity: String? = null, val candidateBytes: Long = 0,
                         val knownUpdate: Boolean = false)

/** Only update IO uses this worker; business repository and auth rendering remain independent. */
class AndroidUpdateRuntime(context: Context) : Closeable {
    private val context = context.applicationContext
    private val main = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor()
    private val closed = AtomicBoolean(); private val pending = AtomicInteger()
    private val listeners = CopyOnWriteArrayList<(UpdateUiState) -> Unit>()
    private val sourceDelegate = lazy { GithubUpdateSource() }
    private val source get() = sourceDelegate.value
    private val journal by lazy { FileUpdateJournal(File(this.context.noBackupFilesDir, "updates/state.json")) }
    private val handoffs by lazy { UpdateHandoffs(FileHandoffStore(File(this.context.noBackupFilesDir, "updates/handoffs.json"))) }
    private val files by lazy { UpdateFiles(File(this.context.filesDir, "updates/verified"), handoffs::protects) }
    private val packages by lazy { AndroidUpdatePackages(this.context) }
    private val core by lazy { UpdateController(source, journal, AndroidUpdateDownloads(this.context), files, packages) }
    @Volatile private var foreground = false
    @Volatile private var guard = UpdateInstallGuard()
    @Volatile var state = UpdateUiState(); private set
    private var status: UpdateStatus = UpdateStatus.Current
    private var failure = ""
    private var installationFailure = false
    private var checked = false
    private var command: UpdateCommand? = null
    private val poll = object : Runnable {
        override fun run() {
            if (!foreground || closed.get()) return
            if (state.error.isBlank() && (state.status is UpdateStatus.Transferring || state.status == UpdateStatus.AwaitingOwnership) || state.held != null)
                submit(clearFailure = false) { reconcile(); if (failure.isBlank() && (status is UpdateStatus.Transferring || status == UpdateStatus.AwaitingOwnership)) status = core.resume() }
            main.postDelayed(this, 1500)
        }
    }

    fun subscribe(listener: (UpdateUiState) -> Unit): Closeable { listeners += listener; listener(state); return Closeable { listeners.remove(listener) } }
    fun updateGuard(value: UpdateInstallGuard) { guard = value }
    fun setForeground(value: Boolean) {
        foreground = value; main.removeCallbacks(poll)
        if (value) {
            if (state.error.isBlank()) submit { reconcile(); status = downloadAutomatically(checkCore()) }
            main.postDelayed(poll, 1500)
        }
    }
    fun check() = submit {
        endContinuation()
        reconcile()
        if (journal.read().entry != null) {
            val stopped = core.cancel()
            if (stopped == UpdateStatus.AwaitingOwnership) { status = stopped; return@submit }
        }
        status = downloadAutomatically(checkCore(manual = true))
    }
    fun cancel() = submit { endContinuation(); status = core.cancel() }

    /** Network/validation retries always re-read the release, including same-tag asset replacement. */
    fun retryFresh(metered: Boolean = false, expectedIdentity: String? = state.candidateIdentity) = submit {
        endContinuation()
        val cancelled = core.cancel()
        if (cancelled == UpdateStatus.AwaitingOwnership) { status = cancelled; return@submit }
        status = checkCore(manual = true)
        val available = status as? UpdateStatus.Available ?: return@submit
        if (closed.get()) return@submit
        val identity = available.snapshot.checked().first.identity
        status = core.download(available.snapshot, metered = allowMeteredForCandidate(metered, expectedIdentity, identity), manual = true)
    }

    fun requestInstall(manual: Boolean, sourceAllowed: Boolean) = submit {
        if (!foreground || !guard.allows(manual) || command != null) return@submit
        reconcile()
        val selected = currentReady() ?: return@submit
        val (candidate, record) = selected.snapshot.checked()
        val file = files.candidateFile(candidate)
        files.verify(file, candidate)
        val archive = packages.archive(file)
        if (closed.get()) return@submit
        if (!UpdatePolicy.newerArchive(candidate, record, packages.installed(), archive, packages.deviceApi)) {
            status = core.resume(); return@submit
        }
        val kind = if (sourceAllowed) HandoffKind.INSTALL else HandoffKind.PERMISSION
        val attempt = handoffs.prepare(selected.snapshot, archive, kind, manual) ?: return@submit
        val uri = if (kind == HandoffKind.INSTALL) FileProvider.getUriForFile(context, "${context.packageName}.updates", file).toString() else ""
        command = UpdateCommand(attempt.token, kind, uri, manual)
    }

    /** Persist the outgoing attempt first; the Activity checks its live guard again before launch. */
    fun issue(token: String, result: (UpdateCommand?) -> Unit) = submit(force = true, clearFailure = false, commandToken = token) {
        try {
            val outgoing = command?.takeIf { it.token == token }
            if (outgoing == null || !foreground || !guard.allows(outgoing.manual)) {
                handoffs.notLaunched(token); clearCommand(token); main.post { result(null) }; return@submit
            }
            handoffs.issue(token); command = null
            main.post { if (!closed.get()) result(outgoing) }
        } catch (failure: Exception) {
            clearCommand(token); main.post { result(null) }; throw failure
        }
    }
    private fun clearCommand(token: String) { if (command?.token == token) command = null }
    fun notLaunched(token: String) = submit(force = true, clearFailure = false, commandToken = token) { handoffs.notLaunched(token); clearCommand(token) }
    fun launchFailed(token: String) = submit(force = true, clearFailure = false, commandToken = token) {
        val current = handoffs.state().active?.token == token
        handoffs.notLaunched(token); clearCommand(token)
        if (current) { installationFailure = true; failure = "无法打开系统更新页面，可稍后再次安装。" }
    }
    fun permissionReturned(token: String, granted: Boolean) = submit(force = true, clearFailure = false, commandToken = token) {
        clearCommand(token)
        val active = handoffs.state().active
        if (active?.token != token || active.kind != HandoffKind.PERMISSION) return@submit
        failure = ""; installationFailure = false; handoffs.permissionReturned(token, granted); reconcile()
    }
    fun installerReturned(token: String) = submit(force = true, clearFailure = false, commandToken = token) {
        clearCommand(token)
        val active = handoffs.state().active
        if (active?.token != token || active.kind != HandoffKind.INSTALL) return@submit
        failure = ""; installationFailure = false; handoffs.installerReturned(token); reconcile()
    }

    private fun endContinuation() { handoffs.endPermissionContinuation(); command = null }
    private fun downloadAutomatically(value: UpdateStatus): UpdateStatus {
        if (closed.get() || value !is UpdateStatus.Available) return value
        val active = handoffs.state().active
        if (active != null && active.update.snapshot.checked().first.identity != value.snapshot.checked().first.identity) endContinuation()
        return core.download(value.snapshot, metered = false, manual = false)
    }

    private fun checkCore(manual: Boolean = false): UpdateStatus {
        val result = core.check(System.currentTimeMillis(), manual)
        checked = journal.read().lastSuccess > 0
        return result
    }

    private fun currentReady(): ProtectedUpdate? {
        val ready = status as? UpdateStatus.Ready
        if (ready != null) {
            val entry = journal.read().entry
            if (entry != null && files.candidateFile(entry.snapshot.checked().first).canonicalFile == ready.file.canonicalFile)
                return ProtectedUpdate(entry.snapshot, ready.identity, false)
        }
        val held = handoffs.state()
        return held.active?.update ?: held.files.lastOrNull()
    }

    private fun reconcile() {
        val confirmed = handoffs.confirm(packages.installed())
        if (confirmed.isNotEmpty()) { failure = ""; installationFailure = false }
        for (finished in confirmed) {
            val candidate = finished.snapshot.checked().first
            val entry = journal.read().entry
            if (entry?.snapshot?.checked()?.first?.identity == candidate.identity) status = core.resume()
            files.remove(candidate)
        }
        if (confirmed.isNotEmpty() && status !is UpdateStatus.Transferring) status = checkCore()
    }

    @Synchronized private fun submit(force: Boolean = false, clearFailure: Boolean = true, commandToken: String? = null, action: () -> Unit) {
        if (closed.get()) return
        if (force) pending.incrementAndGet() else if (!pending.compareAndSet(0, 1)) return
        publishBusy()
        worker.execute {
            try { if (!closed.get()) { if (clearFailure) { failure = ""; installationFailure = false }; action() } }
            catch (_: Exception) { if (commandToken == null) command = null else clearCommand(commandToken); failure = "更新失败，请重试。" }
            finally { pending.decrementAndGet(); if (!closed.get()) publish() }
        }
    }
    private fun publishBusy() = main.post {
        if (!closed.get()) { state = state.copy(busy = true); listeners.forEach { it(state) } }
    }
    private fun publish() {
        val saved = try { handoffs.state() } catch (_: Exception) { HandoffState() }
        val entry = try { journal.read() } catch (_: Exception) { null }
        val candidate = (entry?.entry?.snapshot ?: entry?.pending)?.checked()
        val identity = candidate?.first?.identity
        val held = saved.active?.update ?: saved.files.lastOrNull()
        val heldIdentity = held?.snapshot?.checked()?.first?.identity
        val notice = when (saved.active?.kind) {
            HandoffKind.PERMISSION -> "请在系统页面允许安装更新。"
            HandoffKind.GRANTED -> "已允许安装，可继续安装更新。"
            HandoffKind.INSTALL -> if (saved.active.issued) "请在系统页面完成安装。" else ""
            HandoffKind.RETURNED -> "尚未确认安装结果，可再次安装。"
            null -> if (held != null) "尚未确认安装结果，可再次安装。" else ""
        }
        val update = UpdateUiState(status, pending.get() > 0, failure, identity, held,
            saved.active?.kind == HandoffKind.GRANTED, command, automaticAllowed = failure.isBlank() && identity != null && identity !in saved.offered,
            installationFailure = installationFailure, checked = checked, installNotice = notice, heldIdentity = heldIdentity,
            candidateBytes = candidate?.first?.apk?.bytes ?: 0, knownUpdate = candidate?.second != null)
        main.post { if (!closed.get()) { state = update; listeners.forEach { it(update) } } }
    }
    @Synchronized override fun close() {
        if (!closed.compareAndSet(false, true)) return
        foreground = false; main.removeCallbacks(poll); listeners.clear()
        worker.execute { if (sourceDelegate.isInitialized()) source.close() }; worker.shutdown() // No lifecycle remove/file cleanup.
    }
}
