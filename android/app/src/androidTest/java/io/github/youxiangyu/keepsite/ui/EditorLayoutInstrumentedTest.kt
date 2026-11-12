package io.github.youxiangyu.keepsite.ui

import android.content.pm.ActivityInfo
import android.content.Intent
import android.content.res.Configuration
import android.graphics.Bitmap
import android.graphics.Rect
import android.os.Build
import android.os.SystemClock
import android.view.View
import android.view.ViewGroup
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.*
import androidx.test.espresso.matcher.ViewMatchers.*
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import io.github.youxiangyu.keepsite.BuildConfig
import io.github.youxiangyu.keepsite.EditorLayoutHostActivity
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TestName
import org.junit.runner.RunWith
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class EditorLayoutInstrumentedTest {
    @get:Rule val testName = TestName()
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val runId = InstrumentationRegistry.getArguments().getString("evidenceRunId")
        ?.also { require(it.matches(Regex("[A-Za-z0-9_-]{1,80}"))) } ?: java.util.UUID.randomUUID().toString()
    private var captureSequence = 0
    private fun launch(largeText: Boolean = false, longHint: Boolean = false): ActivityScenario<EditorLayoutHostActivity> {
        if (Build.VERSION.SDK_INT >= 29) instrumentation.uiAutomation.adoptShellPermissionIdentity("android.permission.START_ACTIVITIES_FROM_BACKGROUND")
        val intent = Intent(instrumentation.targetContext, EditorLayoutHostActivity::class.java).putExtra("largeText", largeText).putExtra("longHint", longHint)
        return try { ActivityScenario.launch<EditorLayoutHostActivity>(intent) }
        finally { if (Build.VERSION.SDK_INT >= 29) instrumentation.uiAutomation.dropShellPermissionIdentity() }
    }
    private fun await(scenario: ActivityScenario<EditorLayoutHostActivity>, scene: String = "layout", check: (EditorLayoutHostActivity) -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + 20_000
        while (true) {
            var ready = false; scenario.onActivity { ready = check(it) }
            if (ready) return
            try { assertTrue("Layout/IME condition did not settle within 20 seconds", SystemClock.elapsedRealtime() < deadline) }
            catch (failure: AssertionError) {
                try { capture(scenario, scene, failed = true) } catch (captureFailure: Throwable) { failure.addSuppressed(captureFailure) }
                throw failure
            }
            SystemClock.sleep(40)
        }
    }
    private fun complete(view: View): Boolean {
        val visible = Rect(); val location = IntArray(2); view.getLocationOnScreen(location)
        return view.getGlobalVisibleRect(visible) && visible.contains(Rect(location[0], location[1], location[0] + view.width, location[1] + view.height))
    }
    private fun box(view: View): JSONObject {
        val location = IntArray(2); view.getLocationOnScreen(location)
        return JSONObject().put("x", location[0]).put("y", location[1]).put("width", view.width).put("height", view.height).put("complete", complete(view))
    }
    private fun touchJson(fact: EditorLayoutHostActivity.TouchFact): JSONObject {
        fun button(value: EditorLayoutHostActivity.TouchButton) = JSONObject().put("x", value.x).put("y", value.y)
            .put("width", value.width).put("height", value.height).put("enabled", value.enabled).put("clicks", value.clicks)
        return JSONObject().put("phase", fact.phase)
            .put("action", when (fact.action) { 0 -> "DOWN"; 1 -> "UP"; 3 -> "CANCEL"; else -> fact.action.toString() })
            .put("downTime", fact.downTime).put("eventTime", fact.eventTime).put("uptime", fact.uptime)
            .put("rawX", fact.rawX).put("rawY", fact.rawY).put("localX", fact.localX).put("localY", fact.localY)
            .put("save", button(fact.save)).put("cancel", button(fact.cancel)).put("focusedField", fact.focusedField)
            .put("focusedClass", fact.focusedClass ?: JSONObject.NULL).put("imeVisible", fact.imeVisible).put("imeBottom", fact.imeBottom)
            .put("windowFocus", fact.windowFocus).put("handled", fact.handled ?: JSONObject.NULL)
            .put("dispatchFailure", fact.dispatchFailure ?: JSONObject.NULL)
    }
    private fun capture(scenario: ActivityScenario<EditorLayoutHostActivity>, name: String, failed: Boolean = false) {
        instrumentation.waitForIdleSync()
        val drawn = CountDownLatch(1)
        scenario.onActivity { activity ->
            val decor = activity.window.decorView
            if (Build.VERSION.SDK_INT >= 29 && decor.isHardwareAccelerated) {
                // Text/layout readiness precedes the render thread submitting its frame.
                decor.viewTreeObserver.registerFrameCommitCallback { drawn.countDown() }
                decor.invalidate()
            } else {
                decor.postOnAnimation { decor.postOnAnimation { drawn.countDown() } }
                decor.invalidate()
            }
        }
        assertTrue("Updated window frame was not committed", drawn.await(20, TimeUnit.SECONDS))
        instrumentation.waitForIdleSync()
        val directory = File(instrumentation.targetContext.getExternalFilesDir(null), "editor-layout-evidence/$runId/${testName.methodName}")
        check(directory.isDirectory || directory.mkdirs())
        var data = JSONObject()
        scenario.onActivity { activity ->
            val decor = activity.window.decorView
            val insets = ViewCompat.getRootWindowInsets(decor)
            val visibleFrame = Rect().also(decor::getWindowVisibleDisplayFrame)
            fun inset(type: Int): JSONObject? = insets?.getInsets(type)?.let {
                JSONObject().put("left", it.left).put("top", it.top).put("right", it.right).put("bottom", it.bottom)
            }
            data = JSONObject().put("scene", name).put("scope", "Isolated layout Host; local controls, no business save")
                .put("runId", runId).put("testName", testName.methodName).put("failed", failed)
                .put("wallMillis", System.currentTimeMillis()).put("elapsedRealtimeMs", SystemClock.elapsedRealtime())
                .put("sourceBase", InstrumentationRegistry.getArguments().getString("sourceBase"))
                .put("sourceSnapshot", InstrumentationRegistry.getArguments().getString("sourceSnapshot"))
                .put("appApkSha256", InstrumentationRegistry.getArguments().getString("appApkSha256"))
                .put("testApkSha256", InstrumentationRegistry.getArguments().getString("testApkSha256"))
                .put("apiBaseUrl", BuildConfig.API_BASE_URL).put("sdk", Build.VERSION.SDK_INT)
                .put("orientation", activity.resources.configuration.orientation).put("fontScale", activity.resources.configuration.fontScale)
                .put("density", activity.resources.displayMetrics.density).put("densityDpi", activity.resources.displayMetrics.densityDpi)
                .put("windowFocus", activity.hasWindowFocus()).put("lifecycle", activity.lifecycle.currentState.name)
                .put("decor", box(decor)).put("content", box(activity.editor.root.parent as View))
                .put("windowVisibleFrame", JSONObject().put("left", visibleFrame.left).put("top", visibleFrame.top).put("right", visibleFrame.right).put("bottom", visibleFrame.bottom))
                .put("systemBarsInsets", inset(WindowInsetsCompat.Type.systemBars()))
                .put("imeInsets", inset(WindowInsetsCompat.Type.ime()))
                .put("focusedField", activity.fields.indexOfFirst { it === activity.currentFocus })
                .put("fieldMetrics", JSONArray(activity.fields.map { JSONObject().put("textSizePx", it.textSize).put("inputType", it.inputType).put("imeOptions", it.imeOptions) }))
                .put("editorFontScale", activity.fields.first().resources.configuration.fontScale)
                .put("imeVisible", ViewCompat.getRootWindowInsets(activity.window.decorView)?.isVisible(WindowInsetsCompat.Type.ime()) == true)
                .put("scrollY", activity.editor.scroll.scrollY).put("scroll", box(activity.editor.scroll)).put("body", box(activity.editor.body))
                .put("footer", box(activity.editor.footer)).put("root", box(activity.editor.root)).put("status", box(activity.status))
                .put("notice", box(activity.notice)).put("noticeText", activity.notice.text).put("statusText", activity.status.text)
                .put("noticeSource", activity.noticeSource)
                .put("busy", activity.busyState).put("pending", activity.pendingState)
                .put("progress", box(activity.progress)).put("progressVisibility", activity.progress.visibility)
                .put("noticeTextSizePx", activity.notice.textSize)
                .put("noticeInsideScroll", activity.notice.parent === activity.editor.body)
                .put("save", box(activity.save)).put("cancel", box(activity.cancel)).put("date", box(activity.date))
                .put("saveLabel", activity.save.text).put("saveEnabled", activity.save.isEnabled).put("cancelEnabled", activity.cancel.isEnabled)
                .put("dateEnabled", activity.date.isEnabled).put("photoEnabled", activity.photo.isEnabled)
                .put("fieldsEnabled", JSONArray(activity.fields.map { it.isEnabled }))
                .put("fields", JSONArray(activity.fields.map(::box))).put("saveClicks", activity.saveClicks).put("cancelClicks", activity.cancelClicks)
                .put("hostInstance", activity.diagnosticHostId)
                .put("touchCoordinateSpace", "raw=display screen; local=Activity.dispatchTouchEvent window coordinates; button bounds=display screen")
                .put("touchRecordsEvicted", activity.touchRecordsEvicted).put("touchRecordingErrors", activity.touchRecordingErrors)
                .put("touchEvents", JSONArray(activity.touchDiagnosticSnapshot().map(::touchJson)))
        }
        val stem = "${++captureSequence}-$name-o${data.getInt("orientation")}-${if (failed) "failure" else "reached"}"
        val jsonFile = File(directory, "$stem.json"); val imageFile = File(directory, "$stem.png")
        check(jsonFile.createNewFile() && imageFile.createNewFile()) { "Evidence already exists; never overwrite" }
        jsonFile.writeText(data.toString(2))
        val screenshot = checkNotNull(instrumentation.uiAutomation.takeScreenshot())
        try { imageFile.outputStream().use { assertTrue(screenshot.compress(Bitmap.CompressFormat.PNG, 100, it)) } }
        finally { screenshot.recycle() }
    }
    private fun portrait(scenario: ActivityScenario<EditorLayoutHostActivity>) {
        var previousHost = ""
        var recreated = false
        scenario.onActivity {
            previousHost = it.diagnosticHostId
            recreated = it.resources.configuration.orientation != Configuration.ORIENTATION_PORTRAIT
            it.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_PORTRAIT
        }
        await(scenario) {
            it.resources.configuration.orientation == Configuration.ORIENTATION_PORTRAIT && it.hasWindowFocus() &&
                it.editor.scroll.height > 0 && (!recreated || it.diagnosticHostId != previousHost)
        }
    }

    private fun bounds(view: View): Rect {
        val location = IntArray(2); view.getLocationOnScreen(location)
        return Rect(location[0], location[1], location[0] + view.width, location[1] + view.height)
    }
    private fun descendants(view: View): Sequence<View> = sequence {
        yield(view)
        if (view is ViewGroup) for (index in 0 until view.childCount) yieldAll(descendants(view.getChildAt(index)))
    }
    private fun checkLayout(activity: EditorLayoutHostActivity) {
        assertSame(activity.editor.root, activity.editor.scroll.parent)
        assertSame(activity.editor.root, activity.editor.footer.parent)
        assertTrue(activity.editor.scroll.height > 0)
        assertTrue(bounds(activity.editor.scroll).bottom <= bounds(activity.editor.footer).top)
        assertTrue(bounds(activity.editor.root).contains(bounds(activity.editor.footer)))
        assertTrue(complete(activity.save)); assertTrue(complete(activity.cancel))
        for (button in listOf(activity.save, activity.cancel)) {
            assertTrue(button.height >= activity.dp(48))
            assertEquals(1, descendants(activity.editor.root).count { it is android.widget.Button && it.text.toString() == button.text.toString() })
            assertEquals(0, button.layout.getEllipsisCount(button.layout.lineCount - 1))
        }
        if (activity.notice.visibility == View.VISIBLE) {
            assertSame(activity.editor.body, activity.notice.parent)
            assertEquals(0, activity.editor.body.indexOfChild(activity.notice))
            assertNull("Error text must not be truncated", activity.notice.ellipsize)
        }
    }
    private fun checkState(activity: EditorLayoutHostActivity, busy: Boolean, pending: Boolean) {
        assertEquals(!busy, activity.save.isEnabled)
        assertTrue(activity.cancel.isEnabled)
        assertEquals(if (pending) "核对并重试保存" else "保存电影票", activity.save.text.toString())
        assertTrue(activity.fields.all { it.isEnabled == (!busy && !pending) })
        assertEquals(!busy && !pending, activity.date.isEnabled)
        assertEquals(!busy && !pending, activity.photo.isEnabled)
        assertEquals(if (busy) View.VISIBLE else View.GONE, activity.progress.visibility)
    }
    private fun emptyActionsUseFullContent(scenario: ActivityScenario<EditorLayoutHostActivity>) {
        lateinit var empty: TicketEditorLayout; lateinit var parent: ViewGroup
        scenario.onActivity { activity ->
            parent = activity.editor.root.parent as ViewGroup; parent.removeView(activity.editor.root)
            empty = TicketEditorLayout(activity); parent.addView(empty.root, ViewGroup.LayoutParams(-1, -1))
        }
        await(scenario) { empty.root.height > 0 && empty.scroll.height == empty.root.height }
        scenario.onActivity { activity ->
            assertEquals(View.GONE, empty.footer.visibility); assertEquals(0, empty.footer.height)
            parent.removeView(empty.root); parent.addView(activity.editor.root, ViewGroup.LayoutParams(-1, -1))
        }
        await(scenario) { complete(it.save) && complete(it.cancel) }
    }
    private fun field(scenario: ActivityScenario<EditorLayoutHostActivity>, name: String, scene: String) {
        onView(withHint(name)).perform(scrollTo(), click())
        var previousInset = -1; var stableInsets = 0
        await(scenario, scene) { activity ->
            val input = activity.fields.first { it.hint.toString() == name }
            val insets = ViewCompat.getRootWindowInsets(activity.window.decorView)
            val bottom = insets?.getInsets(WindowInsetsCompat.Type.ime())?.bottom ?: 0
            stableInsets = if (bottom == previousInset) stableInsets + 1 else 0; previousInset = bottom
            activity.currentFocus === input && complete(input) && complete(activity.save) && complete(activity.cancel) &&
                insets?.isVisible(WindowInsetsCompat.Type.ime()) == true && bottom > 0 && stableInsets >= 4
        }
        var saves = 0; var cancels = 0; var hostBefore = ""
        scenario.onActivity { activity ->
            checkLayout(activity)
            assertTrue(bounds(activity.editor.scroll).contains(bounds(activity.fields.first { it.hint.toString() == name })))
            saves = activity.saveClicks; cancels = activity.cancelClicks
            hostBefore = activity.diagnosticHostId
        }
        capture(scenario, scene)
        try {
            onView(withText("保存电影票")).perform(click())
            onView(withText("取消")).perform(click())
            scenario.onActivity {
                val clickContext = "$scene hostBefore=$hostBefore hostNow=${it.diagnosticHostId} saveBefore=$saves saveActual=${it.saveClicks} cancelBefore=$cancels cancelActual=${it.cancelClicks}"
                assertEquals("$clickContext save callback must increment exactly once", saves + 1, it.saveClicks)
                assertEquals("$clickContext cancel callback must increment exactly once", cancels + 1, it.cancelClicks)
            }
        } catch (failure: Throwable) {
            try { capture(scenario, scene + "-actions", failed = true) }
            catch (captureFailure: Throwable) { if (captureFailure !== failure) failure.addSuppressed(captureFailure) }
            throw failure
        }
        onView(withHint(name)).perform(closeSoftKeyboard())
        await(scenario) { ViewCompat.getRootWindowInsets(it.window.decorView)?.isVisible(WindowInsetsCompat.Type.ime()) == false }
    }

    @Test fun fixedActionsStayVisibleWhileContentScrolls() {
        launch().use { scenario ->
            portrait(scenario)
            emptyActionsUseFullContent(scenario)
            scenario.onActivity { activity ->
                checkLayout(activity)
                assertEquals(View.GONE, TicketEditorLayout(activity).footer.visibility)
                assertEquals(0, activity.editor.scroll.scrollY)
                assertTrue(activity.editor.body.height > activity.editor.scroll.height)
            }
            capture(scenario, "portrait-top")
            var fixed = Rect(); scenario.onActivity { fixed = bounds(it.editor.footer); it.editor.scroll.scrollTo(0, it.editor.body.height / 2) }
            await(scenario) { it.editor.scroll.scrollY > 0 }
            scenario.onActivity { checkLayout(it); assertEquals(fixed, bounds(it.editor.footer)) }
            capture(scenario, "portrait-middle")
            scenario.onActivity { it.editor.scroll.fullScroll(View.FOCUS_DOWN) }
            await(scenario) { it.editor.scroll.scrollY > 0 && complete(it.save) && complete(it.cancel) && complete(it.date) }
            capture(scenario, "portrait-end")
            scenario.onActivity { checkLayout(it); assertEquals(fixed, bounds(it.editor.footer)) }
            onView(withText("保存电影票")).perform(click())
            onView(withText("取消")).perform(click())
            scenario.onActivity { assertEquals(1, it.saveClicks); assertEquals(1, it.cancelClicks) }
            capture(scenario, "local-actions")
            scenario.onActivity { it.renderState(error = "保存未完成，草稿仍保留。") }
            await(scenario) { complete(it.notice) && complete(it.save) && complete(it.cancel) }
            capture(scenario, "save-error-revealed")
            scenario.onActivity { activity ->
                val parent = activity.editor.root.parent as ViewGroup
                val notice = android.widget.TextView(activity)
                parent.addView(notice, ViewGroup.LayoutParams(1, 1))
                val params = notice.layoutParams
                val index = parent.indexOfChild(notice)
                val placement = activity.editor.attachNotice(notice)
                assertSame(activity.editor.body, notice.parent)
                placement.close(); placement.close()
                assertSame(parent, notice.parent)
                assertSame(params, notice.layoutParams)
                assertEquals(index, parent.indexOfChild(notice))
                parent.removeView(notice)
            }
        }
    }

    @Test fun fixedActionsWithRealKeyboardAndRecreation() {
        launch().use { scenario ->
            portrait(scenario)
            scenario.onActivity { it.renderState(error = "保存未完成，草稿仍保留。") }
            await(scenario) { !it.notice.isLayoutRequested && complete(it.notice) && it.editor.scroll.scrollY == 0 }
            field(scenario, "电影名称", "portrait-title-ime")
            field(scenario, "座位", "portrait-seat-ime")
            scenario.recreate()
            await(scenario) { it.hasWindowFocus() && it.editor.scroll.height > 0 }
            scenario.onActivity { checkLayout(it); assertEquals("保存未完成，草稿仍保留。", it.notice.text.toString()) }
            capture(scenario, "portrait-recreated")
        }
    }

    @Test fun largeEditorTextAndLongHintRemainReachable() {
        launch(largeText = true, longHint = true).use { scenario ->
            portrait(scenario)
            await(scenario) { complete(it.notice) }
            capture(scenario, "large-error-readable")
            scenario.onActivity { assertEquals(2f, it.fields.first().resources.configuration.fontScale, 0.01f); checkLayout(it); it.editor.scroll.fullScroll(View.FOCUS_DOWN) }
            await(scenario) { complete(it.status) && complete(it.date) }
            capture(scenario, "large-text-long-hint")
            scenario.onActivity { it.renderState(pending = true); checkState(it, busy = false, pending = true) }
            await(scenario) { it.save.layout?.text?.toString() == it.save.text.toString() && complete(it.save) }
            scenario.onActivity { checkLayout(it) }
            capture(scenario, "large-pending-action")
            var failedSave = ""
            scenario.onActivity {
                failedSave = it.notice.text.toString()
                // A labelled save clears the previous error; a subsequent failure restores feedback.
                it.renderState(error = "", busy = "正在保存"); checkState(it, busy = true, pending = false); checkLayout(it)
            }
            capture(scenario, "large-busy-action")
            onView(withText("取消")).perform(click())
            scenario.onActivity { assertEquals(1, it.cancelClicks); it.renderState(error = failedSave); checkState(it, busy = false, pending = false) }
            await(scenario) { !it.notice.isLayoutRequested && complete(it.notice) && it.editor.scroll.scrollY == 0 }
            field(scenario, "电影名称", "large-portrait-title-ime")
            field(scenario, "座位", "large-portrait-seat-ime")
        }
    }
}
