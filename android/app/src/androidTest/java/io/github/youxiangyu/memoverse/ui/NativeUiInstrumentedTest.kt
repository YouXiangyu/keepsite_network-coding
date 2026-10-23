package io.github.youxiangyu.memoverse.ui

import android.app.UiAutomation
import android.graphics.Bitmap
import android.os.SystemClock
import android.os.Build
import android.view.View
import android.view.ViewGroup
import android.widget.ImageView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.ViewModelProvider
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.*
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.*
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import io.github.youxiangyu.memoverse.BuildConfig
import io.github.youxiangyu.memoverse.MainActivity
import io.github.youxiangyu.memoverse.UiViewModel
import io.github.youxiangyu.memoverse.core.AppController
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.json.JSONObject
import org.json.JSONArray
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Runs only against the owned native-server.py fixture, through real login and repository code. */
@RunWith(AndroidJUnit4::class)
class NativeUiInstrumentedTest {
    private lateinit var scenario: ActivityScenario<MainActivity>
    private lateinit var controller: AppController
    private var rotationControlled = false
    private var backgroundPauseMs = 0L
    private var actualBackgroundPauseMs = 0L

    @Before fun openFixtureApp() {
        assertEquals("UI tests must never use the production service", "http://127.0.0.1:4180", BuildConfig.API_BASE_URL)
        val pauseArgument = InstrumentationRegistry.getArguments().getString("backgroundPauseMs")
        backgroundPauseMs = if (pauseArgument == null) 0 else requireNotNull(pauseArgument.toLongOrNull()) { "backgroundPauseMs must be an integer" }
        require(backgroundPauseMs in 0..10_000) { "backgroundPauseMs must be between 0 and 10000" }
        rotate(UiAutomation.ROTATION_FREEZE_0)
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
                scenario.moveToState(Lifecycle.State.RESUMED)
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
                if (rotationControlled) assertTrue("Release test-owned rotation freezing",
                    InstrumentationRegistry.getInstrumentation().uiAutomation.setRotation(UiAutomation.ROTATION_UNFREEZE))
            }
        }
    }

    private fun await(check: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + 15_000
        while (!check() && SystemClock.elapsedRealtime() < deadline) SystemClock.sleep(40)
        assertTrue("Expected the real native UI transition within 15 seconds", check())
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
        scenario.moveToState(Lifecycle.State.RESUMED)
        awaitWindow()
        awaitHealthy("editor-foreground")
        await { controller.snapshot().busy.isBlank() && controller.snapshot().draft?.fields?.title == "UI-typing-12345" }
        onView(withHint("电影名称")).perform(scrollTo()).check(matches(withText("UI-typing-12345")))
        assertEquals("A local edit must not create or modify a server ticket before save", before, controller.snapshot().tickets.associate { it.id to it.version })
        rotate(UiAutomation.ROTATION_FREEZE_0)
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
        scenario.moveToState(Lifecycle.State.RESUMED)
        awaitWindow()
        awaitHealthy("library-foreground")
        await { imageVisible("${title}封面") }
        onView(withContentDescription("打开${title}详情")).check(matches(hasDescendant(withText("已缓存 · 本机缓存"))))
        onView(withText("设备")).perform(click())
        screenshot("devices-portrait")
        onView(withText("刷新状态")).perform(scrollTo())
        screenshot("devices-bottom")
    }
}
