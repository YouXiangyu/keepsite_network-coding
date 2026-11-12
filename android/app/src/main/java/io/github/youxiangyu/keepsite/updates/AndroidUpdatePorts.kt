package io.github.youxiangyu.keepsite.updates

import android.app.DownloadManager
import android.content.Context
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.ParcelFileDescriptor
import java.io.File
import java.net.URI
import java.util.UUID

class AndroidUpdateDownloads(context: Context) : UpdateDownloads {
    private val context = context.applicationContext
    private val manager = this.context.getSystemService(DownloadManager::class.java)
    override fun destination(token: String): String {
        require(UUID.fromString(token).toString() == token)
        val root = requireNotNull(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)) { "更新下载目录不可用。" }
        return Uri.fromFile(File(root, "updates/$token.apk").canonicalFile).toString()
    }
    override fun enqueue(source: String, destination: String, metered: Boolean): Long {
        val file = File(URI(destination))
        val token = file.name.removeSuffix(".apk")
        require(this.destination(token) == destination)
        val request = DownloadManager.Request(Uri.parse(source))
            .setDestinationInExternalFilesDir(context, Environment.DIRECTORY_DOWNLOADS, "updates/$token.apk")
            .setAllowedOverMetered(metered).setAllowedOverRoaming(false)
            // A completion notification opens the external APK directly, bypassing our validation/guard.
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE)
            .setTitle("keepsite 应用更新")
        return manager.enqueue(request)
    }
    override fun tasks(): List<UpdateTask> = manager.query(DownloadManager.Query()).use { cursor ->
        val tasks = mutableListOf<UpdateTask>()
        fun long(column: String) = cursor.getLong(cursor.getColumnIndexOrThrow(column))
        fun text(column: String) = cursor.getString(cursor.getColumnIndexOrThrow(column))
        while (cursor.moveToNext()) {
            require(tasks.size < 1000) { "系统下载任务过多，请稍后重试。" }
            val phase = when (long(DownloadManager.COLUMN_STATUS).toInt()) {
                DownloadManager.STATUS_PENDING -> DownloadPhase.PENDING
                DownloadManager.STATUS_RUNNING -> DownloadPhase.RUNNING
                DownloadManager.STATUS_PAUSED -> DownloadPhase.PAUSED
                DownloadManager.STATUS_SUCCESSFUL -> DownloadPhase.SUCCESS
                else -> DownloadPhase.FAILED
            }
            tasks += UpdateTask(long(DownloadManager.COLUMN_ID), text(DownloadManager.COLUMN_URI), text(DownloadManager.COLUMN_LOCAL_URI), phase,
                long(DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR), long(DownloadManager.COLUMN_TOTAL_SIZE_BYTES), long(DownloadManager.COLUMN_REASON).toInt())
        }
        tasks
    }
    override fun open(id: Long) = ParcelFileDescriptor.AutoCloseInputStream(manager.openDownloadedFile(id))
    override fun remove(id: Long) { manager.remove(id) }
}

class AndroidUpdatePackages(context: Context) : UpdatePackages {
    private val context = context.applicationContext
    override val deviceApi get() = Build.VERSION.SDK_INT
    @Suppress("DEPRECATION") private val flags get() = if (Build.VERSION.SDK_INT >= 28) PackageManager.GET_SIGNING_CERTIFICATES else PackageManager.GET_SIGNATURES
    override fun installed() = identity(context.packageManager.getPackageInfo(context.packageName, flags))
    override fun archive(file: File) = identity(requireNotNull(context.packageManager.getPackageArchiveInfo(file.absolutePath, flags)) { "无法读取安装包身份。" })
    @Suppress("DEPRECATION") private fun identity(info: PackageInfo): UpdatePolicy.PackageIdentity {
        val signatures = if (Build.VERSION.SDK_INT >= 28) info.signingInfo?.apkContentsSigners else info.signatures
        val signers = requireNotNull(signatures) { "安装包签名缺失。" }.map { UpdatePolicy.digest(it.toByteArray()) }.toSet()
        return UpdatePolicy.PackageIdentity(info.packageName, if (Build.VERSION.SDK_INT >= 28) info.longVersionCode else info.versionCode.toLong(),
            signers, requireNotNull(info.applicationInfo).minSdkVersion)
    }
}
