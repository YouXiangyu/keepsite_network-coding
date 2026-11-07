package io.github.youxiangyu.keepsite.networkfixture

import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.DocumentsContract
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.ViewModel
import io.github.youxiangyu.keepsite.BuildConfig
import io.github.youxiangyu.keepsite.core.*
import io.github.youxiangyu.keepsite.ui.*
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Request
import org.json.JSONObject
import java.io.Closeable
import java.lang.reflect.Proxy
import java.util.concurrent.Executors

object HostConfiguration { var fontScale = 1f }

class NetworkHostModel : ViewModel() {
    val diagnostics = NetworkDiagnostics()
    var account = diagnostics.openAccount(1)!!
    val state = NetworkViewState()
    var pending: Pair<NetworkReport, NetworkReportFormat>? = null
    var requested = false
    var lastSaved: Boolean? = null
    var lastUri: android.net.Uri? = null
    var backCount = 0
    var snapshotReads = 0
    val samples = org.json.JSONArray()
    val worker = Executors.newSingleThreadExecutor()
    init {
        diagnostics.socket(account, 1, 1)!!.apply { opened(); received("sync.ready", 10, SocketCapabilities(15000, 1)) }
        diagnostics.peer(account, 1)!!.state(true, true, 1, 0)
    }
    override fun onCleared() { worker.shutdown() }
}

/** A separate verification application. Never constructs AppRepository or launches MainActivity. */
class NetworkHostActivity : AppCompatActivity() {
    override fun attachBaseContext(base: android.content.Context) {
        val configuration = android.content.res.Configuration(base.resources.configuration).apply { fontScale = HostConfiguration.fontScale }
        super.attachBaseContext(base.createConfigurationContext(configuration))
    }
    val model: NetworkHostModel by viewModels()
    var page: NetworkMonitorScreen? = null
    private val main = Handler(Looper.getMainLooper())
    val d get() = model.diagnostics
    private val environment get() = NetworkReportEnvironment(BuildConfig.API_BASE_URL, BuildConfig.VERSION_NAME, android.os.Build.VERSION.SDK_INT)
    private val document = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val pending = model.pending; model.pending = null; model.requested = false
        val uri = result.data?.data
        if (result.resultCode == RESULT_OK && uri != null) {
            model.lastUri = uri
            if (pending == null || !pending.first.current()) {
                model.worker.execute { runCatching { DocumentsContract.deleteDocument(contentResolver, uri) }; main.post { model.lastSaved = false } }
            } else {
                val start = android.os.SystemClock.elapsedRealtimeNanos()
                ReportFileJob(model.worker, model.worker, { pending.first.current() }, { contentResolver.openOutputStream(uri, "wt") },
                    { DocumentsContract.deleteDocument(contentResolver, uri); Unit },
                    { stream, cancelled -> pending.first.write(pending.second, stream, cancelled) }, { saved ->
                        val elapsed = android.os.SystemClock.elapsedRealtimeNanos() - start
                        main.post { model.lastSaved = saved
                            model.samples.put(JSONObject().put("scope", "actual SAF new document open/write/close; shared ReportFileJob+report, not AppRepository").put("elapsedNs", elapsed).put("saved", saved)) }
                    }).start()
            }
        }
    }
    private val controller: AppController by lazy {
        Proxy.newProxyInstance(AppController::class.java.classLoader, arrayOf(AppController::class.java)) { _, method, args ->
            val a = args ?: emptyArray()
            when (method.name) {
                "openNetworkPage" -> d.openPage()
                "networkSnapshot" -> { model.snapshotReads++; d.pageSnapshot(a[0] as NetworkPageOwner) }
                "networkDeviceNames" -> if ((a[0] as NetworkPageOwner).current()) listOf("合成授权设备") else emptyList<String>()
                "setNetworkDetailed" -> d.pageDetailed(a[0] as NetworkPageOwner, a[1] as Boolean)
                "pauseNetworkHistory" -> d.pagePause(a[0] as NetworkPageOwner, a[1] as Boolean)
                "clearNetworkHistory" -> d.pageClear(a[0] as NetworkPageOwner)
                "prepareNetworkReport" -> d.prepareReport(a[0] as NetworkPageOwner, environment)
                "refreshServerTrace" -> Unit // This synthetic UI host performs no network I/O.
                "toString" -> "test-only typed network controller"
                else -> throw IllegalStateException("Unexpected business call: ${method.name}")
            }
        } as AppController
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() { landing() }
        })
    }
    override fun onStart() { super.onStart(); mount() }
    override fun onStop() { page?.close(); page = null; super.onStop() }
    fun mount() {
        page?.close()
        page = NetworkMonitorScreen(this, controller, model.state, ::landing, ::export)
        val container = android.widget.FrameLayout(this).apply { addView(page!!.view, android.widget.FrameLayout.LayoutParams(-1, -1)) }
        androidx.core.view.WindowCompat.setDecorFitsSystemWindows(window, false)
        androidx.core.view.ViewCompat.setOnApplyWindowInsetsListener(container) { view, insets ->
            val bars = insets.getInsets(androidx.core.view.WindowInsetsCompat.Type.systemBars() or androidx.core.view.WindowInsetsCompat.Type.ime())
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom); insets
        }
        setContentView(container)
    }
    private fun landing() {
        model.backCount++; page?.close(); page = null
        setContentView(column().apply { add(label("隔离验证入口")); add(action("重新打开网络监控") { mount() }) })
    }
    fun replaceAccount() { d.closeAccount(model.account); model.account = d.openAccount(1)!! }
    fun holdHttp(): Closeable {
        val release = java.util.concurrent.CountDownLatch(1)
        model.worker.execute {
            val origin = "https://keepsite.space".toHttpUrl()
            val request = Request.Builder().url("https://keepsite.space/api/media/" + "a".repeat(64)).build()
            d.http(1, origin, request, true) { _, observation -> observation!!.response(200); observation.read(16); release.await(10, java.util.concurrent.TimeUnit.SECONDS) }
        }
        return Closeable { release.countDown() }
    }
    fun pulse() {
        val request = Request.Builder().url("https://keepsite.space/api/media/" + "a".repeat(64)).build()
        repeat(8) { index -> main.postDelayed({
            d.http(1, "https://keepsite.space".toHttpUrl(), request, true) { _, observation -> observation!!.response(200); observation.read(64) }
        }, (index + 1) * 100L) }
    }
    fun fillHistory() {
        d.setDetailed(true)
        val origin = "https://keepsite.space".toHttpUrl()
        val request = Request.Builder().url("https://keepsite.space/api/media/" + "a".repeat(64)).build()
        repeat(55) { d.http(1, origin, request, true) { _, observation -> observation!!.response(200); observation.read(64) } }
        repeat(20) { val media = d.media(model.account, 1, 64)!!; val attempt = media.attempt(MonitorRoute.CACHE, MonitorProtocol.CACHE)!!
            attempt.read(64); attempt.complete(true); media.validating { Unit }; media.complete(64, MonitorRoute.CACHE) }
        var context: MonitorTraceContext? = null
        d.http(1, origin, request, true) { _, observation ->
            observation!!.response(200); observation.read(64); context = d.monitor.traceContext(observation.operation, MonitorDestination.AUTHENTICATED_API)
        }
        val action = d.prepareServerAction(1, false)!!
        val fixture = assets.open("diagnostics/server-r3-isolated-trace.json").bufferedReader().use { JSONObject(it.readText()) }
        fixture.put("captureId", action.captureId)
        fixture.getJSONArray("events").getJSONObject(0).put("traceId", context!!.traceId).put("coalesced", false)
        action.received(ServerTraceBatch.parse(fixture.toString().toByteArray(), action.captureId)!!)
        val peer = d.peer(model.account, 1)!!; peer.state(true, true, 1, 1)
        peer.live("synthetic-device", 4096, false, null)!!.publish(MonitorValues(bytes = 512, resumedBytes = 256, bufferedBytes = 0,
            controlBytes = 12, discardedBytes = 0, route = MonitorRoute.P2P_UNKNOWN), MonitorStatus.TRANSFERRING)
    }
    fun report() = d.prepareReport(d.openPage()!!, environment)!!
    private fun export(source: NetworkMonitorScreen, report: NetworkReport, format: NetworkReportFormat, share: Boolean) {
        if (page !== source || !source.current() || !report.current()) return
        if (!share) {
            if (model.requested) return
            model.pending = report to format; model.requested = true; model.lastSaved = null
            document.launch(Intent(Intent.ACTION_CREATE_DOCUMENT).apply {
                addCategory(Intent.CATEGORY_OPENABLE); type = format.mime
                putExtra(Intent.EXTRA_TITLE, "n4-network-${System.currentTimeMillis()}.${format.extension}")
                putExtra(DocumentsContract.EXTRA_INITIAL_URI, DocumentsContract.buildDocumentUri(NetworkDocuments.AUTHORITY, "root"))
            })
        } else model.worker.execute {
            val text = report.render(format)
            main.post { if (text != null && page === source && source.current() && report.current()) {
                startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).apply { type = format.mime; putExtra(Intent.EXTRA_TEXT, text) }, "隔离报告系统分享"))
            } }
        }
    }
}
