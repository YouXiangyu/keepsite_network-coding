package io.github.youxiangyu.keepsite.ui

import android.app.UiAutomation
import android.content.Intent
import android.graphics.Bitmap
import android.os.SystemClock
import android.os.Build
import android.view.View
import android.view.ViewGroup
import android.view.autofill.AutofillManager
import android.widget.ImageView
import android.widget.EditText
import android.widget.TextView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.ViewModelProvider
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.graphics.ColorUtils
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.Espresso.pressBack
import androidx.test.espresso.action.ViewActions.*
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.*
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.runner.lifecycle.ActivityLifecycleCallback
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
import io.github.youxiangyu.keepsite.BuildConfig
import io.github.youxiangyu.keepsite.MainActivity
import io.github.youxiangyu.keepsite.R
import io.github.youxiangyu.keepsite.UiViewModel
import io.github.youxiangyu.keepsite.core.AppController
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.json.JSONObject
import org.json.JSONArray
import java.io.File
import java.io.Closeable
import java.lang.ref.WeakReference
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

/** Runs only against the owned native-server.py fixture, through real login and repository code. */
@RunWith(AndroidJUnit4::class)
class NativeUiInstrumentedTest {
    private lateinit var scenario: ActivityScenario<MainActivity>
    private lateinit var controller: AppController
    private var rotationControlled = false
    private var backgroundPauseMs = 0L
    private var actualBackgroundPauseMs = 0L
    private val isolateFixtureAutofill = ActivityLifecycleCallback { activity, stage ->
        if (activity is MainActivity && stage == Stage.CREATED) {
            // Keep synthetic credentials out of the device password manager, including
            // after rotation/recreation. This flag lives only on this test Activity.
            activity.window.decorView.importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS
            activity.getSystemService(AutofillManager::class.java)?.cancel()
        }
    }

    @Before fun openFixtureApp() {
        assertEquals("UI tests must never use the production service", "http://127.0.0.1:4180", BuildConfig.API_BASE_URL)
        val pauseArgument = InstrumentationRegistry.getArguments().getString("backgroundPauseMs")
        backgroundPauseMs = if (pauseArgument == null) 0 else requireNotNull(pauseArgument.toLongOrNull()) { "backgroundPauseMs must be an integer" }
        require(backgroundPauseMs in 0..10_000) { "backgroundPauseMs must be between 0 and 10000" }
        rotate(UiAutomation.ROTATION_FREEZE_0)
        ActivityLifecycleMonitorRegistry.getInstance().addLifecycleCallback(isolateFixtureAutofill)
        val automation = InstrumentationRegistry.getInstrumentation().uiAutomation
        // Some physical devices abort the instrumented background launch before
        // onCreate, leaving startActivitySync waiting. Borrow only this permission
        // for launch; all application interactions run under the normal identity.
        if (Build.VERSION.SDK_INT >= 29) {
            automation.adoptShellPermissionIdentity("android.permission.START_ACTIVITIES_FROM_BACKGROUND")
        }
        try { scenario = ActivityScenario.launch(MainActivity::class.java) }
        finally { if (Build.VERSION.SDK_INT >= 29) automation.dropShellPermissionIdentity() }
        scenario.onActivity { controller = ViewModelProvider(it)[UiViewModel::class.java].controller }
        awaitWindow()
        await { controller.snapshot().session != null }
        if (controller.snapshot().session?.authenticated == true) {
            scenario.onActivity { controller.logout() }
            await { controller.snapshot().session?.authenticated == false && controller.snapshot().busy.isBlank() }
        }
    }

    @After fun closeFixtureApp() {
        try {
            if (!::scenario.isInitialized) return
            if (::controller.isInitialized && controller.snapshot().session?.authenticated == true) {
                foregroundFixtureActivity()
                if (controller.snapshot().draft?.pendingConfirmation == false) {
                    scenario.onActivity { controller.discardDraft() }
                    await { controller.snapshot().draft == null }
                }
                scenario.onActivity { controller.logout() }
                await { controller.snapshot().busy.isBlank() && controller.snapshot().session?.authenticated == false }
            }
        } finally {
            try { if (::scenario.isInitialized) scenario.close() }
            finally {
                try {
                    if (rotationControlled) assertTrue("Release test-owned rotation freezing",
                        InstrumentationRegistry.getInstrumentation().uiAutomation.setRotation(UiAutomation.ROTATION_UNFREEZE))
                } finally {
                    ActivityLifecycleMonitorRegistry.getInstance().removeLifecycleCallback(isolateFixtureAutofill)
                }
            }
        }
    }

    private fun await(timeoutMs: Long = 15_000, check: () -> Boolean) {
        require(timeoutMs in 1..40_000)
        val deadline = SystemClock.elapsedRealtime() + timeoutMs
        while (!check() && SystemClock.elapsedRealtime() < deadline) SystemClock.sleep(40)
        assertTrue("Expected the real native UI transition within $timeoutMs milliseconds", check())
    }
    private fun login() {
        onView(withHint("邮箱")).perform(scrollTo(), replaceText("alice@example.test"), closeSoftKeyboard())
        onView(withHint("密码")).perform(scrollTo(), replaceText("TestTicket123!"), closeSoftKeyboard())
        onView(withText("登录")).perform(scrollTo(), click())
        await {
            val state = controller.snapshot()
            state.busy.isBlank() && (state.session?.authenticated == true && state.ready && state.tickets.size >= 2 || state.error.isNotBlank())
        }
        assertTrue("Real fixture login failed: ${controller.snapshot().error}", controller.snapshot().session?.authenticated == true && controller.snapshot().ready)
        if (controller.snapshot().draft != null) {
            scenario.onActivity { controller.discardDraft() }
            await { controller.snapshot().draft == null }
        }
    }
    private fun screenshot(name: String) {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        instrumentation.waitForIdleSync()
        if (Build.VERSION.SDK_INT >= 29) {
            repeat(2) {
                val frame = CountDownLatch(1)
                scenario.onActivity { activity ->
                    activity.window.decorView.viewTreeObserver.registerFrameCommitCallback { frame.countDown() }
                    activity.window.decorView.invalidate()
                }
                assertTrue("Capture only after the hardware frame has been submitted", frame.await(5, TimeUnit.SECONDS))
            }
        }
        val target = File(instrumentation.targetContext.getExternalFilesDir("m7-ui"), "$name.png")
        target.parentFile!!.mkdirs()
        val bitmap = instrumentation.uiAutomation.takeScreenshot()
        assertNotNull("The instrumented device must produce a real screenshot", bitmap)
        target.outputStream().use { assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)) }
        bitmap.recycle()
    }
    private fun awaitWindow() = await {
        var ready = false
        scenario.onActivity { ready = it.window.decorView.hasWindowFocus() && it.window.decorView.isLaidOut }
        ready
    }
    private fun foregroundFixtureActivity() {
        lateinit var previous: WeakReference<MainActivity>
        lateinit var originalIntent: Intent
        var taskId = -1
        scenario.onActivity {
            previous = WeakReference(it)
            originalIntent = Intent(it.intent)
            taskId = it.taskId
        }
        // Resume through ActivityScenario without delivering a new launch Intent,
        // which would make it ignore subsequent lifecycle events, including destroy.
        scenario.moveToState(Lifecycle.State.RESUMED)
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        instrumentation.waitForIdleSync()
        scenario.onActivity {
            assertSame("Foreground restoration must reuse the existing Activity", previous.get(), it)
            assertEquals("Foreground restoration must retain the task", taskId, it.taskId)
            assertTrue("Foreground restoration must preserve the launch intent", originalIntent.filterEquals(it.intent))
        }
    }
    private fun awaitKeyboard() = await {
        var visible = false
        scenario.onActivity { visible = ViewCompat.getRootWindowInsets(it.window.decorView)?.isVisible(WindowInsetsCompat.Type.ime()) == true }
        visible
    }
    private fun rotate(rotation: Int) {
        val applied = InstrumentationRegistry.getInstrumentation().uiAutomation.setRotation(rotation)
        if (applied) rotationControlled = true
        assertTrue("UiAutomation must apply the test device rotation", applied)
    }
    private fun awaitHealthy(name: String, startedAt: Long = SystemClock.elapsedRealtime()) {
        val errors = linkedSetOf<String>()
        var healthySince = 0L
        var recovered = false
        val deadline = SystemClock.elapsedRealtime() + 15_000
        while (SystemClock.elapsedRealtime() < deadline) {
            val state = controller.snapshot()
            if (state.error.isNotBlank()) errors.add(state.error)
            if (state.connected && state.ready && state.error.isBlank()) {
                if (healthySince == 0L) healthySince = SystemClock.elapsedRealtime()
                if (SystemClock.elapsedRealtime() - healthySince >= 500) { recovered = true; break }
            } else healthySince = 0
            SystemClock.sleep(40)
        }
        val last = controller.snapshot()
        val record = JSONObject().put("phase", name).put("recoveredWithoutRefresh", recovered)
            .put("elapsedMs", SystemClock.elapsedRealtime() - startedAt).put("connected", last.connected)
            .put("ready", last.ready).put("error", last.error).put("observedErrors", JSONArray(errors.toList()))
        if (name == "library-foreground") record.put("backgroundPauseMs", backgroundPauseMs)
            .put("actualBackgroundPauseMs", actualBackgroundPauseMs)
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        File(context.getExternalFilesDir("m7-ui"), "recovery-$name.json").writeText(record.toString(2))
        if (!recovered) screenshot("$name-unrecovered")
        assertTrue("$name must recover automatically: ${last.error}", recovered)
    }
    private fun descendants(view: View): Sequence<View> = sequence {
        yield(view)
        if (view is ViewGroup) for (index in 0 until view.childCount) yieldAll(descendants(view.getChildAt(index)))
    }
    private fun imageVisible(description: String): Boolean {
        var visible = false
        scenario.onActivity { activity ->
            visible = descendants(activity.window.decorView).filterIsInstance<ImageView>()
                .any { it.contentDescription == description && it.drawable != null && it.isShown }
        }
        return visible
    }
    private fun failedMediaVisible(description: String): Boolean {
        var visible = false
        val failures = setOf("图片下载失败，请重新检查。", "Owned test media gate expired")
        scenario.onActivity { activity ->
            visible = descendants(activity.window.decorView).filterIsInstance<ImageView>().any { image ->
                image.contentDescription == description && image.drawable == null && image.isShown &&
                    (image.parent as? ViewGroup)?.let { card ->
                        descendants(card).filterIsInstance<TextView>().any { it.isShown && it.text.toString() in failures }
                    } == true
            }
        }
        return visible
    }
    private class MediaGate(private val token: String) : Closeable {
        private val origin = "http://127.0.0.1:4180"
        private val client = OkHttpClient.Builder().connectTimeout(5, TimeUnit.SECONDS)
            .readTimeout(5, TimeUnit.SECONDS).callTimeout(10, TimeUnit.SECONDS)
            .followRedirects(false).retryOnConnectionFailure(false).build()
        fun request(path: String, body: JSONObject? = null): JSONObject {
            require(path in listOf("/__test/media-gate", "/__test/progress"))
            val request = Request.Builder().url(origin + path).header("Origin", origin)
                .header("Connection", "close")
                .header("X-Keepsite-Fixture", "native-browser-fixture")
                .header("X-Keepsite-Fixture-Run", token)
            if (body != null) request.post(body.toString().toRequestBody("application/json".toMediaType()))
            return client.newCall(request.build()).execute().use { response ->
                assertEquals("Owned media gate HTTP status", 200, response.code)
                val raw = response.body?.string() ?: throw AssertionError("Missing media gate response")
                assertTrue("Media gate response must remain bounded", raw.length <= 256 * 1024)
                JSONObject(raw)
            }
        }
        fun action(assetId: String, action: String) {
            request("/__test/media-gate", JSONObject().put("assetId", assetId).put("action", action))
        }
        override fun close() {
            client.dispatcher.cancelAll(); client.connectionPool.evictAll(); client.dispatcher.executorService.shutdown()
        }
    }

    @Test fun mutedLabelsMeetContrastOnNormalAndSoftSurfaces() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val muted = context.getColor(R.color.mv_muted)
        for (surface in listOf(android.R.color.white, R.color.mv_soft)) {
            val contrast = ColorUtils.calculateContrast(muted, context.getColor(surface))
            assertTrue("Small muted labels need at least 4.5:1 contrast; measured $contrast", contrast >= 4.5)
        }
    }

    @Test fun explicitRefreshRecoversFailedCoverWithoutReplacingSearchOrMutatingTickets() {
        val token = InstrumentationRegistry.getArguments().getString("runToken")
            ?.takeIf { Regex("[a-f0-9]{32}").matches(it) }
            ?: throw AssertionError("The media recovery test requires its owned fixture runToken")
        login()
        val ticket = controller.snapshot().tickets.first { it.fields.title.isNotBlank() }
        val query = ticket.fields.title
        val description = "${query}封面"
        val versions = controller.snapshot().tickets.associate { it.id to it.version }
        val peerEnabled = controller.snapshot().peer.enabled
        onView(withHint("搜索电影票")).perform(replaceText(query), closeSoftKeyboard())
            .check(matches(withText(query)))
        await { imageVisible(description) }
        MediaGate(token).use { gate ->
            try {
                // Leave the image-bearing pages to release their leases before evicting the account cache.
                onView(withContentDescription("我的")).perform(click())
                onView(withText("同步与设备")).perform(scrollTo(), click())
                scenario.onActivity { controller.setPeerEnabled(false); controller.clearCache() }
                await { controller.snapshot().busy.isBlank() && controller.snapshot().cache.files == 0 }
                assertEquals("", controller.snapshot().cache.error)
                try {
                    gate.action(ticket.cover.assetId, "hold")
                    onView(withContentDescription("票夹")).perform(click())
                    onView(withHint("搜索电影票")).check(matches(withText(query)))
                    await {
                        val started = gate.request("/__test/progress").getJSONArray("gateStarted")
                        (0 until started.length()).any { started.getString(it) == ticket.cover.assetId }
                    }
                    // HttpApi reads time out after 20s; the owned gate also expires after 30s.
                    await(40_000) { failedMediaVisible(description) }
                    assertFalse("A failed cover must not retain a drawable", imageVisible(description))
                    lateinit var searchBeforeRefresh: EditText
                    scenario.onActivity { activity ->
                        searchBeforeRefresh = descendants(activity.window.decorView).filterIsInstance<EditText>()
                            .first { it.hint?.toString() == "搜索电影票" }
                        assertTrue("Library search must provide at least a 44dp touch target",
                            searchBeforeRefresh.height >= activity.dp(44))
                    }
                    val progress = gate.request("/__test/progress")
                    assertFalse("Request evidence must not overflow", progress.getBoolean("mediaRequestsOverflow"))
                    val failedSequence = progress.getLong("mediaRequestCount")
                    gate.action(ticket.cover.assetId, "release")
                    onView(withContentDescription("刷新票夹")).perform(click())
                    await { imageVisible(description) }
                    await { controller.snapshot().busy.isBlank() && controller.snapshot().ready }
                    onView(withHint("搜索电影票")).check(matches(withText(query)))
                    scenario.onActivity { activity ->
                        val searchAfterRefresh = descendants(activity.window.decorView).filterIsInstance<EditText>()
                            .first { it.hint?.toString() == "搜索电影票" }
                        assertSame("Explicit refresh must preserve the library search view", searchBeforeRefresh, searchAfterRefresh)
                    }
                    await {
                        val requests = gate.request("/__test/progress").getJSONArray("mediaRequests")
                        (0 until requests.length()).any { index ->
                            val row = requests.getJSONObject(index)
                            row.getLong("sequence") > failedSequence && row.getString("assetId") == ticket.cover.assetId &&
                                row.getString("client") == "android" && row.optInt("status") == 200 && row.getBoolean("finished")
                        }
                    }
                    assertEquals("Media retry must not mutate ticket metadata", versions,
                        controller.snapshot().tickets.associate { it.id to it.version })
                } finally { gate.action(ticket.cover.assetId, "release") }
            } finally { scenario.onActivity { controller.setPeerEnabled(peerEnabled) } }
        }
    }

    @Test fun failedLoginPreservesTheEnteredCredentials() {
        onView(withHint("邮箱")).perform(scrollTo(), replaceText("alice@example.test"), closeSoftKeyboard())
        onView(withHint("密码")).perform(scrollTo(), replaceText("WrongTicket123!"), closeSoftKeyboard())
        onView(withText("登录")).perform(scrollTo(), click())
        await { controller.snapshot().error.isNotBlank() && controller.snapshot().busy.isBlank() }
        assertFalse(controller.snapshot().session!!.authenticated)
        onView(withHint("邮箱")).check(matches(withText("alice@example.test")))
        onView(withHint("密码")).check(matches(withText("WrongTicket123!")))
        screenshot("login-error")
    }

    @Test fun editingSurvivesRotationAndBackgroundWithoutRevertingTypedFields() {
        login()
        val ticket = controller.snapshot().tickets.first()
        val before = controller.snapshot().tickets.associate { it.id to it.version }
        val title = ticket.fields.title.ifBlank { "未命名电影票" }
        onView(withContentDescription("${title}封面")).perform(click())
        onView(withText("编辑电影票")).perform(scrollTo(), click())
        await { controller.snapshot().draft?.ticketId == ticket.id }
        // OEM Chinese IMEs may compose ASCII hardware key events. Set the prefix
        // deterministically, then exercise successive user edits with digit keys.
        onView(withHint("电影名称")).perform(scrollTo(), replaceText("UI-typing-"), click(), typeText("12345"))
            .check(matches(withText("UI-typing-12345")))
        awaitKeyboard()
        onView(withHint("电影名称")).check(matches(isCompletelyDisplayed()))
        screenshot("editor-keyboard")
        onView(withHint("电影名称")).perform(closeSoftKeyboard())
        await { controller.snapshot().draft?.fields?.title == "UI-typing-12345" }
        val rotationStarted = SystemClock.elapsedRealtime()
        rotate(UiAutomation.ROTATION_FREEZE_90)
        await { var landscape = false; scenario.onActivity { landscape = it.resources.configuration.orientation == android.content.res.Configuration.ORIENTATION_LANDSCAPE }; landscape }
        awaitWindow()
        awaitHealthy("rotation", rotationStarted)
        onView(withHint("电影名称")).perform(scrollTo()).check(matches(withText("UI-typing-12345")))
        screenshot("editor-landscape")
        onView(withHint("电影名称")).perform(click())
        awaitKeyboard()
        onView(withHint("电影名称")).check(matches(isCompletelyDisplayed()))
        screenshot("editor-landscape-keyboard")
        onView(withHint("电影名称")).perform(closeSoftKeyboard())
        scenario.recreate()
        awaitWindow()
        awaitHealthy("recreate")
        onView(withHint("电影名称")).perform(scrollTo()).check(matches(withText("UI-typing-12345")))
        scenario.moveToState(Lifecycle.State.CREATED)
        foregroundFixtureActivity()
        awaitWindow()
        awaitHealthy("editor-foreground")
        await { controller.snapshot().busy.isBlank() && controller.snapshot().draft?.fields?.title == "UI-typing-12345" }
        onView(withHint("电影名称")).perform(scrollTo()).check(matches(withText("UI-typing-12345")))
        assertEquals("A local edit must not create or modify a server ticket before save", before, controller.snapshot().tickets.associate { it.id to it.version })
        lateinit var landscapeActivity: WeakReference<MainActivity>
        scenario.onActivity { landscapeActivity = WeakReference(it) }
        rotate(UiAutomation.ROTATION_FREEZE_0)
        await {
            var portraitRecreated = false
            scenario.onActivity {
                portraitRecreated = it !== landscapeActivity.get() &&
                    it.resources.configuration.orientation == android.content.res.Configuration.ORIENTATION_PORTRAIT
            }
            portraitRecreated
        }
        awaitWindow()
        onView(withHint("电影名称")).perform(scrollTo()).check(matches(withText("UI-typing-12345")))
    }

    @Test fun returningFromBackgroundRebindsVisibleCachedImages() {
        login()
        val title = controller.snapshot().tickets.first().fields.title.ifBlank { "未命名电影票" }
        await { imageVisible("${title}封面") }
        screenshot("library-portrait")
        scenario.moveToState(Lifecycle.State.CREATED)
        val backgroundStarted = SystemClock.elapsedRealtime()
        await { !controller.snapshot().peer.registered }
        if (backgroundPauseMs > 0) SystemClock.sleep(backgroundPauseMs)
        actualBackgroundPauseMs = SystemClock.elapsedRealtime() - backgroundStarted
        foregroundFixtureActivity()
        awaitWindow()
        awaitHealthy("library-foreground")
        await { imageVisible("${title}封面") }
        onView(withContentDescription("打开${title}详情")).check(matches(hasDescendant(withText("已缓存 · 本机缓存"))))
        onView(withContentDescription("我的")).perform(click())
        screenshot("mine-portrait")
        val tickets = controller.snapshot().tickets
        val monthKey = tickets.maxOf { Presentation.month(it.fields.showtime, it.createdAt) }
        val inMonth = tickets.filter { Presentation.month(it.fields.showtime, it.createdAt) == monthKey }
        val month = Presentation.monthLabel(monthKey)
        val monthTitle = inMonth.first().fields.title.ifBlank { "未命名电影票" }
        onView(withContentDescription("查看${month}的${inMonth.size}张电影票")).perform(scrollTo(), click())
        onView(withText("${inMonth.size} 张")).check(matches(isDisplayed()))
        onView(withContentDescription("打开${monthTitle}详情")).perform(click())
        onView(withContentDescription("返回$month")).check(matches(isDisplayed()))
        onView(withContentDescription("我的")).check(matches(isSelected()))
        pressBack()
        onView(withText("${inMonth.size} 张")).check(matches(isDisplayed()))
        pressBack()
        onView(withText("同步与设备")).perform(scrollTo(), click())
        onView(withText("刷新状态")).perform(scrollTo())
        screenshot("sync-bottom")
    }
}
