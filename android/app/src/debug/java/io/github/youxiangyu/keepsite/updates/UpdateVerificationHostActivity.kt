package io.github.youxiangyu.keepsite.updates

import android.content.ClipData
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.Settings
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.core.content.FileProvider
import androidx.lifecycle.Lifecycle
import io.github.youxiangyu.keepsite.BuildConfig
import org.json.JSONObject
import java.io.File
import java.util.UUID

/** Explicit instrumentation host only. No business repository, account, network or automatic launch. */
class UpdateVerificationHostActivity : ComponentActivity() {
    private lateinit var directory: File
    private lateinit var snapshot: UpdateSnapshot
    private lateinit var handoffs: UpdateHandoffs
    private val pending = mutableMapOf<String, HandoffKind>()
    private val requests by lazy { UpdateResultRequests(activityResultRegistry, pending) { token, kind ->
        if (kind == HandoffKind.PERMISSION) handoffs.permissionReturned(token, packageManager.canRequestPackageInstalls())
        else handoffs.installerReturned(token)
        File(directory, "returns.jsonl").appendText(JSONObject().put("token", token).put("kind", kind.name)
            .put("sourceAllowed", packageManager.canRequestPackageInstalls()).put("time", System.currentTimeMillis()).toString() + "\n")
    } }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        require(BuildConfig.DEBUG)
        val run = requireNotNull(intent.getStringExtra("updateVerificationRun"))
        require(UUID.fromString(run).toString() == run)
        directory = File(noBackupFilesDir, "updates-u3/$run").canonicalFile
        val raw = JSONObject(File(directory, "fixture-snapshot.json").readText())
        snapshot = UpdateSnapshot(raw.getString("release"), null).also { it.checked() }
        handoffs = UpdateHandoffs(FileHandoffStore(File(directory, "handoffs.json")))
        savedInstanceState?.getStringArrayList("requests")?.forEach {
            val kind = HandoffKind.valueOf(it.substringBefore(':')); val token = it.substringAfter(':')
            require(kind == HandoffKind.PERMISSION || kind == HandoffKind.INSTALL)
            require(UUID.fromString(token).toString() == token)
            pending[token] = kind
        }
        requests.restore()
        setContentView(TextView(this).apply { text = "keepsite U3 isolated verification\nNo account or automatic update actions" })
    }

    /** Test states exercise the shared guard, not MainActivity's form/draft integration. */
    fun launchUpdate(kind: HandoffKind, draft: Boolean = false, busy: Boolean = false): Boolean {
        require(kind == HandoffKind.PERMISSION || kind == HandoffKind.INSTALL)
        val guard = UpdateInstallGuard(resumed = lifecycle.currentState == Lifecycle.State.RESUMED,
            focused = window.decorView.hasWindowFocus(), draft = draft, busy = busy)
        if (!guard.allows(manual = true)) return false
        val candidate = snapshot.checked().first
        val files = UpdateFiles(File(filesDir, "updates/verified"), handoffs::protects)
        val file = files.candidateFile(candidate)
        files.verify(file, candidate)
        val packages = AndroidUpdatePackages(this)
        val archive = packages.archive(file)
        require(UpdatePolicy.newerArchive(candidate, null, packages.installed(), archive, packages.deviceApi))
        if (kind == HandoffKind.INSTALL) require(packageManager.canRequestPackageInstalls())
        val attempt = requireNotNull(handoffs.prepare(snapshot, archive, kind, manual = true))
        val outgoing = if (kind == HandoffKind.PERMISSION) {
            Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName"))
        } else {
            val uri = FileProvider.getUriForFile(this, "$packageName.updates", file)
            Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION).apply { clipData = ClipData.newRawUri("APK", uri) }
        }
        handoffs.issue(attempt.token)
        try { requests.launch(attempt.token, kind, outgoing) }
        catch (failure: Exception) { requests.failed(attempt.token); handoffs.notLaunched(attempt.token); throw failure }
        return true
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putStringArrayList("requests", ArrayList(pending.map { (token, kind) -> "${kind.name}:$token" }))
        super.onSaveInstanceState(outState)
    }
    override fun onDestroy() { requests.close(); super.onDestroy() }
}
