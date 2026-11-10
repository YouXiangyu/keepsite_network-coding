package io.github.youxiangyu.keepsite.networkfixture

import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.Description
import org.junit.runners.model.Statement

class DisplayStateRuleTest {
    private class Device : DisplayControl {
        var user = "0"
        var size: String? = null
        var density: String? = null
        var rotation = "free"
        val settings = mutableMapOf("user_rotation" to "0", "accelerometer_rotation" to "1")
        override var fontScale = 1f
        val writes = mutableListOf<String>()
        val reads = mutableListOf<String>()
        var rejectRotation = false
        var malformedDensity = false
        var fail: (String) -> Throwable? = { null }
        private fun rotationFromSettings() {
            // Android 15 default-display settings use 0 for either missing key.
            rotation = if ((settings["accelerometer_rotation"] ?: "0") == "1") "free"
                else "lock ${settings["user_rotation"] ?: "0"}"
        }
        override fun setRotation(rotation: Int): Boolean {
            writes += "rotation $rotation"
            if (rejectRotation) return false
            this.rotation = "lock $rotation"
            settings["user_rotation"] = rotation.toString()
            settings["accelerometer_rotation"] = "0"
            return true
        }
        override fun shell(command: String): String {
            val words = command.split(' ')
            val write = (words[0] == "wm" && words.size > 2) || (words[0] == "settings" && words[3] != "get")
            if (write) writes += command else reads += command
            fail(command)?.let { throw it }
            return when {
                command == "am get-current-user" -> user
                command == "wm size" -> "Physical size: 1080x1920" + (size?.let { "\nOverride size: $it" } ?: "")
                command == "wm density" -> if (malformedDensity) "unrecognized density" else
                    "Physical density: 420" + (density?.let { "\nOverride density: $it" } ?: "")
                command == "wm user-rotation" -> rotation
                words[0] == "wm" && words[1] == "size" -> { size = words[2].takeUnless { it == "reset" }; "" }
                words[0] == "wm" && words[1] == "density" -> { density = words[2].takeUnless { it == "reset" }; "" }
                words[0] == "wm" && words[1] == "user-rotation" -> {
                    rotation = words.drop(2).joinToString(" ")
                    settings["accelerometer_rotation"] = if (rotation == "free") "1" else "0"
                    if (rotation.startsWith("lock")) settings["user_rotation"] = words[3]
                    ""
                }
                words[0] == "settings" && words[3] == "get" -> settings[words[5]] ?: "null"
                words[0] == "settings" && words[3] == "delete" -> {
                    settings.remove(words[5])
                    rotationFromSettings()
                    "Deleted 1 rows"
                }
                words[0] == "settings" && words[3] == "put" -> {
                    settings[words[5]] = words[6]
                    rotationFromSettings()
                    ""
                }
                else -> error("Unexpected command: $command")
            }
        }
        fun state() = listOf(user, size, density, rotation, settings.toMap(), fontScale)
    }
    private fun run(device: Device, guard: String? = "external-journal-v1", body: (DisplayStateRule) -> Unit) {
        val rule = DisplayStateRule(device) { guard }
        rule.apply(object : Statement() { override fun evaluate() = body(rule) },
            Description.createTestDescription(javaClass, "display")).evaluate()
    }
    private fun failure(body: () -> Unit): Throwable {
        try { body() } catch (failure: Throwable) { return failure }
        throw AssertionError("Expected failure")
    }

    @Test fun missingExternalJournalRefusesAllDeviceAccess() {
        val device = Device()
        failure { run(device, null) { it.configure("720x1600", 1f) } }
        assertTrue(device.reads.isEmpty())
        assertTrue(device.writes.isEmpty())
    }

    @Test fun unknownSnapshotRefusesBodyAndAllMutations() {
        val device = Device().apply { malformedDensity = true }
        var bodyRan = false
        failure { run(device) { bodyRan = true; it.configure("720x1600", 1f) } }
        assertFalse(bodyRan)
        assertTrue(device.writes.isEmpty())
    }

    @Test fun inconsistentRotationSnapshotRefusesBodyAndAllMutations() {
        val device = Device().apply { rotation = "free"; settings.clear() }
        var bodyRan = false
        val actual = failure { run(device) { bodyRan = true; it.configure("720x1600", 1f) } }
        assertTrue(actual.message!!.contains("Inconsistent rotation"))
        assertFalse(bodyRan)
        assertTrue(device.writes.isEmpty())
    }

    @Test fun normalBodyResetsOnlyOriginallyAbsentOverrides() {
        val device = Device()
        val before = device.state()
        run(device) { it.configure("720x1600", 2f); it.rotate(1) }
        assertEquals(before, device.state())
        assertTrue(device.writes.containsAll(listOf("wm size reset", "wm density reset", "wm user-rotation free")))
    }

    @Test fun nonDefaultOverridesLockedRotationAndLocalFontRestoreExactly() {
        val device = Device().apply {
            user = "10"; size = "900x1700"; density = "360"; rotation = "lock 3"; fontScale = 1.3f
            settings["user_rotation"] = "3"; settings["accelerometer_rotation"] = "0"
        }
        val before = device.state()
        run(device) { it.configure("1536x2048", 2f); it.rotate(1) }
        assertEquals(before, device.state())
        assertTrue(device.writes.containsAll(listOf("wm size 900x1700", "wm density 360", "wm user-rotation lock 3")))
        assertFalse(device.writes.any { it.endsWith("reset") })
        assertTrue(device.writes.filter { it.startsWith("settings") }.all { it.startsWith("settings --user 10 ") })
    }

    @Test fun assertionFailurePreservesSameThrowableAndRestores() {
        val device = Device()
        val before = device.state()
        val original = AssertionError("body failed")
        val actual = failure { run(device) { it.configure("720x1600", 2f); throw original } }
        assertSame(original, actual)
        assertTrue(actual.suppressed.isEmpty())
        assertEquals(before, device.state())
    }

    @Test fun partialConfigureFailureStillRestoresEarlierSizeMutation() {
        val device = Device()
        val before = device.state()
        val original = IllegalStateException("density rejected")
        device.fail = { if (it == "wm density 320") original else null }
        val actual = failure { run(device) { it.configure("720x1600", 2f) } }
        assertSame(original, actual)
        assertEquals(before, device.state())
    }

    @Test fun cleanupFailureAfterSuccessFailsCaseAndAttemptsOtherFields() {
        val device = Device()
        device.fail = { if (it == "wm size reset") IllegalStateException("size restore rejected") else null }
        val actual = failure { run(device) { it.configure("720x1600", 2f) } }
        assertTrue(actual.message!!.contains("restoration incomplete"))
        assertEquals(1, actual.suppressed.size)
        assertNull(device.density)
        assertEquals("free", device.rotation)
        assertEquals(1f, device.fontScale)
        assertTrue(device.writes.contains("settings --user 0 put system accelerometer_rotation 1"))
    }

    @Test fun bodyAndCleanupFailuresKeepBodyPrimaryAndAttachCleanup() {
        val device = Device()
        device.fail = { if (it == "wm size reset") IllegalStateException("restore rejected") else null }
        val original = AssertionError("original body")
        val actual = failure { run(device) { it.configure("720x1600", 2f); throw original } }
        assertSame(original, actual)
        assertEquals(1, actual.suppressed.size)
        assertTrue(actual.suppressed.single().message!!.contains("restoration incomplete"))
        assertEquals(1f, device.fontScale)
        assertEquals("free", device.rotation)
    }

    @Test fun setupAndCleanupFailuresKeepSetupPrimaryAndAttemptEveryRestore() {
        val device = Device()
        val original = IllegalStateException("setup density failed")
        device.fail = {
            when (it) {
                "wm density 320" -> original
                "wm size reset" -> IllegalStateException("restore size failed")
                else -> null
            }
        }
        val actual = failure { run(device) { it.configure("720x1600", 2f) } }
        assertSame(original, actual)
        assertEquals(1, actual.suppressed.size)
        assertTrue(device.writes.containsAll(listOf("wm density reset", "wm user-rotation free",
            "settings --user 0 put system user_rotation 0", "settings --user 0 put system accelerometer_rotation 1")))
    }

    @Test fun rejectedUiAutomationRotationFailsSetupAndRestores() {
        val device = Device().apply { rejectRotation = true }
        val before = device.state()
        val actual = failure { run(device) { it.configure("720x1600", 2f) } }
        assertTrue(actual.message!!.contains("rejected rotation"))
        assertEquals(before, device.state())
    }

    @Test fun knownAbsentSettingsAreDeletedRatherThanWrittenAsDefaults() {
        val device = Device().apply { settings.clear(); rotation = "lock 0" }
        val before = device.state()
        run(device) { it.configure("720x1600", 2f) }
        assertEquals(before, device.state())
        assertTrue(device.writes.containsAll(listOf("settings --user 0 delete system user_rotation",
            "settings --user 0 delete system accelerometer_rotation")))
    }

    @Test fun changedUserRefusesCleanupWritesButStillRestoresLocalFont() {
        val device = Device()
        var count = 0
        val actual = failure { run(device) {
            it.configure("720x1600", 2f)
            count = device.writes.size
            device.user = "10"
        } }
        assertEquals(count, device.writes.size)
        assertEquals(1f, device.fontScale)
        assertTrue(actual.message!!.contains("restoration incomplete"))
    }
}
