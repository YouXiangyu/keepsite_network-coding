package io.github.youxiangyu.keepsite.ui

import android.content.pm.ActivityInfo
import android.content.res.Configuration
import android.os.SystemClock
import android.os.Build
import android.view.View
import android.view.autofill.AutofillManager
import android.widget.EditText
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.ViewModelProvider
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
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
import io.github.youxiangyu.keepsite.UiViewModel
import org.junit.Assert.*
import org.junit.Assume.assumeFalse
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/** Read-only online smoke test: never submits credentials or changes cloud records. */
@RunWith(AndroidJUnit4::class)
class OnlineUiInstrumentedTest {
    private fun await(detail: () -> String = { "" }, check: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + 35_000
        while (!check() && SystemClock.elapsedRealtime() < deadline) SystemClock.sleep(40)
        assertTrue("Expected the online UI transition within 35 seconds: ${detail()}", check())
    }

    @Test fun onlineLoginKeepsFocusedFieldsVisibleWithKeyboardAndRotation() {
        assertEquals("https://keepsite.space", BuildConfig.API_BASE_URL)
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        // Skip before Activity.onStart can refresh a saved session or start account networking.
        val secrets = File(context.noBackupFilesDir, "session-secrets")
        assumeFalse("Preserve any saved account before launching the app",
            listOf("cookie", "cookie.bak", "cookie.new").any { File(secrets, it).exists() })
        val automation = InstrumentationRegistry.getInstrumentation().uiAutomation
        val autofill = ActivityLifecycleCallback { activity, stage ->
            if (activity is MainActivity && stage == Stage.CREATED) {
                activity.window.decorView.importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS
                activity.getSystemService(AutofillManager::class.java)?.cancel()
            }
        }
        ActivityLifecycleMonitorRegistry.getInstance().addLifecycleCallback(autofill)
        try {
            if (Build.VERSION.SDK_INT >= 29) automation.adoptShellPermissionIdentity("android.permission.START_ACTIVITIES_FROM_BACKGROUND")
            val launched = try { ActivityScenario.launch(MainActivity::class.java) }
                finally { if (Build.VERSION.SDK_INT >= 29) automation.dropShellPermissionIdentity() }
            launched.use { scenario ->
                // Exercise real Activity recreation without changing the device rotation setting.
                scenario.onActivity { it.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_PORTRAIT }
                var authenticated = false
                var initialState = ""
                await(detail = { initialState }) {
                    var ready = false
                    scenario.onActivity { activity ->
                        val state = ViewModelProvider(activity)[UiViewModel::class.java].controller.snapshot()
                        authenticated = state.session?.authenticated == true
                        initialState = "session=${state.session != null}, error=${state.error}, focus=${activity.window.decorView.hasWindowFocus()}, orientation=${activity.resources.configuration.orientation}"
                        ready = state.session != null && state.error.isBlank() && activity.window.decorView.hasWindowFocus() &&
                            activity.resources.configuration.orientation == Configuration.ORIENTATION_PORTRAIT
                    }
                    ready
                }
                assumeFalse("Preserve an existing signed-in account", authenticated)
                fun checkField(hint: String) {
                    onView(withHint(hint)).perform(scrollTo(), click())
                    await {
                        var visible = false
                        scenario.onActivity { activity ->
                            val focused = activity.currentFocus
                            // IME visibility is signalled before its resize animation finishes.
                            // Wait for the same strict visibility condition asserted below.
                            visible = ViewCompat.getRootWindowInsets(activity.window.decorView)?.isVisible(WindowInsetsCompat.Type.ime()) == true &&
                                focused is EditText && focused.hint.toString() == hint && isCompletelyDisplayed().matches(focused)
                        }
                        visible
                    }
                    onView(withHint(hint)).check(matches(isCompletelyDisplayed())).perform(closeSoftKeyboard())
                }
                checkField("邮箱")
                checkField("密码")
                scenario.onActivity { it.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE }
                await {
                    var landscape = false
                    scenario.onActivity { landscape = it.resources.configuration.orientation == Configuration.ORIENTATION_LANDSCAPE && it.window.decorView.hasWindowFocus() }
                    landscape
                }
                checkField("密码")
            }
        } finally {
            ActivityLifecycleMonitorRegistry.getInstance().removeLifecycleCallback(autofill)
        }
    }
}
