package io.github.youxiangyu.keepsite.updates

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.UUID

data class UpdateInstallGuard(val resumed: Boolean = false, val focused: Boolean = false, val draft: Boolean = false,
                             val awaitingDraft: Boolean = false, val busy: Boolean = false, val accountInteraction: Boolean = false,
                             val picker: Boolean = false, val authenticated: Boolean = false) {
    fun allows(manual: Boolean) = resumed && focused && !draft && !awaitingDraft && !busy && !accountInteraction && !picker && (manual || authenticated)
    fun shouldRequest(ready: Boolean, working: Boolean, hasCommand: Boolean, automaticAllowed: Boolean, continuation: Boolean, failed: Boolean) =
        !working && !hasCommand && !failed && (ready || continuation) && (automaticAllowed || continuation) && allows(continuation)
    companion object {
        fun accountInputsActive(inputs: Iterable<Pair<Boolean, Boolean>>) = inputs.any { (visible, nonempty) -> visible && nonempty }
    }
}
enum class HandoffKind { PERMISSION, GRANTED, INSTALL, RETURNED }
internal fun allowMeteredForCandidate(requested: Boolean, displayed: String?, candidate: String) = requested && displayed == candidate
data class ProtectedUpdate(val snapshot: UpdateSnapshot, val target: UpdatePolicy.PackageIdentity, val possiblyRead: Boolean) {
    val fileName get() = snapshot.checked().first.apk.let { "${it.id}-${it.sha256}.apk" }
    fun installed(current: UpdatePolicy.PackageIdentity) = current.name == target.name && current.signers == target.signers && current.code >= target.code
}
data class UpdateHandoff(val token: String, val update: ProtectedUpdate, val kind: HandoffKind, val issued: Boolean = false, val previouslyRead: Boolean = false)
data class HandoffState(val files: List<ProtectedUpdate> = emptyList(), val active: UpdateHandoff? = null, val offered: Set<String> = emptySet())
interface HandoffStore { fun read(): HandoffState; fun write(value: HandoffState) }

/** File leases do not block release checks or safe retirement of an obsolete system task. */
class UpdateHandoffs(private val store: HandoffStore) {
    fun state() = store.read()
    fun protects(file: File) = state().files.any { it.fileName == file.name }
    fun prepare(snapshot: UpdateSnapshot, target: UpdatePolicy.PackageIdentity, kind: HandoffKind, manual: Boolean): UpdateHandoff? {
        val saved = state(); val identity = snapshot.checked().first.identity
        val record = snapshot.checked().second
        require(target.name == UpdatePolicy.PACKAGE)
        if (record != null) require(record.code == target.code && setOf(record.certificate) == target.signers)
        if (!manual && identity in saved.offered) return null
        val update = ProtectedUpdate(snapshot, target, false)
        val previous = saved.files.firstOrNull { it.fileName == update.fileName }
        val attempt = UpdateHandoff(UUID.randomUUID().toString(), update, kind, previouslyRead = previous?.possiblyRead == true)
        val files = if (kind == HandoffKind.INSTALL) saved.files.filterNot { it.fileName == update.fileName } + update.copy(possiblyRead = attempt.previouslyRead) else saved.files
        store.write(saved.copy(files = files, active = attempt))
        return attempt
    }
    fun issue(token: String): UpdateHandoff {
        val saved = state(); val active = requireNotNull(saved.active)
        require(active.token == token && !active.issued)
        val files = if (active.kind == HandoffKind.INSTALL) saved.files.map { if (it.fileName == active.update.fileName) it.copy(possiblyRead = true) else it } else saved.files
        val issued = active.copy(issued = true)
        store.write(saved.copy(files = files, active = issued, offered = (saved.offered + active.update.snapshot.checked().first.identity).toList().takeLast(64).toSet()))
        return issued
    }
    /** Caller proves no intent was launched. Unknown/process-lost returns never use this path. */
    fun notLaunched(token: String) {
        val saved = state(); val active = saved.active ?: return
        if (active.token != token) return
        val files = if (active.kind == HandoffKind.INSTALL && !active.previouslyRead) saved.files.filterNot { it.fileName == active.update.fileName } else saved.files
        store.write(saved.copy(files = files, active = if (active.previouslyRead) active.copy(kind = HandoffKind.RETURNED, issued = true) else null))
    }
    fun endPermissionContinuation() {
        val saved = state(); val active = saved.active ?: return
        if (active.kind == HandoffKind.PERMISSION || active.kind == HandoffKind.GRANTED) store.write(saved.copy(active = null))
        else if (active.kind == HandoffKind.INSTALL && !active.issued) notLaunched(active.token)
    }
    fun permissionReturned(token: String, granted: Boolean) {
        val saved = state(); val active = saved.active ?: return
        if (active.kind != HandoffKind.PERMISSION || active.token != token) return
        store.write(saved.copy(active = if (granted) active.copy(kind = HandoffKind.GRANTED, issued = false) else null))
    }
    fun installerReturned(token: String) {
        val saved = state(); val active = saved.active ?: return
        if (active.kind == HandoffKind.INSTALL && active.token == token) store.write(saved.copy(active = active.copy(kind = HandoffKind.RETURNED)))
    }
    fun confirm(current: UpdatePolicy.PackageIdentity): List<ProtectedUpdate> {
        val saved = state(); val finished = saved.files.filter { it.installed(current) }
        val active = saved.active?.takeUnless { it.update.installed(current) }
        if (finished.isNotEmpty() || active != saved.active) store.write(saved.copy(files = saved.files - finished.toSet(), active = active))
        return finished
    }
}

class FileHandoffStore(file: File) : HandoffStore {
    private val file = file.canonicalFile
    private val directory = requireNotNull(this.file.parentFile)
    override fun read(): HandoffState {
        if (!file.exists()) return HandoffState()
        require(file.length() <= 8 * 1024 * 1024) { "安装交接记录过大。" }
        val value = JSONObject(file.readText(Charsets.UTF_8))
        require(value.getInt("schema") == 1)
        val files = value.getJSONArray("files").let { array -> require(array.length() <= 16); (0 until array.length()).map { update(array.getJSONObject(it)) } }
        val offered = value.getJSONArray("offered").let { array -> require(array.length() <= 64); (0 until array.length()).map { array.getString(it).also { text -> require(text.length in 1..500) } }.toSet() }
        val active = if (value.opt("active") == JSONObject.NULL) null else value.getJSONObject("active").let {
            val token = it.getString("token"); require(UUID.fromString(token).toString() == token)
            UpdateHandoff(token, update(it.getJSONObject("update")), HandoffKind.valueOf(it.getString("kind")), boolean(it, "issued"), boolean(it, "previouslyRead"))
        }
        if (active != null && (active.kind == HandoffKind.INSTALL || active.kind == HandoffKind.RETURNED))
            require(files.any { it.fileName == active.update.fileName && it.target == active.update.target && (!active.issued || it.possiblyRead) })
        return HandoffState(files, active, offered)
    }
    override fun write(value: HandoffState) {
        require(value.files.size <= 16 && value.offered.size <= 64)
        val active = value.active?.let { JSONObject().put("token", it.token).put("update", json(it.update)).put("kind", it.kind.name)
            .put("issued", it.issued).put("previouslyRead", it.previouslyRead) }
        val bytes = JSONObject().put("schema", 1).put("files", JSONArray(value.files.map(::json))).put("offered", JSONArray(value.offered.toList()))
            .put("active", active ?: JSONObject.NULL).toString().toByteArray(Charsets.UTF_8)
        require(bytes.size <= 8 * 1024 * 1024); require(directory.isDirectory || directory.mkdirs())
        val temporary = File(directory, file.name + ".tmp")
        FileOutputStream(temporary).use { it.write(bytes); it.fd.sync() }
        Files.move(temporary.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
    }
    private fun update(value: JSONObject): ProtectedUpdate {
        val snapshot = UpdateSnapshot(value.getString("release"), if (value.opt("verification") == JSONObject.NULL) null else value.getString("verification"))
        val (_, record) = snapshot.checked()
        val signers = value.getJSONArray("signers").let { (0 until it.length()).map { index -> it.getString(index) }.toSet() }
        val code = value.get("code").let { require(it is Int || it is Long); (it as Number).toLong() }
        val target = UpdatePolicy.PackageIdentity(UpdatePolicy.PACKAGE, code, signers, value.getInt("minSdk"))
        if (record != null) require(record.code == target.code && setOf(record.certificate) == target.signers)
        return ProtectedUpdate(snapshot, target, boolean(value, "possiblyRead"))
    }
    private fun json(value: ProtectedUpdate) = JSONObject().put("release", value.snapshot.release).put("verification", value.snapshot.verification ?: JSONObject.NULL)
        .put("code", value.target.code).put("signers", JSONArray(value.target.signers.toList())).put("minSdk", value.target.minSdk).put("possiblyRead", value.possiblyRead)
    private fun boolean(value: JSONObject, name: String) = requireNotNull(value.opt(name) as? Boolean)
}
