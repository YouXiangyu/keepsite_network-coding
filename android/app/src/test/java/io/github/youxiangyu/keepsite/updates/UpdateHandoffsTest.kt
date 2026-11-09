package io.github.youxiangyu.keepsite.updates

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.ByteArrayInputStream
import java.io.File

class UpdateHandoffsTest {
    @get:Rule val temporary = TemporaryFolder()
    private val bytes = "synthetic verified APK".toByteArray()
    private val signer = "a".repeat(64)
    private val target = UpdatePolicy.PackageIdentity(UpdatePolicy.PACKAGE, 4, setOf(signer))
    private val safe = UpdateInstallGuard(resumed = true, focused = true, authenticated = true)
    private fun snapshot(id: Int = 1): UpdateSnapshot {
        val asset = JSONObject().put("id", id).put("name", "keepsite.apk").put("state", "uploaded").put("size", bytes.size)
            .put("digest", "sha256:${UpdatePolicy.digest(bytes)}")
            .put("browser_download_url", "https://github.com/${UpdatePolicy.REPOSITORY}/releases/download/v0.1/keepsite.apk")
        return UpdateSnapshot(JSONObject().put("draft", false).put("prerelease", false).put("tag_name", "v0.1")
            .put("assets", JSONArray().put(asset)).toString(), null)
    }
    private fun store() = FileHandoffStore(File(temporary.newFolder(), "handoffs.json"))

    @Test fun latestGuardProtectsDraftAndVisibleAccountWorkButNotHiddenGoogleOnlyEmail() {
        assertFalse(safe.copy(awaitingDraft = true).allows(manual = true))
        assertFalse(safe.copy(draft = true).allows(manual = true))
        assertFalse(safe.copy(resumed = false).allows(manual = true))
        assertFalse(safe.copy(focused = false).allows(manual = true))
        val hiddenEmail = UpdateInstallGuard.accountInputsActive(listOf(false to true))
        assertTrue(safe.copy(authenticated = false, accountInteraction = hiddenEmail).allows(manual = true))
        assertFalse(safe.copy(authenticated = false).allows(manual = false))
        val visibleCode = UpdateInstallGuard.accountInputsActive(listOf(true to true))
        assertFalse(safe.copy(accountInteraction = visibleCode).allows(manual = true))
        assertFalse(safe.copy(busy = true, picker = true).allows(manual = true))
    }

    @Test fun readyObserverDoesNotResubmitAutomaticWorkerAfterPromptButManualAndNewCandidateCanContinue() {
        val registry = UpdateHandoffs(store()); val first = snapshot(); var workers = 0; var failed = false
        val files = UpdateFiles(temporary.newFolder())
        fun onReady(value: UpdateSnapshot, corrupt: Boolean = false) {
            val identity = value.checked().first.identity
            val automaticAllowed = identity !in registry.state().offered
            if (safe.shouldRequest(true, false, false, automaticAllowed, false, failed)) {
                workers++
                val candidate = value.checked().first
                files.candidateFile(candidate).writeBytes(if (corrupt) ByteArray(bytes.size) else bytes)
                try { files.verify(files.candidateFile(candidate), candidate) }
                catch (_: IllegalArgumentException) { failed = true; return }
                val attempt = requireNotNull(registry.prepare(value, target, HandoffKind.PERMISSION, manual = false))
                registry.issue(attempt.token); registry.permissionReturned(attempt.token, granted = false)
            }
        }
        onReady(first); onReady(first); onReady(first) // Actual busy/Ready observer re-delivery must not schedule more work.
        assertEquals(1, workers)
        assertNotNull(registry.prepare(first, target, HandoffKind.INSTALL, manual = true))
        onReady(snapshot(2)); assertEquals(2, workers)
        onReady(snapshot(3), corrupt = true); onReady(snapshot(3)); onReady(snapshot(3))
        assertEquals(3, workers) // A real verification failure before offered is also terminal for automatic observer delivery.
    }

    @Test fun fileLeaseSurvivesRecreationWhileCoreCanRetireOldTaskAndCheckAnotherRelease() {
        val store = store(); val registry = UpdateHandoffs(store)
        val directory = temporary.newFolder("verified"); val files = UpdateFiles(directory, registry::protects)
        var saved = UpdateJournal(); var sourceCalls = 0; var task: UpdateTask? = null; var next = snapshot(); val meteredGrants = mutableListOf<Boolean>()
        val journal = object : UpdateJournalStore { override fun read() = saved; override fun write(value: UpdateJournal) { saved = value } }
        val downloads = object : UpdateDownloads {
            override fun destination(token: String) = "file:///owned/$token.apk"
            override fun enqueue(source: String, destination: String, metered: Boolean): Long {
                meteredGrants += metered
                task = UpdateTask(1, source, destination, DownloadPhase.SUCCESS); return 1
            }
            override fun tasks() = listOfNotNull(task)
            override fun open(id: Long) = ByteArrayInputStream(bytes)
            override fun remove(id: Long) { task = null }
        }
        val packages = object : UpdatePackages {
            override val deviceApi = 35
            override fun installed() = target.copy(code = 3)
            override fun archive(file: File) = target
        }
        val core = UpdateController(UpdateSource { sourceCalls++; next }, journal, downloads, files, packages)
        val first = snapshot(); val ready = core.download(first) as UpdateStatus.Ready
        val prepared = requireNotNull(registry.prepare(first, target, HandoffKind.INSTALL, true)); registry.issue(prepared.token)
        val restored = UpdateHandoffs(store); assertTrue(restored.protects(ready.file))
        val unread = ByteArrayInputStream(byteArrayOf(0))
        files.stage(unread, first.checked().first); assertEquals(1, unread.available()); assertArrayEquals(bytes, ready.file.readBytes())
        saved = saved.copy(entry = saved.entry!!.copy(taskId = null)); task = null
        assertEquals(UpdateStatus.Cancelled, core.cancel()); assertNull(saved.entry); assertTrue(ready.file.exists())
        next = snapshot(2)
        assertTrue(core.check(100, manual = true) is UpdateStatus.Available); assertEquals(1, sourceCalls)
        val mobile = allowMeteredForCandidate(true, first.checked().first.identity, next.checked().first.identity)
        assertTrue(core.download(next, metered = mobile, manual = true) is UpdateStatus.Ready)
        assertFalse(meteredGrants.last()); assertTrue(ready.file.exists())
    }

    @Test fun installerReturnAndRecreationKeepLeaseUntilActualMatchingInstalledVersionConfirmsIt() {
        val store = store(); val registry = UpdateHandoffs(store)
        val pending = requireNotNull(registry.prepare(snapshot(), target, HandoffKind.INSTALL, true)); registry.issue(pending.token)
        registry.installerReturned(pending.token)
        val restored = UpdateHandoffs(store)
        assertEquals(HandoffKind.RETURNED, restored.state().active!!.kind)
        assertTrue(restored.confirm(target.copy(code = 3)).isEmpty())
        assertTrue(restored.confirm(target.copy(signers = setOf("b".repeat(64)))).isEmpty())
        assertEquals(1, restored.state().files.size)
        assertEquals(1, restored.confirm(target).size); assertTrue(restored.state().files.isEmpty()); assertNull(restored.state().active)
    }

    @Test fun deniedPermissionKeepsExistingBytesAndManualInstallCanReuseThemWithoutAnotherDownload() {
        val registry = UpdateHandoffs(store()); val files = UpdateFiles(temporary.newFolder(), registry::protects)
        val snapshot = snapshot(); val file = files.stage(ByteArrayInputStream(bytes), snapshot.checked().first)
        val permission = requireNotNull(registry.prepare(snapshot, target, HandoffKind.PERMISSION, true)); registry.issue(permission.token)
        registry.permissionReturned(permission.token, false); assertTrue(file.exists()); assertNull(registry.state().active)
        assertNull(registry.prepare(snapshot, target, HandoffKind.PERMISSION, false))
        files.verify(file, snapshot.checked().first)
        assertNotNull(registry.prepare(snapshot, target, HandoffKind.INSTALL, true)); assertArrayEquals(bytes, file.readBytes())
        registry.endPermissionContinuation()
        val oldSettings = requireNotNull(registry.prepare(snapshot, target, HandoffKind.PERMISSION, true)); registry.issue(oldSettings.token)
        registry.permissionReturned(oldSettings.token, true); assertEquals(HandoffKind.GRANTED, registry.state().active!!.kind)
        registry.endPermissionContinuation(); assertNull(registry.state().active)
        registry.permissionReturned(oldSettings.token, true); assertNull(registry.state().active)
        val newSettings = requireNotNull(registry.prepare(this.snapshot(2), target, HandoffKind.PERMISSION, true)); registry.issue(newSettings.token)
        registry.permissionReturned(oldSettings.token, true)
        assertEquals(newSettings.token, registry.state().active!!.token); assertEquals(HandoffKind.PERMISSION, registry.state().active!!.kind)
    }
}
