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
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class EditorLayoutInstrumentedTest {
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private fun launch(largeText: Boolean = false, longHint: Boolean = false): ActivityScenario<EditorLayoutHostActivity> {
        if (Build.VERSION.SDK_INT >= 29) instrumentation.uiAutomation.adoptShellPermissionIdentity("android.permission.START_ACTIVITIES_FROM_BACKGROUND")
        val intent = Intent(instrumentation.targetContext, EditorLayoutHostActivity::class.java).putExtra("largeText", largeText).putExtra("longHint", longHint)
        return try { ActivityScenario.launch<EditorLayoutHostActivity>(intent) }
        finally { if (Build.VERSION.SDK_INT >= 29) instrumentation.uiAutomation.dropShellPermissionIdentity() }
    }
    private fun await(scenario: ActivityScenario<EditorLayoutHostActivity>, check: (EditorLayoutHostActivity) -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + 20_000
        while (true) {
            var ready = false; scenario.onActivity { ready = check(it) }
            if (ready) return
            assertTrue("Layout/IME condition did not settle within 20 seconds", SystemClock.elapsedRealtime() < deadline)
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
    private fun capture(scenario: ActivityScenario<EditorLayoutHostActivity>, name: String) {
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
        val directory = File(instrumentation.targetContext.getExternalFilesDir(null), "editor-layout-evidence").apply { mkdirs() }
        var data = JSONObject()
        scenario.onActivity { activity ->
            data = JSONObject().put("scene", name).put("scope", "Isolated layout Host; local controls, no business save")
                .put("sourceBase", InstrumentationRegistry.getArguments().getString("sourceBase"))
                .put("sourceSnapshot", InstrumentationRegistry.getArguments().getString("sourceSnapshot"))
                .put("apiBaseUrl", BuildConfig.API_BASE_URL).put("sdk", Build.VERSION.SDK_INT)
                .put("orientation", activity.resources.configuration.orientation).put("fontScale", activity.resources.configuration.fontScale)
                .put("editorFontScale", activity.fields.first().resources.configuration.fontScale)
                .put("imeVisible", ViewCompat.getRootWindowInsets(activity.window.decorView)?.isVisible(WindowInsetsCompat.Type.ime()) == true)
                .put("scrollY", activity.editor.scroll.scrollY).put("scroll", box(activity.editor.scroll)).put("body", box(activity.editor.body))
                .put("footer", box(activity.editor.footer)).put("root", box(activity.editor.root)).put("status", box(activity.status))
                .put("save", box(activity.save)).put("cancel", box(activity.cancel)).put("date", box(activity.date))
                .put("saveLabel", activity.save.text).put("saveEnabled", activity.save.isEnabled).put("cancelEnabled", activity.cancel.isEnabled)
                .put("fields", JSONArray(activity.fields.map(::box))).put("saveClicks", activity.saveClicks).put("cancelClicks", activity.cancelClicks)
        }
        File(directory, "$name.json").writeText(data.toString(2))
        val screenshot = checkNotNull(instrumentation.uiAutomation.takeScreenshot())
        try { File(directory, "$name.png").outputStream().use { assertTrue(screenshot.compress(Bitmap.CompressFormat.PNG, 100, it)) } }
        finally { screenshot.recycle() }
    }
    private fun portrait(scenario: ActivityScenario<EditorLayoutHostActivity>) {
        scenario.onActivity { it.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_PORTRAIT }
        await(scenario) { it.resources.configuration.orientation == Configuration.ORIENTATION_PORTRAIT && it.hasWindowFocus() && it.editor.scroll.height > 0 }
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
        await(scenario) { activity ->
            val input = activity.fields.first { it.hint.toString() == name }
            val insets = ViewCompat.getRootWindowInsets(activity.window.decorView)
            val bottom = insets?.getInsets(WindowInsetsCompat.Type.ime())?.bottom ?: 0
            stableInsets = if (bottom == previousInset) stableInsets + 1 else 0; previousInset = bottom
            activity.currentFocus === input && complete(input) && complete(activity.save) && complete(activity.cancel) &&
                insets?.isVisible(WindowInsetsCompat.Type.ime()) == true && bottom > 0 && stableInsets >= 4
        }
        var saves = 0; var cancels = 0
        scenario.onActivity { activity ->
            checkLayout(activity)
            assertTrue(bounds(activity.editor.scroll).contains(bounds(activity.fields.first { it.hint.toString() == name })))
            saves = activity.saveClicks; cancels = activity.cancelClicks
        }
        capture(scenario, scene)
        onView(withText("保存电影票")).perform(click())
        onView(withText("取消")).perform(click())
        scenario.onActivity { assertEquals(saves + 1, it.saveClicks); assertEquals(cancels + 1, it.cancelClicks) }
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
        }
    }

    @Test fun fixedActionsWithRealKeyboardAndRecreation() {
        launch().use { scenario ->
            portrait(scenario)
            field(scenario, "电影名称", "portrait-title-ime")
            field(scenario, "座位", "portrait-seat-ime")
            scenario.onActivity { it.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE }
            await(scenario) { it.resources.configuration.orientation == Configuration.ORIENTATION_LANDSCAPE && it.hasWindowFocus() }
            field(scenario, "座位", "landscape-seat-ime")
            portrait(scenario)
            scenario.recreate()
            await(scenario) { it.hasWindowFocus() && it.editor.scroll.height > 0 }
            scenario.onActivity { checkLayout(it) }
            capture(scenario, "portrait-recreated")
        }
    }

    @Test fun largeEditorTextAndLongHintRemainReachable() {
        launch(largeText = true, longHint = true).use { scenario ->
            portrait(scenario)
            scenario.onActivity { assertEquals(2f, it.fields.first().resources.configuration.fontScale, 0.01f); checkLayout(it); it.editor.scroll.fullScroll(View.FOCUS_DOWN) }
            await(scenario) { complete(it.status) && complete(it.date) }
            capture(scenario, "large-text-long-hint")
            scenario.onActivity { it.save.text = "核对并重试保存" }
            await(scenario) { it.save.layout?.text?.toString() == it.save.text.toString() && complete(it.save) }
            scenario.onActivity { checkLayout(it) }
            capture(scenario, "large-pending-action")
            scenario.onActivity { it.save.isEnabled = false; it.cancel.isEnabled = false; checkLayout(it) }
            capture(scenario, "large-busy-action")
            scenario.onActivity { it.save.text = "保存电影票"; it.save.isEnabled = true; it.cancel.isEnabled = true }
            field(scenario, "电影名称", "large-portrait-title-ime")
            scenario.onActivity { it.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE }
            await(scenario) { it.resources.configuration.orientation == Configuration.ORIENTATION_LANDSCAPE && it.hasWindowFocus() }
            field(scenario, "座位", "large-landscape-seat-ime")
            portrait(scenario)
        }
    }
}
