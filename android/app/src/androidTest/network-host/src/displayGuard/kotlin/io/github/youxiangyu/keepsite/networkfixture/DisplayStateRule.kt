package io.github.youxiangyu.keepsite.networkfixture

import org.junit.rules.TestRule
import org.junit.runner.Description
import org.junit.runners.model.Statement

/** Test-only device access; shell must throw on nonzero exit or missing exit status. */
internal interface DisplayControl {
    fun shell(command: String): String
    fun setRotation(rotation: Int): Boolean
    var fontScale: Float
}

/** Per-case cleanup. Killed instrumentation still requires the external journal runner. */
internal class DisplayStateRule(
    private val control: DisplayControl,
    private val displayGuard: () -> String?,
) : TestRule {
    private data class Dimension(val physical: String, val override: String?)
    private data class Snapshot(
        val user: String, val size: Dimension, val density: Dimension,
        val rotation: String, val userRotation: String, val accelerometer: String, val font: Float,
    )
    private var before: Snapshot? = null
    private var changed = false

    override fun apply(base: Statement, description: Description): Statement = object : Statement() {
        override fun evaluate() {
            check(displayGuard() == "external-journal-v1") { "Run via the external display-journal runner" }
            check(before == null) { "Display rule already active" }
            before = snapshot() // All parsing/validation completes before configure can write anything.
            var primary: Throwable? = null
            try {
                base.evaluate()
            } catch (failure: Throwable) {
                primary = failure
                throw failure
            } finally {
                val saved = before!!
                try {
                    restore(saved)
                } catch (cleanup: Throwable) {
                    if (primary != null) primary.addSuppressed(cleanup) else throw cleanup
                } finally {
                    before = null
                    changed = false
                }
            }
        }
    }

    fun configure(size: String, font: Float) {
        require(Regex("[1-9][0-9]*x[1-9][0-9]*").matches(size)) { "Invalid target size" }
        require(font.isFinite() && font > 0f) { "Invalid local font scale" }
        val saved = active()
        sameUser(saved)
        changed = true // A later setup failure must restore earlier successful mutations.
        control.shell("wm size $size")
        settled("target size") { dimension("size").override == size }
        sameUser(saved)
        control.shell("wm density 320")
        settled("target density") { dimension("density").override == "320" }
        control.fontScale = font
        check(control.fontScale == font) { "Local font scale did not change" }
        rotate(0)
    }

    fun rotate(rotation: Int) {
        require(rotation in 0..3) { "Invalid frozen rotation" }
        val saved = active()
        sameUser(saved)
        changed = true
        check(control.setRotation(rotation)) { "UiAutomation rejected rotation $rotation" }
        settled("frozen rotation") { rotation() == "lock $rotation" }
    }

    private fun active() = checkNotNull(before) { "Display mutations require the active rule" }
    private fun user(): String = control.shell("am get-current-user").trim().also {
        check(Regex("0|[1-9][0-9]*").matches(it) && it.toIntOrNull() != null) { "Unknown Android user: $it" }
    }
    private fun sameUser(saved: Snapshot) = check(user() == saved.user) { "Android user changed; refusing display writes" }
    private fun dimension(kind: String): Dimension {
        val value = if (kind == "size") "[1-9][0-9]*x[1-9][0-9]*" else "[1-9][0-9]*"
        val match = Regex("Physical $kind: ($value)(?:\\nOverride $kind: ($value))?")
            .matchEntire(control.shell("wm $kind").trim().replace("\r\n", "\n"))
        checkNotNull(match) { "Unknown wm $kind response" }
        return Dimension(match.groupValues[1], match.groupValues[2].ifEmpty { null })
    }
    private fun rotation(): String = control.shell("wm user-rotation").trim().also {
        check(Regex("free|lock [0-3]").matches(it)) { "Unknown rotation mode: $it" }
    }
    private fun setting(user: String, key: String): String = control.shell("settings --user $user get system $key").trim().also {
        val pattern = if (key == "user_rotation") "null|[0-3]" else "null|[01]"
        check(Regex(pattern).matches(it)) { "Unknown $key response: $it" }
    }
    private fun snapshot(): Snapshot {
        val user = user()
        val saved = Snapshot(user, dimension("size"), dimension("density"), rotation(),
            setting(user, "user_rotation"), setting(user, "accelerometer_rotation"), control.fontScale)
        check(saved.font.isFinite() && saved.font > 0f) { "Unknown local font scale" }
        sameUser(saved)
        val expectedRotation = if (saved.accelerometer == "1") "free"
            else "lock ${saved.userRotation.takeUnless { it == "null" } ?: "0"}"
        check(saved.rotation == expectedRotation) { "Inconsistent rotation mode and raw settings; refusing display writes" }
        return saved
    }

    private fun restore(saved: Snapshot) {
        val errors = mutableListOf<Throwable>()
        fun attempt(label: String, block: () -> Unit) {
            try { block() } catch (failure: Throwable) {
                errors += AssertionError("Display cleanup failed: $label", failure)
            }
        }
        // Always restore local state, even if a device command fails or the user changes.
        attempt("local font") {
            control.fontScale = saved.font
            check(control.fontScale == saved.font) { "Local font readback differs" }
        }
        if (changed) {
            listOf("size" to saved.size, "density" to saved.density).forEach { (kind, original) ->
                attempt(kind) {
                    sameUser(saved)
                    control.shell("wm $kind ${original.override ?: "reset"}")
                    settled("restored $kind") { dimension(kind) == original }
                }
            }
            attempt("rotation mode") {
                sameUser(saved)
                control.shell("wm user-rotation ${saved.rotation}")
            }
            listOf("user_rotation" to saved.userRotation, "accelerometer_rotation" to saved.accelerometer).forEach { (key, value) ->
                attempt(key) {
                    sameUser(saved)
                    val operation = if (value == "null") "delete system $key" else "put system $key $value"
                    control.shell("settings --user ${saved.user} $operation")
                }
            }
            // Verify after all rotation writes: settings can themselves affect frozen/free mode.
            attempt("rotation readback") { sameUser(saved); settled("restored rotation") { rotation() == saved.rotation } }
            listOf("user_rotation" to saved.userRotation, "accelerometer_rotation" to saved.accelerometer).forEach { (key, value) ->
                attempt("$key readback") { sameUser(saved); settled("restored $key") { setting(saved.user, key) == value } }
            }
        }
        if (errors.isNotEmpty()) throw AssertionError("Display restoration incomplete (${errors.size} failures)").apply {
            errors.forEach { addSuppressed(it) }
        }
    }

    private fun settled(label: String, matches: () -> Boolean) {
        val deadline = System.nanoTime() + 2_000_000_000L
        while (!matches()) {
            check(System.nanoTime() < deadline) { "$label readback differs" }
            Thread.sleep(50)
        }
    }
}
