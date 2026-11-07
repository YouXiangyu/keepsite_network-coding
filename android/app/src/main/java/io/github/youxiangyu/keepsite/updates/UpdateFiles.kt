package io.github.youxiangyu.keepsite.updates

import java.io.File
import java.security.MessageDigest
import java.util.concurrent.CancellationException

/** Operates only inside the caller's update directory; never the media or account cache. */
class UpdateFiles(directory: File) {
    private val directory = directory.canonicalFile

    fun candidateFile(candidate: UpdatePolicy.Candidate): File {
        require(candidate.apk.id > 0)
        UpdatePolicy.requireSha256(candidate.apk.sha256)
        return File(directory, "${candidate.apk.id}-${candidate.apk.sha256}.apk")
    }

    fun verify(file: File, candidate: UpdatePolicy.Candidate, cancelled: () -> Boolean = { false }) {
        val actual = file.canonicalFile
        require(actual.parentFile == directory && actual == candidateFile(candidate).canonicalFile) { "安装包不在本次更新的私有目录内。" }
        require(candidate.apk.bytes in 1..UpdatePolicy.MAX_APK_BYTES && actual.isFile && actual.length() == candidate.apk.bytes) { "安装包缺失或大小不符。" }
        val digest = MessageDigest.getInstance("SHA-256")
        var count = 0L
        actual.inputStream().use { input ->
            val block = ByteArray(64 * 1024)
            while (true) {
                if (cancelled()) throw CancellationException("更新校验已取消。")
                val read = input.read(block)
                if (read < 0) break
                count += read
                require(count <= candidate.apk.bytes) { "安装包超过预期大小。" }
                digest.update(block, 0, read)
            }
        }
        if (cancelled()) throw CancellationException("更新校验已取消。")
        require(count == candidate.apk.bytes && UpdatePolicy.hex(digest.digest()) == candidate.apk.sha256) { "安装包完整性校验未通过。" }
    }
}
