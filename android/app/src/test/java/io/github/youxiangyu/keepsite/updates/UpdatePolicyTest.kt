package io.github.youxiangyu.keepsite.updates

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.util.concurrent.CancellationException

class UpdatePolicyTest {
    @get:Rule val temporary = TemporaryFolder()
    private val published = "a13e743fd6754fddbe15c96e602d6bec10c6ba85019ea3c73488df25a802ca04"
    private val development = "256859f8bdb20ff0fe68d567435e099b7b6cfe1ffc2fc7bfaac4cbfaa5817dab"
    private val payload = "synthetic APK bytes".toByteArray()
    private val apkHash = UpdatePolicy.digest(payload)
    private fun installed(code: Long = 3, signer: String = published) = UpdatePolicy.PackageIdentity(UpdatePolicy.PACKAGE, code, setOf(signer))
    private fun asset(id: Long, name: String, bytes: ByteArray, tag: String = "v0.1") = JSONObject()
        .put("id", id).put("name", name).put("state", "uploaded").put("size", bytes.size)
        .put("digest", "sha256:${UpdatePolicy.digest(bytes)}")
        .put("browser_download_url", "https://github.com/${UpdatePolicy.REPOSITORY}/releases/download/$tag/$name")
    private fun record(code: Long = 4, signer: String = published) = JSONObject().put("applicationId", UpdatePolicy.PACKAGE)
        .put("versionCode", code).put("versionName", "0.1.0").put("sha256", apkHash).put("bytes", payload.size)
        .put("certificateSha256", signer)
    private fun release(metadata: JSONObject? = record(), tag: String = "v0.1"): JSONObject {
        val assets = JSONArray().put(asset(1, "keepsite.apk", payload, tag))
        if (metadata != null) assets.put(asset(2, "apk-verification.json", metadata.toString().toByteArray(), tag))
        return JSONObject().put("draft", false).put("prerelease", false).put("tag_name", tag).put("body", "Release notes").put("assets", assets)
    }
    private fun candidate(value: JSONObject) = requireNotNull(UpdatePolicy.release(value.toString()))
    private fun verification(candidate: UpdatePolicy.Candidate, record: JSONObject) = UpdatePolicy.verification(candidate, record.toString().toByteArray())

    @Test fun sameTagUsesNumericApkVersionAndChangedTagCannotManufactureAnUpgrade() {
        val next = record(4); val candidate = candidate(release(next))
        val verified = verification(candidate, next)
        assertEquals(UpdatePolicy.Action.DOWNLOAD, UpdatePolicy.action(candidate, verified, installed()))
        assertTrue(UpdatePolicy.newerArchive(candidate, verified, installed(), installed(4), 35))
        val same = record(3); val renamed = candidate(release(same, "renamed-release"))
        val unchanged = verification(renamed, same)
        assertEquals(UpdatePolicy.Action.CURRENT, UpdatePolicy.action(renamed, unchanged, installed()))
        assertFalse(UpdatePolicy.newerArchive(renamed, unchanged, installed(5), installed(3), 35))
    }

    @Test fun missingRecordProbesDigestBoundApkButPresentBadRecordCannotBeSkipped() {
        val legacy = candidate(release(null))
        assertEquals(UpdatePolicy.Action.DOWNLOAD, UpdatePolicy.action(legacy, null, installed()))
        assertFalse(UpdatePolicy.newerArchive(legacy, null, installed(), installed(3), 35))
        val present = candidate(release())
        assertThrows(IllegalArgumentException::class.java) { UpdatePolicy.action(present, null, installed()) }
        assertThrows(IllegalArgumentException::class.java) { UpdatePolicy.verification(present, "{}".toByteArray()) }
    }

    @Test fun recordMustMatchAssetBytesDigestPackageAndCandidateSnapshot() {
        val wrong = record().put("sha256", "0".repeat(64)); val wrongCandidate = candidate(release(wrong))
        assertThrows(IllegalArgumentException::class.java) { verification(wrongCandidate, wrong) }
        val wrongPackage = record().put("applicationId", "another.app"); val other = candidate(release(wrongPackage))
        assertThrows(IllegalArgumentException::class.java) { verification(other, wrongPackage) }
        val original = record(); val first = candidate(release(original)); val checked = verification(first, original)
        assertThrows(IllegalArgumentException::class.java) { UpdatePolicy.action(first.copy(apk = first.apk.copy(id = 9)), checked, installed()) }
        assertThrows(IllegalArgumentException::class.java) { UpdatePolicy.action(first.copy(verification = first.verification!!.copy(id = 10)), checked, installed()) }
        val stringCode = record().put("versionCode", "4"); val coerced = candidate(release(stringCode))
        assertThrows(IllegalArgumentException::class.java) { verification(coerced, stringCode) }
    }

    @Test fun formalReleaseRequiresOneOfficialUploadedApkAndUsableDigest() {
        assertNull(UpdatePolicy.release(release().put("prerelease", true).toString()))
        val multiple = release(); multiple.getJSONArray("assets").put(asset(3, "other.apk", payload))
        assertThrows(IllegalArgumentException::class.java) { UpdatePolicy.release(multiple.toString()) }
        val foreign = release(); foreign.getJSONArray("assets").getJSONObject(0).put("browser_download_url", "https://github.com.evil.test/download/keepsite.apk")
        assertThrows(IllegalArgumentException::class.java) { UpdatePolicy.release(foreign.toString()) }
        val traversal = release(tag = "../other")
        val apk = traversal.getJSONArray("assets").getJSONObject(0)
        apk.put("browser_download_url", apk.getString("browser_download_url").replace("/../", "/%2e%2e/"))
        assertThrows(IllegalArgumentException::class.java) { UpdatePolicy.release(traversal.toString()) }
        val missingHash = release(); missingHash.getJSONArray("assets").getJSONObject(0).remove("digest")
        assertThrows(IllegalArgumentException::class.java) { UpdatePolicy.release(missingHash.toString()) }
    }

    @Test fun signingTrustComesFromInstalledPackageForBothDistributionAndDevelopment() {
        val publishedRecord = record(); val publishedCandidate = candidate(release(publishedRecord))
        val publishedChecked = verification(publishedCandidate, publishedRecord)
        assertTrue(UpdatePolicy.newerArchive(publishedCandidate, publishedChecked, installed(), installed(4), 35))
        val devRecord = record(signer = development); val devCandidate = candidate(release(devRecord)); val devChecked = verification(devCandidate, devRecord)
        assertTrue(UpdatePolicy.newerArchive(devCandidate, devChecked, installed(signer = development), installed(4, development), 35))
        assertThrows(IllegalArgumentException::class.java) { UpdatePolicy.newerArchive(publishedCandidate, publishedChecked, installed(signer = development), installed(4), 35) }
    }

    @Test fun actualArchiveMustMatchRecordAndDeviceRatherThanOnlyRemoteVersion() {
        val record = record(); val candidate = candidate(release(record)); val checked = verification(candidate, record)
        assertThrows(IllegalArgumentException::class.java) { UpdatePolicy.newerArchive(candidate, checked, installed(), installed(5), 35) }
        assertThrows(IllegalArgumentException::class.java) { UpdatePolicy.newerArchive(candidate, checked, installed(), installed(4).copy(name = "another.app"), 35) }
        assertThrows(IllegalArgumentException::class.java) { UpdatePolicy.newerArchive(candidate, checked, installed(), installed(4).copy(minSdk = 36), 35) }
        assertFalse(UpdatePolicy.newerArchive(candidate, checked, installed(4), installed(4), 35))
    }

    @Test fun privateFileVerificationRejectsCorruptionTruncationAndForeignPathsAndCanCancel() {
        val files = UpdateFiles(temporary.newFolder("updates")); val candidate = candidate(release(null)); val file = files.candidateFile(candidate)
        file.writeBytes(payload); files.verify(file, candidate)
        file.writeBytes(ByteArray(payload.size))
        assertThrows(IllegalArgumentException::class.java) { files.verify(file, candidate) }
        file.writeBytes(payload.copyOf(payload.size - 1))
        assertThrows(IllegalArgumentException::class.java) { files.verify(file, candidate) }
        file.writeBytes(payload)
        assertThrows(CancellationException::class.java) { files.verify(file, candidate) { true } }
        val foreign = File(temporary.newFolder("other"), file.name).apply { writeBytes(payload) }
        assertThrows(IllegalArgumentException::class.java) { files.verify(foreign, candidate) }
        assertArrayEquals(payload, foreign.readBytes())
    }
}
