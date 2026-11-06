package io.github.youxiangyu.keepsite.ui

import android.content.pm.ActivityInfo
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
import io.github.youxiangyu.keepsite.ui.Design.Space
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

@RunWith(AndroidJUnit4::class)
class EditorLayoutInstrumentedTest {
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private fun launch(): ActivityScenario<EditorLayoutHostActivity> {
        if (Build.VERSION.SDK_INT >= 29) instrumentation.uiAutomation.adoptShellPermissionIdentity("android.permission.START_ACTIVITIES_FROM_BACKGROUND")
        return try { ActivityScenario.launch(EditorLayoutHostActivity::class.java) }
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
        val directory = File(instrumentation.targetContext.getExternalFilesDir(null), "editor-layout-evidence").apply { mkdirs() }
        var data = JSONObject()
        scenario.onActivity { activity ->
            data = JSONObject().put("scene", name).put("scope", "Isolated layout Host; local controls, no business save")
                .put("sourceBase", InstrumentationRegistry.getArguments().getString("sourceBase"))
                .put("sourceSnapshot", InstrumentationRegistry.getArguments().getString("sourceSnapshot"))
                .put("apiBaseUrl", BuildConfig.API_BASE_URL).put("sdk", Build.VERSION.SDK_INT)
                .put("orientation", activity.resources.configuration.orientation).put("fontScale", activity.resources.configuration.fontScale)
                .put("imeVisible", ViewCompat.getRootWindowInsets(activity.window.decorView)?.isVisible(WindowInsetsCompat.Type.ime()) == true)
                .put("scrollY", activity.editor.scroll.scrollY).put("scroll", box(activity.editor.scroll)).put("body", box(activity.editor.body))
                .put("save", box(activity.save)).put("cancel", box(activity.cancel)).put("date", box(activity.date))
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

    @Test fun originalScrollLayoutAndLocalActions() {
        launch().use { scenario ->
            portrait(scenario)
            scenario.onActivity { activity ->
                assertSame(activity.editor.scroll, activity.editor.root)
                assertSame(activity.editor.body, activity.save.parent)
                assertSame(activity.editor.body, activity.cancel.parent)
                assertEquals(activity.editor.body.indexOfChild(activity.save) + 1, activity.editor.body.indexOfChild(activity.cancel))
                assertEquals(activity.dp(Space.M), (activity.save.layoutParams as ViewGroup.MarginLayoutParams).topMargin)
                assertEquals(activity.dp(Space.S), (activity.cancel.layoutParams as ViewGroup.MarginLayoutParams).topMargin)
                assertEquals(0, activity.editor.scroll.scrollY)
                assertTrue(activity.editor.body.height > activity.editor.scroll.height)
                assertFalse("Record the original portrait first-screen limitation", complete(activity.save))
            }
            capture(scenario, "portrait-top")
            scenario.onActivity { it.editor.scroll.fullScroll(View.FOCUS_DOWN) }
            await(scenario) { it.editor.scroll.scrollY > 0 && complete(it.save) && complete(it.cancel) && complete(it.date) }
            capture(scenario, "portrait-end")
            onView(withText("保存电影票")).perform(click())
            onView(withText("取消")).perform(click())
            scenario.onActivity { assertEquals(1, it.saveClicks); assertEquals(1, it.cancelClicks) }
            capture(scenario, "local-actions")
        }
    }

    @Test fun originalKeyboardAndRecreationEvidence() {
        launch().use { scenario ->
            portrait(scenario)
            fun field(name: String, scene: String) {
                onView(withHint(name)).perform(scrollTo(), click())
                var previousInset = -1; var stableInsets = 0
                await(scenario) { activity ->
                    val input = activity.fields.first { it.hint.toString() == name }
                    val insets = ViewCompat.getRootWindowInsets(activity.window.decorView)
                    val bottom = insets?.getInsets(WindowInsetsCompat.Type.ime())?.bottom ?: 0
                    stableInsets = if (bottom == previousInset) stableInsets + 1 else 0; previousInset = bottom
                    activity.currentFocus === input && complete(input) &&
                        insets?.isVisible(WindowInsetsCompat.Type.ime()) == true && bottom > 0 && stableInsets >= 4
                }
                capture(scenario, scene)
                onView(withHint(name)).perform(closeSoftKeyboard())
                await(scenario) { ViewCompat.getRootWindowInsets(it.window.decorView)?.isVisible(WindowInsetsCompat.Type.ime()) == false }
            }
            field("电影名称", "portrait-title-ime")
            field("座位", "portrait-seat-ime")
            scenario.onActivity { it.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE }
            await(scenario) { it.resources.configuration.orientation == Configuration.ORIENTATION_LANDSCAPE && it.hasWindowFocus() }
            field("座位", "landscape-seat-ime")
            portrait(scenario)
            scenario.recreate()
            await(scenario) { it.hasWindowFocus() && it.editor.scroll.height > 0 }
            capture(scenario, "portrait-recreated")
        }
    }
}
