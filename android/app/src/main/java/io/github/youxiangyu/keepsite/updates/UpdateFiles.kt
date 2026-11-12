package io.github.youxiangyu.keepsite.updates

import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.util.concurrent.CancellationException

/** Operates only inside the caller's update directory; never the media or account cache. */
class UpdateFiles(directory: File, private val isProtected: (File) -> Boolean = { false }) {
    private val directory = directory.canonicalFile

    fun candidateFile(candidate: UpdatePolicy.Candidate): File {
        require(candidate.apk.id > 0)
        UpdatePolicy.requireSha256(candidate.apk.sha256)
        return File(directory, "${candidate.apk.id}-${candidate.apk.sha256}.apk")
    }

    fun remove(candidate: UpdatePolicy.Candidate) {
        val target = candidateFile(candidate)
        for (path in listOf(target, File(directory, target.name + ".part"))) {
            val file = path.canonicalFile
            require(file.parentFile == directory) { "不能清理更新目录外的文件。" }
            if (path == target && isProtected(file)) continue
            if (file.exists()) require(file.isFile && file.delete()) { "旧安装包暂时无法清理。" }
        }
    }

    /** The caller owns the input. Only a complete hash-matched internal copy is published. */
    fun stage(input: InputStream, candidate: UpdatePolicy.Candidate, cancelled: () -> Boolean = { false }): File {
        require(candidate.apk.bytes in 1..UpdatePolicy.MAX_APK_BYTES)
        require(directory.isDirectory || directory.mkdirs())
        val target = candidateFile(candidate)
        val part = File(directory, target.name + ".part")
        require(target.canonicalFile.parentFile == directory && part.canonicalFile.parentFile == directory)
        if (isProtected(target.canonicalFile)) { verify(target, candidate, cancelled); return target }
        try {
            val digest = MessageDigest.getInstance("SHA-256")
            var count = 0L
            FileOutputStream(part).use { output ->
                val block = ByteArray(64 * 1024)
                while (true) {
                    if (cancelled()) throw CancellationException("更新校验已取消。")
                    val read = input.read(block)
                    if (read < 0) break
                    count += read
                    require(count <= candidate.apk.bytes) { "安装包超过预期大小。" }
                    digest.update(block, 0, read); output.write(block, 0, read)
                }
                require(count == candidate.apk.bytes && UpdatePolicy.hex(digest.digest()) == candidate.apk.sha256) { "安装包完整性校验未通过。" }
                if (cancelled()) throw CancellationException("更新校验已取消。")
                output.fd.sync()
            }
            Files.move(part.toPath(), target.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
            return target
        } finally { part.delete() }
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
