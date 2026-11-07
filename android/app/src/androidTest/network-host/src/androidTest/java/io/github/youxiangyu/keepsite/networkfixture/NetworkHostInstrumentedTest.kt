package io.github.youxiangyu.keepsite.networkfixture

import android.app.UiAutomation
import android.graphics.Bitmap
import android.os.SystemClock
import android.view.View
import android.view.ViewGroup
import android.view.accessibility.AccessibilityNodeInfo
import android.widget.EditText
import android.widget.ScrollView
import android.widget.TextView
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.*
import androidx.test.espresso.action.ViewActions.*
import androidx.test.espresso.matcher.ViewMatchers.*
import androidx.test.platform.app.InstrumentationRegistry
import io.github.youxiangyu.keepsite.core.*
import org.junit.Assert.*
import org.junit.Test
import org.json.JSONArray
import org.json.JSONObject
import org.hamcrest.Matchers.*
import java.io.File

/** Shared-component verification only. No production MainActivity, repository or network. */
class NetworkHostInstrumentedTest {
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val automation get() = instrumentation.uiAutomation
    private val output get() = File(instrumentation.targetContext.getExternalFilesDir(null), "network-n4").apply { mkdirs() }
    private fun shell(command: String): String = automation.executeShellCommand(command).use { descriptor ->
        java.io.FileInputStream(descriptor.fileDescriptor).bufferedReader().use { it.readText() }
    }
    private fun waitFor(message: String, predicate: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + 15000
        while (SystemClock.elapsedRealtime() < deadline) { if (predicate()) return; Thread.sleep(100) }
        fail(message)
    }
    private fun views(root: View): List<View> = listOf(root) + if (root is ViewGroup) (0 until root.childCount).flatMap { views(root.getChildAt(it)) } else emptyList()
    private fun button(text: String) = onView(withText(text)).perform(scrollTo(), click())
    private fun screenshot(name: String, scenario: ActivityScenario<NetworkHostActivity>) {
        instrumentation.waitForIdleSync()
        if (name == "01-360-summary") {
            waitFor("portrait target window focused and laid out") {
                var stable = false
                scenario.onActivity { activity -> stable = activity.hasWindowFocus() && activity.window.decorView.isLaidOut &&
                    activity.resources.configuration.orientation == android.content.res.Configuration.ORIENTATION_PORTRAIT &&
                    activity.windowManager.defaultDisplay.rotation == android.view.Surface.ROTATION_0 }
                stable
            }
            Thread.sleep(700) // Driver surface-rotation settlement only, excluded from all performance samples.
        }
        capture("$name.png")
        scenario.onActivity { activity ->
            val rows = JSONArray()
            views(activity.window.decorView).filterIsInstance<TextView>().forEach { view ->
                val bounds = android.graphics.Rect(); val visible = view.getGlobalVisibleRect(bounds)
                rows.put(JSONObject().put("text", view.text.toString()).put("sizePx", view.textSize).put("visible", visible)
                    .put("bounds", listOf(bounds.left, bounds.top, bounds.right, bounds.bottom)).put("width", view.width).put("height", view.height)
                    .put("lineCount", view.lineCount).put("focused", view.hasFocus()))
            }
            File(output, "$name-layout.json").writeText(JSONObject().put("widthDp", activity.resources.configuration.screenWidthDp)
                .put("heightDp", activity.resources.configuration.screenHeightDp).put("fontScale", activity.resources.configuration.fontScale)
                .put("scrollY", activity.page?.view?.scrollY ?: JSONObject.NULL).put("savedScroll", activity.model.state.scroll[activity.model.state.tab])
                .put("scope", "test-only auxiliary APK, actual shared NetworkMonitorScreen/core/theme; synthetic typed state, no business network")
                .put("views", rows).toString(2))
        }
    }
    private fun capture(name: String) {
        val bitmap = automation.takeScreenshot()!!
        try { File(output, name).outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) } } finally { bitmap.recycle() }
    }
    private fun configure(size: String, font: Float) {
        shell("wm size $size"); shell("wm density 320")
        HostConfiguration.fontScale = font
        automation.setRotation(UiAutomation.ROTATION_FREEZE_0)
    }
    private fun globalText(node: AccessibilityNodeInfo?, text: String): AccessibilityNodeInfo? {
        if (node == null) return null
        if (node.text?.toString()?.equals(text, true) == true && node.isEnabled && node.isVisibleToUser) return node
        repeat(node.childCount) { globalText(node.getChild(it), text)?.let { found -> return found } }
        return null
    }
    private fun saveSystemDocument() {
        waitFor("actual DocumentsUI save button") { globalText(automation.rootInActiveWindow, "Save") != null }
        if (globalText(automation.rootInActiveWindow, "Keepsite N4 isolated reports") == null) {
            fun drawer(node: AccessibilityNodeInfo?): AccessibilityNodeInfo? {
                if (node == null) return null
                if (node.contentDescription?.toString()?.lowercase()?.let { "root" in it || "navigation" in it || "drawer" in it } == true) return node
                repeat(node.childCount) { drawer(node.getChild(it))?.let { found -> return found } }
                return null
            }
            val navigation = drawer(automation.rootInActiveWindow) ?: throw AssertionError("Missing DocumentsUI roots drawer")
            assertTrue(navigation.performAction(AccessibilityNodeInfo.ACTION_CLICK))
            waitFor("owned SAF provider root") { globalText(automation.rootInActiveWindow, "Keepsite N4 isolated reports") != null }
            var root = globalText(automation.rootInActiveWindow, "Keepsite N4 isolated reports")!!
            while (!root.isClickable && root.parent != null) root = root.parent
            assertTrue(root.performAction(AccessibilityNodeInfo.ACTION_CLICK))
            waitFor("owned root opened") { globalText(automation.rootInActiveWindow, "Save") != null }
        }
        assertNotNull("Must create only inside our owned provider", globalText(automation.rootInActiveWindow, "Keepsite N4 isolated reports"))
        val node = globalText(automation.rootInActiveWindow, "Save")!!
        assertTrue(node.performAction(AccessibilityNodeInfo.ACTION_CLICK))
    }
    @Test fun narrowOverviewDetailsConnectionsAndBackUseActualSharedViews() {
        configure("720x1600", 1f)
        ActivityScenario.launch(NetworkHostActivity::class.java).use { scenario ->
            waitFor("page ready") { var ready = false; scenario.onActivity { ready = it.page != null }; ready }
            screenshot("01-360-summary", scenario)
            scenario.onActivity { activity ->
                val texts = views(activity.window.decorView).filterIsInstance<TextView>()
                listOf("API最近可达", "实时连接", "数据同步", "设备传输", "最近文件路径").forEach { name -> assertTrue(texts.any { it.text.toString().startsWith(name) }) }
                assertFalse(activity.d.snapshot()!!.observations.detailed)
            }
            button("展开观测详情"); screenshot("02-360-details", scenario)
            onView(withText("服务器或 S3 文件读取")).perform(scrollTo()); screenshot("02b-360-role-diagrams", scenario)
            button("连接与传输")
            scenario.onActivity { it.fillHistory() }
            var release: java.io.Closeable? = null
            scenario.onActivity { release = it.holdHttp() }
            try {
                waitFor("actual observer callback remains active") { var count = 0; scenario.onActivity { count = it.d.snapshot()!!.api.active }; count == 1 }
                Thread.sleep(650); screenshot("03-360-connections", scenario)
            } finally { release?.close() }
            scenario.onActivity { activity -> assertTrue(views(activity.window.decorView).filterIsInstance<TextView>().any { it.text.toString().contains("合成授权设备") }) }
            androidx.test.espresso.Espresso.pressBack()
            onView(withText("隔离验证入口")).check(androidx.test.espresso.assertion.ViewAssertions.matches(isDisplayed()))
            scenario.onActivity { assertEquals(1, it.model.backCount) }
            var reads = 0; scenario.onActivity { reads = it.model.snapshotReads }
            Thread.sleep(1100)
            scenario.onActivity { assertEquals("closed page must stop its memory read tick", reads, it.model.snapshotReads) }
        }
    }
    @Test fun largeFontTabletFiltersKeepInputAndExpandFullTraceAcrossClocksThenRotate() {
        configure("1536x2048", 2f)
        ActivityScenario.launch(NetworkHostActivity::class.java).use { scenario ->
            screenshot("04-768-large-summary", scenario)
            scenario.onActivity { assertEquals(2f, it.resources.configuration.fontScale); it.fillHistory() }
            button("事件 Trace"); Thread.sleep(650)
            var prefix = ""
            scenario.onActivity { activity -> prefix = activity.d.snapshot()!!.observations.events.last { it.protocol == MonitorProtocol.HTTP }.operationId.take(12) }
            onView(withContentDescription("协议筛选")).perform(scrollTo(), click())
            onData(anything()).atPosition(1).perform(click())
            onView(withHint("操作或 trace 别名前缀")).perform(scrollTo(), click(), typeText(prefix))
            var input: EditText? = null; var scroll = 0
            scenario.onActivity { activity ->
                input = views(activity.window.decorView).filterIsInstance<EditText>().single(); scroll = activity.page!!.view.scrollY
                val start = SystemClock.elapsedRealtimeNanos()
                input!!.setText(prefix.dropLast(1)); input!!.setText(prefix); input!!.setSelection(prefix.length)
                activity.page!!.view.postOnAnimation { activity.model.samples.put(JSONObject().put("scope", "two actual operation prefix edits through next native view callback; not screen/GPU presentation").put("elapsedNs", SystemClock.elapsedRealtimeNanos() - start)) }
                activity.pulse()
            }
            Thread.sleep(1100)
            scenario.onActivity { activity ->
                val same = views(activity.window.decorView).filterIsInstance<EditText>().single()
                assertSame(input, same); assertEquals(prefix, same.text.toString()); assertTrue(same.hasFocus()); assertTrue(kotlin.math.abs(activity.page!!.view.scrollY - scroll) <= 4)
            }
            androidx.test.espresso.Espresso.closeSoftKeyboard()
            try { onView(allOf(withText(allOf(containsString(prefix), containsString("完成"))), isAssignableFrom(android.widget.Button::class.java))).perform(scrollTo(), click()) }
            catch (failure: Throwable) { screenshot("red-trace-scroll-target", scenario); throw failure }
            Thread.sleep(150)
            screenshot("05-768-large-trace", scenario)
            scenario.onActivity { activity ->
                val texts = views(activity.window.decorView).filterIsInstance<TextView>().map { it.text.toString() }
                assertTrue(texts.count { it.contains(" clock ") } >= 2)
                assertTrue(texts.any { it.contains("完整关联成员") })
            }
            var beforeRotate = 0; scenario.onActivity { beforeRotate = it.page!!.view.scrollY }
            automation.setRotation(UiAutomation.ROTATION_FREEZE_90); Thread.sleep(850)
            screenshot("06-landscape-large-trace", scenario)
            scenario.onActivity { activity ->
                assertEquals(prefix, views(activity.window.decorView).filterIsInstance<EditText>().single().text.toString())
                val scrollView = activity.page!!.view
                val maximum = (scrollView.getChildAt(0).height - scrollView.height).coerceAtLeast(0)
                activity.model.samples.put(JSONObject().put("scope", "actual rotation scroll restoration").put("before", beforeRotate).put("after", scrollView.scrollY).put("maximum", maximum))
                File(output, "device-ui-samples.json").writeText(JSONObject().put("samples", activity.model.samples).toString(2))
                assertTrue("rotation should retain bounded scroll: before=$beforeRotate after=${scrollView.scrollY} maximum=$maximum", kotlin.math.abs(scrollView.scrollY - beforeRotate.coerceAtMost(maximum)) <= 4)
            }
        }
    }
    @Test fun actualSafSaveLateAccountResultAndSystemChooserHaveOwnedOutputsOnly() {
        configure("780x1688", 1f)
        ActivityScenario.launch(NetworkHostActivity::class.java).use { scenario ->
            scenario.onActivity { it.fillHistory() }
            button("事件 Trace"); button("展开采集与报告选项")
            scenario.onActivity { activity ->
                val formats = JSONArray()
                NetworkReportFormat.entries.forEach { format -> repeat(3) { index ->
                    val report = activity.report(); val t = SystemClock.elapsedRealtimeNanos(); val text = report.render(format)!!
                    formats.put(JSONObject().put("format", format.name).put("sample", index).put("elapsedNs", SystemClock.elapsedRealtimeNanos() - t).put("utf8Bytes", text.toByteArray().size))
                    assertFalse(text.contains("合成授权设备"))
                } }
                File(output, "device-format.json").writeText(JSONObject().put("scope", "actual Android shared sealed whitelist+format function only; excludes SAF/UI").put("samples", formats).toString(2))
            }
            try { button("保存文件") } catch (failure: Throwable) { screenshot("red-save-scroll-target", scenario); throw failure }
            waitFor("DocumentsUI") { globalText(automation.rootInActiveWindow, "Save") != null }
            capture("07-actual-saf.png")
            saveSystemDocument()
            waitFor("new report saved") { var saved: Boolean? = null; scenario.onActivity { saved = it.model.lastSaved }; saved == true }
            var successful: android.net.Uri? = null
            scenario.onActivity { activity ->
                successful = activity.model.lastUri!!
                assertEquals(NetworkDocuments.AUTHORITY, successful!!.authority)
                val text = activity.contentResolver.openInputStream(successful!!)!!.bufferedReader().use { it.readText() }
                assertEquals("keepsite_network_report", JSONObject(text).getString("kind")); File(output, "actual-saf-report.json").writeText(text)
            }
            button("保存文件")
            waitFor("second DocumentsUI") { globalText(automation.rootInActiveWindow, "Save") != null }
            scenario.onActivity { activity -> assertTrue(activity.model.pending!!.first.current()); activity.replaceAccount(); assertFalse(activity.model.pending!!.first.current()) }
            saveSystemDocument()
            waitFor("late result cancelled") { var saved: Boolean? = null; scenario.onActivity { saved = it.model.lastSaved }; saved == false }
            scenario.onActivity { activity ->
                activity.contentResolver.query(activity.model.lastUri!!, null, null, null, null)!!.use { assertEquals(0, it.count) }
                activity.contentResolver.openInputStream(successful!!)!!.use { assertTrue(it.readBytes().isNotEmpty()) }
            }
            Thread.sleep(650)
            button("展开采集与报告选项")
            var beforeClear: NetworkReport? = null; scenario.onActivity { beforeClear = it.report() }
            button("清空本机诊断记录")
            scenario.onActivity { assertFalse(beforeClear!!.current()); assertTrue(it.report().current()) }
            button("展开采集与报告选项")
            button("系统分享")
            waitFor("actual Sharing text contents visible") {
                val root = automation.rootInActiveWindow
                root?.packageName?.toString() in setOf("android", "com.android.intentresolver") && globalText(root, "Sharing text") != null
            }
            Thread.sleep(500) // External system sheet settlement, not an application timing sample.
            assertNotNull(globalText(automation.rootInActiveWindow, "Sharing text"))
            capture("08-system-chooser-no-recipient.png")
            val chooser = JSONObject().put("enteredRootPackage", automation.rootInActiveWindow!!.packageName.toString()).put("sharingTextVisibleBeforeBack", true)
            shell("input keyevent 4")
            waitFor("chooser gone and original host window resumed") {
                val root = automation.rootInActiveWindow
                if (root?.packageName?.toString() != instrumentation.targetContext.packageName || globalText(root, "Sharing text") != null) false
                else {
                    var resumed = false
                    scenario.onActivity { activity -> resumed = activity.lifecycle.currentState == androidx.lifecycle.Lifecycle.State.RESUMED &&
                        activity.hasWindowFocus() && activity.page?.view?.isShown == true && activity.page?.current() == true }
                    resumed
                }
            }
            Thread.sleep(500)
            assertNull(globalText(automation.rootInActiveWindow, "Sharing text"))
            scenario.onActivity { activity -> File(output, "device-submissions.json").writeText(JSONObject().put("samples", activity.model.samples)
                .put("successfulDocumentPreserved", true).put("lateAccountDocumentDeleted", true).put("shareRecipientChosen", false).toString(2))
                assertEquals(androidx.lifecycle.Lifecycle.State.RESUMED, activity.lifecycle.currentState); assertTrue(activity.hasWindowFocus())
                chooser.put("returnedRootPackage", automation.rootInActiveWindow!!.packageName.toString()).put("hostResumed", true).put("hostWindowFocused", true)
                    .put("chooserGoneAfterBack", true).put("recipientChosen", false)
                File(output, "chooser-window-proof.json").writeText(chooser.toString(2))
            }
            screenshot("09-after-system-return", scenario)
        }
    }
}
