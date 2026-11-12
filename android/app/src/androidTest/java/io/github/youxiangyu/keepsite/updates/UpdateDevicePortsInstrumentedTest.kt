package io.github.youxiangyu.keepsite.updates

import android.content.Intent
import android.graphics.Bitmap
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Process
import android.os.SystemClock
import android.view.KeyEvent
import android.view.accessibility.AccessibilityNodeInfo
import androidx.core.content.FileProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.security.MessageDigest
import java.util.UUID

/** Opt-in staged device checks. Default suite never downloads, launches a host or opens system UI. */
@RunWith(AndroidJUnit4::class)
class UpdateDevicePortsInstrumentedTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context get() = instrumentation.targetContext
    private val arguments get() = InstrumentationRegistry.getArguments()
    private val devSigner = "256859f8bdb20ff0fe68d567435e099b7b6cfe1ffc2fc7bfaac4cbfaa5817dab"
    private val publicSigner = "a13e743fd6754fddbe15c96e602d6bec10c6ba85019ea3c73488df25a802ca04"
    private fun phase(name: String): File {
        assumeTrue("Explicit U3 phase required", arguments.getString("keepsiteUpdatePhase") == name)
        val run = requireNotNull(arguments.getString("keepsiteUpdateRun"))
        require(UUID.fromString(run).toString() == run)
        val packages = AndroidUpdatePackages(context)
        assertEquals(UpdatePolicy.PACKAGE, context.packageName)
        assertEquals(setOf(devSigner), packages.installed().signers)
        assertEquals(3L, packages.installed().code)
        return File(context.noBackupFilesDir, "updates-u3/$run").canonicalFile.also { require(it.isDirectory) }
    }
    private fun main(block: () -> Unit) {
        var failure: Throwable? = null
        instrumentation.runOnMainSync { try { block() } catch (error: Throwable) { failure = error } }
        failure?.let { throw it }
    }
    private fun await(label: String, predicate: () -> Boolean) {
        val end = SystemClock.elapsedRealtime() + 15_000
        while (!predicate() && SystemClock.elapsedRealtime() < end) SystemClock.sleep(100)
        assertTrue(label, predicate())
    }
    private fun evidence(directory: File, name: String, value: JSONObject) {
        require(name.matches(Regex("[a-z-]+")))
        File(directory, "$name.json").writeText(value.put("time", System.currentTimeMillis()).put("pid", Process.myPid()).toString(2))
    }
    private fun journal(directory: File) = FileUpdateJournal(File(directory, "download.json"))
    private fun publicSnapshot(directory: File): UpdateSnapshot {
        val raw = JSONObject(File(directory, "public-snapshot.json").readText())
        return UpdateSnapshot(raw.getString("release"), raw.getString("verification")).also {
            val (candidate, record) = it.checked()
            // Pinned reviewed public artifact. A new release requires a new reviewed device plan.
            assertEquals(56_264_457L, candidate.apk.bytes)
            assertEquals("2a7dcedff63017eb10b7e24a440e16a930721ac337d9045f42d5951d1cfb1262", candidate.apk.sha256)
            assertEquals(3L, requireNotNull(record).code)
            assertEquals(publicSigner, record.certificate)
        }
    }
    private fun ownedTask(directory: File): Pair<UpdateEntry, UpdateTask> {
        val entry = requireNotNull(journal(directory).read().entry)
        val downloads = AndroidUpdateDownloads(context)
        assertEquals(downloads.destination(entry.token), entry.destination)
        val tasks = downloads.tasks()
        auditTasks(directory, entry.destination, tasks, "observed")
        val task = tasks.single { it.id == entry.taskId }
        assertTrue(trustedUpdateDownloadUri(task.source))
        task.destination?.let { assertEquals(entry.destination, it) }
        return entry to task
    }
    private fun auditTasks(directory: File, destination: String, tasks: List<UpdateTask>, stage: String) {
        val value = JSONObject().put("time", System.currentTimeMillis()).put("stage", stage).put("destination", destination)
            .put("tasks", JSONArray(tasks.map { JSONObject().put("id", it.id).put("source", it.source).put("destination", it.destination ?: JSONObject.NULL)
                .put("phase", it.phase.name).put("bytes", it.downloaded) }))
            .put("matchingIds", JSONArray(tasks.filter { it.destination == destination }.map { it.id }))
        File(directory, "dm-ids.jsonl").appendText(value.toString() + "\n")
    }
    private fun taskJson(entry: UpdateEntry, task: UpdateTask) = JSONObject().put("id", task.id).put("token", entry.token)
        .put("destination", entry.destination).put("localUri", task.destination ?: JSONObject.NULL).put("phase", task.phase.name)
        .put("bytes", task.downloaded).put("total", task.total).put("source", task.source).put("reason", task.reason)

    @Suppress("DEPRECATION")
    @Test fun enqueuePublicWhileOffline() {
        val directory = phase("A-setup")
        val snapshot = publicSnapshot(directory)
        val network = context.getSystemService(ConnectivityManager::class.java)
        assertFalse("Disconnect all eligible unmetered networks before setup", network.allNetworks.any {
            network.getNetworkCapabilities(it)?.let { caps -> caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) &&
                caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED) } == true
        })
        assertNull("Never overwrite another setup", journal(directory).read().entry)
        val downloads = AndroidUpdateDownloads(context)
        val token = UUID.randomUUID().toString()
        val intent = UpdateEntry(snapshot, token, downloads.destination(token))
        auditTasks(directory, intent.destination, downloads.tasks(), "before-enqueue")
        journal(directory).write(UpdateJournal(entry = intent))
        val id = downloads.enqueue(snapshot.checked().first.apk.url, intent.destination, metered = false)
        journal(directory).write(UpdateJournal(entry = intent.copy(taskId = id)))
        val (entry, task) = ownedTask(directory)
        assertTrue(task.phase == DownloadPhase.PENDING || task.phase == DownloadPhase.PAUSED)
        assertEquals(0L, task.downloaded)
        evidence(directory, "a-queued", taskJson(entry, task).put("scope", "real DM port; code3 is already current"))
        // The outer owned-device runner ends instrumentation, Home/am-kill, locks and restores Wi-Fi.
    }

    @Test fun resumeSamePublicDownload() {
        val directory = phase("A-resume")
        val snapshot = publicSnapshot(directory)
        val (entry, task) = ownedTask(directory)
        assertEquals(snapshot, entry.snapshot)
        assertEquals(DownloadPhase.SUCCESS, task.phase)
        assertEquals(entry.destination, task.destination)
        val candidate = snapshot.checked().first
        assertEquals(candidate.apk.bytes, task.downloaded)
        val digest = MessageDigest.getInstance("SHA-256")
        var bytes = 0L
        AndroidUpdateDownloads(context).open(task.id).use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) { val count = input.read(buffer); if (count < 0) break; bytes += count; digest.update(buffer, 0, count) }
        }
        assertEquals(candidate.apk.bytes, bytes)
        assertEquals(candidate.apk.sha256, UpdatePolicy.hex(digest.digest()))
        evidence(directory, "a-resumed", taskJson(entry, task).put("sha256", candidate.apk.sha256))
    }

    @Test fun rejectPublicArchiveSignedByAnotherCertificate() {
        val directory = phase("B-archive")
        val (entry, task) = ownedTask(directory)
        assertEquals(DownloadPhase.SUCCESS, task.phase)
        val (candidate, record) = entry.snapshot.checked()
        val files = UpdateFiles(File(directory, "public-verified"))
        val file = AndroidUpdateDownloads(context).open(task.id).use { files.stage(it, candidate) }
        files.verify(file, candidate)
        val packages = AndroidUpdatePackages(context)
        val archive = packages.archive(file)
        assertEquals(setOf(publicSigner), archive.signers)
        assertEquals(3L, archive.code)
        var rejected = false
        try { UpdatePolicy.newerArchive(candidate, record, packages.installed(), archive, packages.deviceApi) }
        catch (failure: IllegalArgumentException) { rejected = failure.message == "安装包与当前应用签名不同。" }
        assertTrue("Must reject the actual differing signer before any installation Intent", rejected)
        evidence(directory, "b-rejected", JSONObject().put("archiveCode", archive.code).put("archiveSigner", publicSigner)
            .put("installedSigner", devSigner).put("installationIntentCreated", false))
    }

    private fun fixture(directory: File): Pair<UpdateSnapshot, File> {
        val input = File(directory, "fixture.apk")
        val metadata = JSONObject(File(directory, "fixture-input.json").readText())
        val bytes = metadata.getLong("bytes"); val hash = metadata.getString("sha256")
        assertEquals(bytes, input.length())
        val assetId = (UUID.fromString(directory.name).mostSignificantBits and Long.MAX_VALUE).coerceAtLeast(1)
        val asset = JSONObject().put("id", assetId).put("name", "keepsite-u3.apk").put("state", "uploaded")
            .put("size", bytes).put("digest", "sha256:$hash")
            .put("browser_download_url", "https://github.com/${UpdatePolicy.REPOSITORY}/releases/download/u3-offline-fixture/keepsite-u3.apk")
        val snapshot = UpdateSnapshot(JSONObject().put("draft", false).put("prerelease", false).put("tag_name", "u3-offline-fixture")
            .put("assets", JSONArray().put(asset)).toString(), null)
        val candidate = snapshot.checked().first
        val files = UpdateFiles(File(context.filesDir, "updates/verified"))
        val target = files.candidateFile(candidate)
        val ownRecord = File(directory, "fixture-snapshot.json")
        if (ownRecord.exists()) assertEquals(snapshot.release, JSONObject(ownRecord.readText()).getString("release"))
        else {
            assertFalse("Never overwrite a pre-existing provider copy", target.exists())
            ownRecord.writeText(JSONObject().put("release", snapshot.release).put("ownedFile", target.canonicalPath).toString())
        }
        if (target.exists()) files.verify(target, candidate) else input.inputStream().use { files.stage(it, candidate) }
        val packages = AndroidUpdatePackages(context)
        val archive = packages.archive(target)
        assertEquals(packages.installed().code + 1, archive.code)
        assertEquals(setOf(devSigner), archive.signers)
        assertTrue(UpdatePolicy.newerArchive(candidate, null, packages.installed(), archive, packages.deviceApi))
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.updates", target)
        context.contentResolver.openInputStream(uri)!!.use { assertTrue(it.read() >= 0) }
        val provider = requireNotNull(context.packageManager.resolveContentProvider("${context.packageName}.updates", 0))
        assertFalse(provider.exported); assertTrue(provider.grantUriPermissions)
        var escaped = false
        try { FileProvider.getUriForFile(context, "${context.packageName}.updates", File(directory, "fixture.apk")) }
        catch (_: IllegalArgumentException) { escaped = true }
        assertTrue("Unverified input/journal must be outside provider roots", escaped)
        return snapshot to target
    }
    private fun host(directory: File): UpdateVerificationHostActivity {
        val intent = Intent(context, UpdateVerificationHostActivity::class.java)
            .putExtra("updateVerificationRun", directory.name).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val host = instrumentation.startActivitySync(intent) as UpdateVerificationHostActivity
        await("Verification host is resumed and focused") { host.hasWindowFocus() }
        return host
    }
    private fun returnCount(directory: File) = File(directory, "returns.jsonl").let { if (it.exists()) it.readLines().size else 0 }
    private fun capture(directory: File, name: String) {
        val bitmap = requireNotNull(instrumentation.uiAutomation.takeScreenshot())
        try { File(directory, "$name.png").outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) } }
        finally { bitmap.recycle() }
        val rows = mutableListOf<String>()
        fun visit(node: AccessibilityNodeInfo?) {
            if (node == null) return
            rows += "${node.packageName}|${node.viewIdResourceName}|${node.text}|checked=${node.isChecked}|checkable=${node.isCheckable}|clickable=${node.isClickable}|enabled=${node.isEnabled}"
            for (index in 0 until node.childCount) visit(node.getChild(index))
        }
        visit(instrumentation.uiAutomation.rootInActiveWindow)
        File(directory, "$name.txt").writeText(rows.joinToString("\n"))
    }
    private fun sourceSwitch(node: AccessibilityNodeInfo?): AccessibilityNodeInfo? {
        if (node == null) return null
        if (node.packageName?.toString() == "com.android.settings" && node.isCheckable && node.isClickable && node.isEnabled &&
            (0 until node.childCount).any { node.getChild(it)?.text?.toString() == "Allow from this source" }) return node
        for (index in 0 until node.childCount) sourceSwitch(node.getChild(index))?.let { return it }
        return null
    }
    private fun hasText(node: AccessibilityNodeInfo?, text: String, operable: Boolean = false): Boolean {
        if (node == null) return false
        if (node.text?.toString() == text && (!operable || node.isEnabled && node.isClickable)) return true
        return (0 until node.childCount).any { hasText(node.getChild(it), text, operable) }
    }
    private fun setSourcePermission(expected: Boolean) {
        if (context.packageManager.canRequestPackageInstalls() == expected) return
        val control = requireNotNull(sourceSwitch(instrumentation.uiAutomation.rootInActiveWindow)) { "Expected source-only permission switch missing; stop for inspection" }
        assertEquals(!expected, control.isChecked)
        val clickable = if (control.isClickable) control else requireNotNull(control.parent).also { assertTrue(it.isClickable) }
        assertTrue(clickable.performAction(AccessibilityNodeInfo.ACTION_CLICK))
        await("Only this app's source permission changed") { context.packageManager.canRequestPackageInstalls() == expected }
    }

    @Test fun sourcePermissionRoundTripThroughRealRegistry() {
        val directory = phase("C-permission")
        require(arguments.getString("allowSourceToggle") == "yes")
        fixture(directory)
        val initiallyAllowed = context.packageManager.canRequestPackageInstalls()
        val baseline = File(directory, "source-permission-before.json")
        if (!baseline.exists()) baseline.writeText(JSONObject().put("allowed", initiallyAllowed).toString())
        val host = host(directory)
        try {
            val count = returnCount(directory)
            main { assertTrue(host.launchUpdate(HandoffKind.PERMISSION)) }
            await("Source permission settings") { instrumentation.uiAutomation.rootInActiveWindow?.packageName?.toString() == "com.android.settings" }
            await("Source permission row is ready") { sourceSwitch(instrumentation.uiAutomation.rootInActiveWindow) != null }
            capture(directory, "c-source-settings")
            setSourcePermission(true)
            instrumentation.sendKeyDownUpSync(KeyEvent.KEYCODE_BACK)
            await("Original permission result delivered") { returnCount(directory) == count + 1 }
            val state = FileHandoffStore(File(directory, "handoffs.json")).read()
            assertEquals(HandoffKind.GRANTED, state.active!!.kind)
            evidence(directory, "c-permission-returned", JSONObject().put("before", initiallyAllowed).put("after", true).put("kind", state.active!!.kind.name))
        } finally { main { host.finish() } }
    }

    @Test fun openSameSignerHigherCodeConfirmationAndCancel() {
        val directory = phase("C-install-cancel")
        val (snapshot, file) = fixture(directory)
        assertTrue("Run explicit source permission stage first", context.packageManager.canRequestPackageInstalls())
        val packages = AndroidUpdatePackages(context); val before = packages.installed()
        val host = host(directory)
        try {
            val count = returnCount(directory)
            main {
                assertFalse(host.launchUpdate(HandoffKind.INSTALL, draft = true))
                assertFalse(host.launchUpdate(HandoffKind.INSTALL, busy = true))
                assertTrue(host.launchUpdate(HandoffKind.INSTALL))
            }
            await("System package confirmation page") { instrumentation.uiAutomation.rootInActiveWindow?.packageName?.toString() in
                setOf("com.android.packageinstaller", "com.google.android.packageinstaller", "com.android.permissioncontroller", "com.google.android.permissioncontroller") }
            await("Actual update question and operable Update/Cancel controls, after staging") {
                val root = instrumentation.uiAutomation.rootInActiveWindow
                hasText(root, "keepsite U3C TEST - CANCEL ONLY") && hasText(root, "Do you want to update this app?") &&
                    hasText(root, "Update", operable = true) && hasText(root, "Cancel", operable = true)
            }
            capture(directory, "c-confirmation-before-cancel")
            assertTrue("Must be the reviewed offline fixture confirmation, not an error page",
                File(directory, "c-confirmation-before-cancel.txt").readText().contains("keepsite U3C TEST - CANCEL ONLY"))
            val leases = UpdateHandoffs(FileHandoffStore(File(directory, "handoffs.json")))
            assertTrue(leases.protects(file)); assertTrue(leases.state().files.single().possiblyRead)
            instrumentation.sendKeyDownUpSync(KeyEvent.KEYCODE_BACK)
            await("Original installer result delivered after cancel") { returnCount(directory) == count + 1 }
            assertEquals(before, packages.installed())
            assertEquals(HandoffKind.RETURNED, leases.state().active!!.kind)
            assertTrue(leases.protects(file)); UpdateFiles(file.parentFile!!, leases::protects).remove(snapshot.checked().first)
            assertTrue("Possibly-read copy remains after cancellation", file.exists())
            evidence(directory, "c-cancelled", JSONObject().put("installedCodeBefore", before.code).put("installedCodeAfter", packages.installed().code)
                .put("signer", devSigner).put("kind", leases.state().active!!.kind.name).put("protectedCopyRetained", file.exists()))
        } finally { main { host.finish() } }
    }

    @Test fun restoreOriginalSourcePermission() {
        val directory = phase("C-restore-source")
        require(arguments.getString("allowSourceToggle") == "yes")
        val expected = JSONObject(File(directory, "source-permission-before.json").readText()).getBoolean("allowed")
        val host = host(directory)
        try {
            val count = returnCount(directory)
            main { assertTrue(host.launchUpdate(HandoffKind.PERMISSION)) }
            await("Source permission settings") { instrumentation.uiAutomation.rootInActiveWindow?.packageName?.toString() == "com.android.settings" }
            await("Source permission row is ready") { sourceSwitch(instrumentation.uiAutomation.rootInActiveWindow) != null }
            setSourcePermission(expected)
            instrumentation.sendKeyDownUpSync(KeyEvent.KEYCODE_BACK)
            await("Permission restoration result delivered") { returnCount(directory) == count + 1 }
            assertEquals(expected, context.packageManager.canRequestPackageInstalls())
            evidence(directory, "c-source-restored", JSONObject().put("allowed", expected))
        } finally { main { host.finish() } }
    }
}
