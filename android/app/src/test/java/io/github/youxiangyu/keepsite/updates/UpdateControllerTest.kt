package io.github.youxiangyu.keepsite.updates

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.ByteArrayInputStream
import java.io.File
import java.io.IOException

class UpdateControllerTest {
    @get:Rule val temporary = TemporaryFolder()
    private val signer = "a".repeat(64)
    private val bytes = "synthetic updated package".toByteArray()
    private fun snapshot(code: Long? = null): UpdateSnapshot {
        val asset = JSONObject().put("id", 1).put("name", "keepsite.apk").put("state", "uploaded").put("size", bytes.size)
            .put("digest", "sha256:${UpdatePolicy.digest(bytes)}")
            .put("browser_download_url", "https://github.com/${UpdatePolicy.REPOSITORY}/releases/download/v0.1/keepsite.apk")
        val assets = JSONArray().put(asset)
        val record = code?.let {
            JSONObject().put("applicationId", UpdatePolicy.PACKAGE).put("versionCode", it).put("versionName", "0.1.0")
                .put("sha256", UpdatePolicy.digest(bytes)).put("bytes", bytes.size).put("certificateSha256", signer).toString()
        }
        if (record != null) assets.put(JSONObject().put("id", 2).put("name", "apk-verification.json").put("state", "uploaded")
            .put("size", record.toByteArray().size).put("digest", "sha256:${UpdatePolicy.digest(record.toByteArray())}")
            .put("browser_download_url", "https://github.com/${UpdatePolicy.REPOSITORY}/releases/download/v0.1/apk-verification.json"))
        return UpdateSnapshot(JSONObject().put("draft", false).put("prerelease", false).put("tag_name", "v0.1")
            .put("assets", assets).toString(), record)
    }
    private inner class Store : UpdateJournalStore {
        var value = UpdateJournal(); var failIdWrite = false
        override fun read() = value
        override fun write(value: UpdateJournal) {
            if (failIdWrite && value.entry?.taskId != null) throw IOException("simulated ID-write crash")
            this.value = value
        }
    }
    private inner class Downloads : UpdateDownloads {
        val values = mutableListOf<UpdateTask>(); val removed = mutableListOf<Long>(); val metered = mutableListOf<Boolean>()
        var enqueues = 0; var opens = 0; var body = bytes; var nullDestination = false
        override fun destination(token: String) = "file:///owned/updates/$token.apk"
        override fun enqueue(source: String, destination: String, metered: Boolean): Long {
            enqueues++; this.metered += metered
            values += UpdateTask(enqueues.toLong(), source, if (nullDestination) null else destination, DownloadPhase.PENDING)
            return enqueues.toLong()
        }
        override fun tasks() = values.toList()
        override fun open(id: Long) = ByteArrayInputStream(body).also { opens++ }
        override fun remove(id: Long) { removed += id; values.removeAll { it.id == id } }
    }
    private inner class Packages : UpdatePackages {
        override val deviceApi = 35
        var installedCode = 3L; var archiveCode = 4L; var archiveSigner = signer; var inspected: File? = null
        override fun installed() = UpdatePolicy.PackageIdentity(UpdatePolicy.PACKAGE, installedCode, setOf(signer))
        override fun archive(file: File): UpdatePolicy.PackageIdentity {
            inspected = file; assertArrayEquals(bytes, file.readBytes())
            return UpdatePolicy.PackageIdentity(UpdatePolicy.PACKAGE, archiveCode, setOf(archiveSigner))
        }
    }
    private inner class Fixture {
        val store = Store(); val downloads = Downloads(); val packages = Packages()
        val files = UpdateFiles(temporary.newFolder()); var calls = 0; var failSource = false; var latest = snapshot()
        fun controller() = UpdateController(UpdateSource { calls++; if (failSource) throw IOException("offline") else latest }, store,
            downloads, files, packages)
    }

    @Test fun successfulCheckCachesCandidateAcrossRecreationButFailedCheckDoesNotStartCooldown() {
        val f = Fixture(); f.failSource = true
        assertThrows(IOException::class.java) { f.controller().check(100) }; assertEquals(0, f.store.value.lastSuccess)
        f.failSource = false; assertTrue(f.controller().check(200) is UpdateStatus.Available)
        assertTrue(f.controller().check(300) is UpdateStatus.Available); assertEquals(2, f.calls)
        f.controller().check(400, manual = true); assertEquals(3, f.calls); assertEquals(0, f.downloads.enqueues)
        assertEquals(UpdateStatus.Cancelled, f.controller().cancel())
        assertEquals(UpdateStatus.Cancelled, f.controller().download(snapshot()))
        assertTrue(f.controller().download(snapshot(), manual = true) is UpdateStatus.Transferring)
    }

    @Test fun enqueueIdCrashWithPendingNullPathWaitsWithoutClaimingOrDuplicating() {
        val f = Fixture(); f.store.failIdWrite = true; f.downloads.nullDestination = true
        assertThrows(IOException::class.java) { f.controller().download(snapshot()) }
        assertNull(f.store.value.entry!!.taskId)
        assertEquals(UpdateStatus.AwaitingOwnership, f.controller().resume())
        assertEquals(UpdateStatus.AwaitingOwnership, f.controller().download(snapshot(), manual = true))
        assertEquals(1, f.downloads.enqueues); assertTrue(f.downloads.removed.isEmpty())
        f.store.failIdWrite = false
        f.downloads.values[0] = f.downloads.values[0].copy(destination = f.store.value.entry!!.destination, source = "https://untrusted.example/package.apk")
        assertEquals(UpdateStatus.AwaitingOwnership, f.controller().resume()); assertNull(f.store.value.entry!!.taskId)
        f.downloads.values[0] = f.downloads.values[0].copy(source = "https://release-assets.githubusercontent.com/redirected-apk")
        assertTrue(f.controller().resume() is UpdateStatus.Transferring)
        assertEquals(1L, f.store.value.entry!!.taskId)
        assertEquals(UpdateStatus.Cancelled, f.controller().cancel()); assertEquals(listOf(1L), f.downloads.removed)
    }

    @Test fun cancelledCandidateStaysSuppressedAndOnlyManualRetryGrantsMeteredForItsNewTask() {
        val f = Fixture(); f.downloads.nullDestination = true
        assertTrue(f.controller().download(snapshot()) is UpdateStatus.Transferring)
        assertEquals(1L, f.store.value.entry!!.taskId)
        assertEquals(UpdateStatus.Cancelled, f.controller().cancel())
        assertEquals(listOf(1L), f.downloads.removed)
        assertEquals(UpdateStatus.Cancelled, f.controller().download(snapshot()))
        assertThrows(IllegalArgumentException::class.java) { f.controller().download(snapshot(), metered = true) }
        f.controller().download(snapshot(), metered = true, manual = true)
        assertEquals(listOf(false, true), f.downloads.metered); assertEquals(2, f.downloads.enqueues)
        assertTrue(f.store.value.entry!!.metered)
        f.controller().cancel(); assertNull(f.store.value.entry)
        f.latest = snapshot(5)
        assertTrue(f.controller().check(100, manual = true) is UpdateStatus.Available)
        assertTrue(f.controller().download(f.latest) is UpdateStatus.Transferring)
    }

    @Test fun unknownPendingTaskCannotBeCancelledOrReplacedBySourceUrlGuess() {
        val f = Fixture(); f.store.failIdWrite = true; f.downloads.nullDestination = true
        assertThrows(IOException::class.java) { f.controller().download(snapshot()) }; f.store.failIdWrite = false
        assertEquals(UpdateStatus.AwaitingOwnership, f.controller().cancel())
        assertTrue(f.store.value.entry!!.cancelled)
        assertEquals(UpdateStatus.AwaitingOwnership, f.controller().download(snapshot(), manual = true))
        assertEquals(UpdateStatus.AwaitingOwnership, f.controller().retry())
        assertTrue(f.downloads.removed.isEmpty()); assertEquals(1, f.downloads.enqueues)
    }

    @Test fun completedTaskStagesPrivateCopyBeforeActualIdentityAndRechecksInstalledVersion() {
        val f = Fixture(); f.downloads.nullDestination = true; f.controller().download(snapshot())
        f.downloads.values[0] = f.downloads.values[0].copy(phase = DownloadPhase.SUCCESS)
        assertEquals(UpdateStatus.AwaitingOwnership, f.controller().resume()); assertEquals(0, f.downloads.opens)
        f.downloads.values[0] = f.downloads.values[0].copy(destination = f.store.value.entry!!.destination)
        val ready = f.controller().resume() as UpdateStatus.Ready
        assertEquals(ready.file, f.packages.inspected); assertTrue(f.store.value.entry!!.ready)
        assertEquals(1, f.downloads.opens)
        f.packages.installedCode = 4
        assertEquals(UpdateStatus.Current, f.controller().resume()); assertEquals(1, f.downloads.opens)
        assertNull(f.store.value.entry); assertFalse(ready.file.exists()); assertEquals(listOf(1L), f.downloads.removed)
        f.latest = snapshot(5)
        val next = f.controller().check(100, manual = true) as UpdateStatus.Available
        assertEquals(5L, next.snapshot.checked().second!!.code); assertEquals(1, f.calls)
        assertTrue(f.controller().check(101) is UpdateStatus.Available); assertEquals(1, f.calls)
    }

    @Test fun corruptedDownloadAndWrongActualSignerNeverPublishReady() {
        val f = Fixture(); f.controller().download(snapshot())
        f.downloads.values[0] = f.downloads.values[0].copy(phase = DownloadPhase.SUCCESS)
        f.downloads.body = bytes + byteArrayOf(0)
        assertThrows(IllegalArgumentException::class.java) { f.controller().resume() }
        f.downloads.body = ByteArray(bytes.size)
        assertThrows(IllegalArgumentException::class.java) { f.controller().resume() }
        assertNull(f.packages.inspected); assertFalse(f.store.value.entry!!.ready)
        assertFalse(f.files.candidateFile(snapshot().checked().first).exists())
        f.downloads.body = bytes; f.packages.archiveSigner = "b".repeat(64)
        assertThrows(IllegalArgumentException::class.java) { f.controller().resume() }; assertFalse(f.store.value.entry!!.ready)
        f.packages.archiveSigner = signer
        assertTrue(f.controller().retry() is UpdateStatus.Transferring); assertEquals(2, f.downloads.enqueues)
        f.downloads.values[0] = f.downloads.values[0].copy(phase = DownloadPhase.FAILED)
        assertTrue(f.controller().resume() is UpdateStatus.Failed)
        assertTrue(f.controller().retry() is UpdateStatus.Transferring); assertEquals(3, f.downloads.enqueues)
    }

    @Test fun journalRoundTripRevalidatesSnapshotAndRetainsCorruptRecordForRecovery() {
        val file = File(temporary.newFolder(), "journal.json"); val journal = FileUpdateJournal(file)
        val snapshot = snapshot()
        val entry = UpdateEntry(snapshot, "00000000-0000-0000-0000-000000000001", "file:///owned/token.apk", metered = true)
        journal.write(UpdateJournal(100, entry, snapshot)); assertEquals(entry, journal.read().entry)
        val value = JSONObject(file.readText()); val badRelease = JSONObject(value.getJSONObject("entry").getString("release"))
        badRelease.getJSONArray("assets").getJSONObject(0).put("digest", "unverifiable")
        value.getJSONObject("entry").put("release", badRelease.toString()); file.writeText(value.toString())
        val corrupted = file.readBytes()
        assertThrows(IllegalArgumentException::class.java) { journal.read() }; assertArrayEquals(corrupted, file.readBytes())
    }
}
