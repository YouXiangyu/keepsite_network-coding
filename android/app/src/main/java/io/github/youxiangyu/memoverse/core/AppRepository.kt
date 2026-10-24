package io.github.youxiangyu.memoverse.core

import android.content.Context
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.util.AtomicFile
import android.util.Base64
import io.github.youxiangyu.memoverse.BuildConfig
import io.github.youxiangyu.memoverse.peer.NativePeer
import org.json.JSONObject
import java.io.Closeable
import java.io.File
import java.io.FileOutputStream
import java.security.SecureRandom
import java.util.UUID
import java.util.concurrent.CancellationException
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

class AppRepository(context: Context) : AppController {
    private val context = context.applicationContext
    private val main = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor()
    private val mediaWorkers = Executors.newFixedThreadPool(4)
    private val listeners = CopyOnWriteArrayList<(AppState) -> Unit>()
    private val mediaJobs = CopyOnWriteArrayList<MediaJob>()
    private val secrets = SecretStore(context)
    private val api = HttpApi(BuildConfig.API_BASE_URL, secrets)
    private val lock = Any()
    private val actionScope = ThreadLocal<Long>()
    @Volatile private var state = AppState()
    @Volatile private var foreground = false
    @Volatile private var closed = false
    @Volatile private var peerWanted = true
    @Volatile private var peer: NativePeer? = null
    @Volatile private var cache: FileCache? = null
    private var cacheSubscription: Closeable? = null
    private var events: SyncEvents? = null
    private var sync: SyncEngine? = null
    private var accountId: String? = null
    private var draft: Draft? = null
    private val syncing = AtomicBoolean()
    private val pending = HashMap<String, Pair<String, JSONObject>>()

    private data class Draft(
        val ticketId: String?, val createId: String, val fields: TicketFields, val initialFields: TicketFields,
        val operationId: String, val uploadId: String, val photo: File? = null, val mime: String = "",
        val pending: Boolean = false, val uploaded: MediaRef? = null, val uploadPending: Boolean = false,
    ) {
        val awaitingConfirmation get() = pending || uploadPending
        fun view() = DraftView(ticketId, fields, photo != null, if (photo != null) "已选择原票照片" else "",
            operationId, uploadId, awaitingConfirmation, photo)
    }

    private fun current(expected: Long) = !closed && expected == api.epoch()
    private fun guard(expected: Long) { if (!current(expected)) throw CancellationException() }
    private fun publish(expected: Long? = actionScope.get(), predicate: () -> Boolean = { true }, update: (AppState) -> AppState) {
        val latest = synchronized(lock) {
            if (closed || expected?.let { it != api.epoch() } == true || !predicate()) return
            update(state).also { state = it }
        }
        main.post { if (!closed && state === latest) listeners.forEach { it(latest) } }
    }
    override fun snapshot() = state
    override fun subscribe(listener: (AppState) -> Unit): Closeable {
        listeners.add(listener); main.post { if (!closed && listener in listeners) listener(state) }
        return Closeable { listeners.remove(listener) }
    }
    private fun run(label: String = "", expected: Long = api.epoch(), completed: () -> Unit = {},
                    allowed: () -> Boolean = { true }, block: (Long) -> Unit) {
        val accepted = synchronized(lock) {
            if (!current(expected) || !allowed() || label.isNotEmpty() && state.busy.isNotEmpty()) false
            else {
                if (label.isNotEmpty()) publish(expected) { it.copy(busy = label, error = "", message = "") }
                true
            }
        }
        if (!accepted) { completed(); return }
        val action = Runnable {
            actionScope.set(expected)
            try {
                synchronized(lock) { guard(expected); if (!allowed()) throw CancellationException() }
                api.withScope(expected) { block(expected) }; guard(expected)
            }
            catch (_: CancellationException) { }
            catch (failure: Exception) {
                if (current(expected)) {
                    if (failure is ApiFailure && failure.status == 401 && api.session?.authenticated == true) expire(expected)
                    else publish(expected, predicate = allowed) { it.copy(error = failure.message?.take(500) ?: "操作未完成，请重试。") }
                }
            } finally {
                completed(); if (current(expected) && label.isNotEmpty()) publish(expected) { it.copy(busy = "") }
                actionScope.remove()
            }
        }
        try { worker.execute(action) }
        catch (_: java.util.concurrent.RejectedExecutionException) { completed() }
    }
    override fun clearNotice() = publish { it.copy(error = "", message = "") }
    override fun setForeground(foreground: Boolean) {
        synchronized(lock) {
            if (closed || this.foreground == foreground) return
            this.foreground = foreground
            if (!foreground) {
                api.cancelRequests(); stopNetwork(); mediaJobs.forEach(MediaJob::close)
                publish { it.copy(connected = false, busy = "", peer = it.peer.copy(registered = false, sending = 0, receiving = 0)) }
            }
        }
        if (foreground) refresh()
    }
    private fun stopNetwork() {
        peer?.close(); peer = null; events?.close(); events = null
    }
    private fun stopAccount() {
        stopNetwork(); mediaJobs.forEach(MediaJob::close)
        cacheSubscription?.close(); cacheSubscription = null; cache?.close(); cache = null
        sync = null; accountId = null; draft = null; pending.clear()
    }
    private fun expire(expected: Long) {
        synchronized(lock) {
            if (!current(expected)) return
            api.clearSession(); stopAccount()
            // This intentional invalidation owns the new anonymous generation.
            actionScope.set(api.epoch())
            publish { AppState(session = Session(configured = it.session?.configured ?: true,
                emailEnabled = it.session?.emailEnabled ?: true, googleEnabled = it.session?.googleEnabled ?: false), error = "登录状态已失效，请重新登录。") }
        }
    }
    override fun refresh() = refresh(api.epoch())
    private fun refresh(expected: Long, owner: SyncEvents? = null) = run(expected = expected,
        allowed = { owner == null || events === owner && foreground }) {
        val session = Wire.session(api.json("/api/auth/session", authenticated = false))
        guard(it); install(session, it)
        if (session.authenticated && foreground) synchronize(it)
    }
    private fun install(session: Session, expected: Long) {
        synchronized(lock) {
            guard(expected)
            if (session.userId != accountId) {
                stopAccount()
                publish { AppState(session = session, busy = it.busy, peer = PeerView(enabled = peerWanted)) }
                accountId = session.userId
                if (session.userId != null) {
                    val directory = File(context.cacheDir, "media-v1/${sha256(session.userId.toByteArray())}")
                    val created = FileCache(directory)
                    cache = created
                    cacheSubscription = created.subscribe { publish(predicate = { cache === created }) { it.copy(cache = created.info()) } }
                    sync = SyncEngine(session.userId) { path -> api.json(path) }
                    restoreDraft()
                }
            }
            api.session = session
            publish { it.copy(session = session, error = "") }
            if (session.authenticated && foreground && events == null) startNetwork(expected)
        }
    }
    private fun startNetwork(expected: Long) {
        val userId = accountId ?: return
        val local = cache ?: return
        val owner = java.util.concurrent.atomic.AtomicReference<SyncEvents>()
        val port = SyncEvents(api, userId, hint = { server, cursor ->
            owner.get()?.let { source ->
                if (sync?.current?.serverId != server || (sync?.current?.cursor ?: -1) < cursor) scheduleSync(expected, source)
            }
        }, changed = { connected ->
            publish(expected, predicate = { owner.get() != null && events === owner.get() }) { it.copy(connected = connected) }
        }, authCheck = {
            owner.get()?.let { refresh(expected, it) }
        })
        owner.set(port)
        events = port
        if (peerWanted) peer = createPeer(userId, port, local, expected)
        sync?.current?.let { port.align(it.serverId) }
    }
    private fun scheduleSync(expected: Long, owner: SyncEvents) {
        if (!syncing.compareAndSet(false, true)) return
        run(expected = expected, completed = { syncing.set(false) },
            allowed = { events === owner && foreground }) { synchronize(it) }
    }
    private fun synchronize(expected: Long, receipt: Receipt? = null) {
        guard(expected)
        val engine = sync ?: throw ApiFailure(401, message = "请先登录。")
        val value = engine.sync(receipt)
        guard(expected)
        cache?.setRefs(value.tickets.flatMap { listOfNotNull(it.original, it.poster) })
        guard(expected)
        publish { it.copy(tickets = value.tickets.sortedWith(compareByDescending<Ticket> { ticket -> ticket.createdAt }.thenBy { ticket -> ticket.id }),
            ready = true, cursor = value.cursor, capabilities = value.capabilities, error = "") }
        events?.align(value.serverId)
        cache?.refresh()
    }
    private fun authAction(label: String, path: String, body: JSONObject, success: String) = run(label) { expected ->
        api.json(path, "POST", body, false); guard(expected); publish { it.copy(message = success) }
    }
    override fun login(email: String, password: String) = run("正在登录") { expected ->
        val session = Wire.session(api.json("/api/auth/login", "POST", JSONObject().put("email", email).put("password", password), false))
        if (!session.authenticated) Wire.invalid()
        guard(expected); install(session, expected); if (foreground) synchronize(expected)
    }
    override fun register(email: String, password: String) = authAction("正在注册", "/api/auth/register",
        JSONObject().put("email", email).put("password", password), "请查收验证邮件，并输入验证码。")
    override fun confirmEmail(email: String, code: String) = authAction("正在验证邮箱", "/api/auth/verify-email",
        JSONObject().put("email", email).put("code", code), "邮箱已验证，可以登录。")
    override fun resendEmail(email: String) = authAction("正在发送验证码", "/api/auth/resend-code", JSONObject().put("email", email), "验证码请求已提交，请查收邮件。")
    override fun resetPassword(email: String) = authAction("正在发送重置邮件", "/api/auth/forgot-password", JSONObject().put("email", email), "重置请求已提交，请查收邮件。")
    override fun confirmPassword(email: String, code: String, password: String) = authAction("正在重置密码", "/api/auth/reset-password",
        JSONObject().put("email", email).put("code", code).put("password", password), "密码已重置，请重新登录。")
    override fun refreshAccountMethods() = run("正在读取登录方式") { expected ->
        val methods = Wire.accountMethods(api.json("/api/auth/account/methods"))
        guard(expected); publish { it.copy(accountMethods = methods) }
    }
    override fun addAccountPassword(action: String, email: String, password: String, code: String) = run("正在添加邮箱密码") { expected ->
        require(action in setOf("register", "confirm", "link"))
        val body = JSONObject().put("email", email).put("password", password)
        if (action == "confirm") body.put("code", code)
        val result = api.json("/api/auth/account/password/$action", "POST", body)
        guard(expected)
        if (action == "register") {
            if (!Wire.bool(result, "verificationRequired")) Wire.invalid()
            val verifiedEmail = Wire.text(result, "email", 254)
            publish { it.copy(accountPasswordEmail = verifiedEmail, message = "请查收验证邮件，再输入验证码和刚设置的密码。") }
        } else {
            val methods = Wire.accountMethods(result)
            publish { it.copy(accountMethods = methods, accountPasswordEmail = null, message = "邮箱密码登录方式已添加。") }
        }
    }
    override fun startGoogle(openBrowser: (String) -> Unit) = startGoogle(false, openBrowser)
    override fun linkGoogle(openBrowser: (String) -> Unit) = startGoogle(true, openBrowser)
    private fun startGoogle(link: Boolean, openBrowser: (String) -> Unit) = run(if (link) "正在打开 Google 绑定" else "正在打开 Google 登录") { expected ->
        val verifier = randomProof(); val appState = randomProof()
        val challenge = Base64.encodeToString(java.security.MessageDigest.getInstance("SHA-256").digest(verifier.toByteArray()), Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
        val prefix = if (link) "/api/auth/account/google/native" else "/api/auth/native/google"
        val result = api.json("$prefix/start", "POST", JSONObject().put("challenge", challenge).put("state", appState), link)
        guard(expected)
        val url = Wire.text(result, "browserUrl", 4096)
        val parsed = Uri.parse(url)
        val origin = Uri.parse(api.origin)
        val launchPath = if (link) "/api/auth/account/google/launch" else "/api/auth/native/google/launch"
        if (parsed.scheme != "https" || parsed.authority != origin.authority || parsed.path != launchPath
            || parsed.fragment != null || parsed.queryParameterNames != setOf("flow") || parsed.getQueryParameter("flow").isNullOrEmpty()) Wire.invalid()
        val expires = Wire.number(result, "expiresAt")
        if (expires <= System.currentTimeMillis() || expires > System.currentTimeMillis() + 11 * 60_000) Wire.invalid()
        secrets.write("google-proof", NativeGoogleProof(verifier, appState, expires, if (link) "link" else "login", if (link) accountId else null).toJson().toString())
        main.post { if (current(expected)) openBrowser(url) }
    }
    override fun handleGoogleCallback(uri: Uri) = run("正在完成 Google 登录") { expected ->
        val saved = secrets.read("google-proof")?.let(::JSONObject)?.let(NativeGoogleProof::fromJson) ?: throw ApiFailure(message = "登录流程已结束，请重新发起。")
        // A browser return may recreate the process before onStart refreshes the stored session.
        if (saved.link && api.session == null) {
            val session = Wire.session(api.json("/api/auth/session", authenticated = false))
            guard(expected); install(session, expected)
        }
        val query = saved.callback(uri.toString(), accountId, System.currentTimeMillis())
        if (query["error"] != null) { secrets.clear("google-proof"); throw ApiFailure(message = "Google 授权未完成，请重试。") }
        val link = saved.link
        val result = api.json(saved.redeemPath, "POST", saved.redeemBody(query), link)
        guard(expected); secrets.clear("google-proof")
        if (link) {
            val methods = Wire.accountMethods(result)
            publish { it.copy(accountMethods = methods, message = "Google 登录方式已绑定。") }
            return@run
        }
        val session = Wire.session(result)
        if (!session.authenticated) Wire.invalid()
        install(session, expected); if (foreground) synchronize(expected)
    }
    private fun randomProof(): String = Base64.encodeToString(ByteArray(32).also(SecureRandom()::nextBytes), Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
    override fun logout() {
        synchronized(lock) {
            if (closed) return
            api.cancelRequests(); stopAccount(); secrets.clear("google-proof")
            val previous = state.session
            publish { AppState(session = previous?.copy(userId = null, csrfToken = "", email = "", name = ""), busy = "正在退出") }
        }
        worker.execute {
            var error = ""
            try { api.json("/api/auth/logout", "POST", JSONObject()) }
            catch (_: Exception) { error = "已在本机退出，服务器会话撤销尚未确认。" }
            finally {
                synchronized(lock) {
                    api.clearSession(); stopAccount()
                    publish { it.copy(session = it.session?.copy(userId = null, csrfToken = "", email = "", name = ""),
                        tickets = emptyList(), ready = false, connected = false, draft = null, busy = "", message = error.ifEmpty { "已退出。" }) }
                }
            }
        }
    }

    private fun draftDirectory(): File {
        val id = accountId ?: throw ApiFailure(401, message = "请先登录。")
        return File(context.noBackupFilesDir, "draft-v1/${sha256(id.toByteArray())}").apply { mkdirs() }
    }
    private fun persistDraft(active: Draft) {
        val file = AtomicFile(File(draftDirectory(), "draft.json"))
        val body = JSONObject().put("ticketId", active.ticketId ?: JSONObject.NULL).put("createId", active.createId)
            .put("fields", Wire.fieldsJson(active.fields)).put("initialFields", Wire.fieldsJson(active.initialFields))
            .put("operationId", active.operationId).put("uploadId", active.uploadId).put("pending", active.pending).put("uploadPending", active.uploadPending)
            .put("photo", active.photo?.name ?: JSONObject.NULL).put("mime", active.mime)
        val output = file.startWrite()
        try { output.write(body.toString().toByteArray()); file.finishWrite(output) }
        catch (failure: Exception) { file.failWrite(output); throw failure }
    }
    private fun restoreDraft() {
        try {
            val directory = draftDirectory()
            val file = File(directory, "draft.json")
            if (!file.isFile || file.length() > 32768) return
            val data = AtomicFile(file).openRead().use { JSONObject(it.readBytes().toString(Charsets.UTF_8)) }
            val filename = Wire.nullable(data, "photo", 100)
            val photo = filename?.let {
                if (!it.matches(Regex("photo-[a-f0-9-]+"))) Wire.invalid()
                File(directory, it).takeIf { selected -> selected.isFile && selected.length() in 1..MAX_IMAGE_BYTES }
                    ?: throw ApiFailure(message = "未提交照片已不可用，请重新选择。")
            }
            draft = Draft(Wire.nullable(data, "ticketId", 120), Wire.id(data, "createId"), Wire.fields(Wire.obj(data, "fields")),
                Wire.fields(Wire.obj(data, "initialFields")), Wire.id(data, "operationId"), Wire.id(data, "uploadId"), photo,
                Wire.string(data, "mime", 50), Wire.bool(data, "pending"),
                uploadPending = if (data.has("uploadPending")) Wire.bool(data, "uploadPending") else false)
            publish { it.copy(draft = draft?.view()) }
        } catch (_: Exception) { publish { it.copy(error = "未提交草稿无法恢复，请重新选择原票。") } }
    }
    private fun setDraft(value: Draft?) {
        if (value != null) persistDraft(value)
        draft = value
        publish { it.copy(draft = value?.view()) }
    }
    override fun newDraft() = run {
        if (draft != null) { publish { it.copy(message = "请先完成或放弃当前草稿。") }; return@run }
        if (accountId == null) throw ApiFailure(401, message = "请先登录。")
        setDraft(Draft(null, UUID.randomUUID().toString(), TicketFields(), TicketFields(), UUID.randomUUID().toString(), UUID.randomUUID().toString()))
    }
    override fun editDraft(ticketId: String) = run {
        if (draft != null) { publish { it.copy(message = "请先完成或放弃当前草稿。") }; return@run }
        val ticket = state.tickets.find { it.id == ticketId } ?: throw ApiFailure(message = "该票根已不可用。")
        setDraft(Draft(ticket.id, ticket.id, ticket.fields, ticket.fields, UUID.randomUUID().toString(), UUID.randomUUID().toString()))
    }
    override fun setDraftFields(fields: TicketFields) = run {
        val active = draft ?: return@run
        if (active.awaitingConfirmation) return@run
        setDraft(active.copy(fields = fields))
    }
    override fun selectPhoto(uri: Uri) = run("正在读取照片") { expected ->
        val active = draft ?: throw ApiFailure(message = "请先新建或编辑票根。")
        if (active.awaitingConfirmation) throw ApiFailure(message = "请先核对上次提交结果。")
        val bytes = context.contentResolver.openInputStream(uri)?.use { stream ->
            val output = java.io.ByteArrayOutputStream(); val buffer = ByteArray(16 * 1024)
            while (true) {
                guard(expected); val size = stream.read(buffer); if (size < 0) break
                if (output.size().toLong() + size > MAX_IMAGE_BYTES) throw ApiFailure(message = "图片超过 12 MiB。")
                output.write(buffer, 0, size)
            }
            output.toByteArray()
        } ?: throw ApiFailure(message = "无法读取所选照片。")
        val ref = ImageFiles.inspect(bytes); guard(expected)
        val file = File(draftDirectory(), "photo-${UUID.randomUUID()}")
        try {
            FileOutputStream(file).use { out -> out.write(bytes); out.fd.sync() }
            guard(expected)
            setDraft(active.copy(photo = file, mime = ref.mimeType, uploadId = UUID.randomUUID().toString(), uploaded = null))
            active.photo?.delete()
        } catch (failure: Exception) { file.delete(); throw failure }
    }
    private fun applied(operationId: String): Receipt? {
        val value = api.json("/api/operations/$operationId")
        if (Wire.id(value, "operationId") != operationId) Wire.invalid()
        if (!Wire.bool(value, "applied")) {
            if (!value.has("committedCursor") || !value.isNull("committedCursor")) Wire.invalid()
            return null
        }
        return Wire.receipt(value, operationId)
    }
    override fun saveDraft() = run("正在保存并核对") { expected ->
        var active = draft ?: throw ApiFailure(message = "当前没有草稿。")
        Wire.validateFields(active.fields)
        if (active.pending) {
            val receipt = applied(active.operationId)
            if (receipt != null) {
                synchronize(expected, receipt); finishDraft(active, expected); return@run
            }
        }
        if (active.ticketId == null && active.photo == null) throw ApiFailure(message = "请先选择原票照片。")
        val fields = JSONObject()
        FIELD_NAMES.zip(active.fields.values()).forEachIndexed { index, (key, value) ->
            if (active.ticketId == null && value.isNotEmpty() || active.ticketId != null && value != active.initialFields.values()[index]) fields.put(key, value.trim())
        }
        if (active.ticketId != null && fields.length() == 0 && active.photo == null) throw ApiFailure(message = "还没有修改内容。")
        val body = JSONObject().put("operationId", active.operationId).put("fields", fields)
        if (active.photo != null) {
            val uploaded = prepareDraftUpload(prepare = {
                var result = active.uploaded
                if (result == null) {
                    try {
                        val status = api.json("/api/uploads/${active.uploadId}")
                        if (Wire.id(status, "uploadId") != active.uploadId) Wire.invalid()
                        val kind = Wire.text(status, "status", 20)
                        if (kind in setOf("stored", "attached")) result = Wire.media(Wire.obj(status, "asset"), true)
                        else if (kind != "pending" || !status.has("asset") || !status.isNull("asset")) Wire.invalid()
                    } catch (failure: ApiFailure) { if (failure.status != 404) throw failure }
                }
                if (result == null) {
                    // Local preparation cannot leave a fresh draft waiting for a request never sent.
                    ImageFiles.inspect(active.photo!!.readBytes()); guard(expected)
                    active = active.copy(uploadPending = true); setDraft(active)
                    result = api.upload(active.photo!!, active.mime, active.uploadId)
                }
                result
            }, rejected = { if (current(expected)) setDraft(active.copy(uploadPending = false)) })
            guard(expected)
            active = active.copy(uploaded = uploaded, uploadPending = false); setDraft(active)
            body.put("originalAssetId", uploaded.assetId)
        }
        val path: String
        val method: String
        if (active.ticketId == null) { path = "/api/tickets"; method = "POST"; body.put("id", active.createId) }
        else { path = "/api/tickets/${active.ticketId}"; method = "PATCH" }
        active = active.copy(pending = true); setDraft(active)
        confirmDraftSubmission(active.operationId,
            write = { api.json(path, method, body) },
            rejected = { if (current(expected)) setDraft(active.copy(pending = false)) },
            confirm = { receipt -> guard(expected); synchronize(expected, receipt); finishDraft(active, expected) })
    }
    private fun finishDraft(active: Draft, expected: Long) {
        guard(expected)
        if (draft?.operationId != active.operationId) return
        AtomicFile(File(draftDirectory(), "draft.json")).delete(); active.photo?.delete(); draft = null
        publish { it.copy(draft = null, message = "服务端已确认保存。", error = "") }
    }
    override fun discardDraft() = run { expected ->
        val active = draft ?: return@run
        if (active.awaitingConfirmation) {
            val receipt = applied(active.operationId)
            if (receipt == null) throw ApiFailure(message = "提交结果尚未确认，请先核对并重试保存。")
            synchronize(expected, receipt)
        }
        finishDraft(active, expected)
        publish { it.copy(message = "当前草稿已关闭。") }
    }
    private fun ticketAction(ticketId: String, kind: String) = run(if (kind == "delete") "正在删除" else "正在重新处理") { expected ->
        if (state.tickets.none { it.id == ticketId }) throw ApiFailure(message = "该票根已不可用。")
        val key = "$kind:$ticketId"
        val request = pending.getOrPut(key) { UUID.randomUUID().toString().let { it to JSONObject().put("operationId", it) } }
        val receipt = applied(request.first) ?: Wire.receipt(api.json("/api/tickets/$ticketId" + if (kind == "process") "/process" else "",
            if (kind == "delete") "DELETE" else "POST", request.second), request.first)
        guard(expected); synchronize(expected, receipt); pending.remove(key)
        publish { it.copy(message = if (kind == "delete") "删除已确认。" else "处理任务已登记。") }
    }
    override fun deleteTicket(ticketId: String) = ticketAction(ticketId, "delete")
    override fun retryProcessing(ticketId: String) = ticketAction(ticketId, "process")
    override fun clearCache() = run("正在清除本机缓存") { expected ->
        mediaJobs.forEach(MediaJob::close); cache?.clear(); guard(expected)
        publish { it.copy(message = "本机缓存已清理，云端记录仍保留。") }
    }
    override fun setPeerEnabled(enabled: Boolean) {
        synchronized(lock) {
            peerWanted = enabled
            publish { it.copy(peer = it.peer.copy(enabled = enabled, registered = if (enabled) it.peer.registered else false)) }
            if (!enabled) { peer?.close(); peer = null }
        }
        if (enabled) {
            run { expected ->
                synchronized(lock) {
                    guard(expected)
                    val port = events; val local = cache; val userId = accountId
                    if (peerWanted && foreground && peer == null && port != null && local != null && userId != null) {
                        peer = createPeer(userId, port, local, expected)
                    }
                }
            }
        }
    }
    private fun createPeer(userId: String, port: SyncEvents, local: FileCache, expected: Long): NativePeer {
        val owner = java.util.concurrent.atomic.AtomicReference<NativePeer>()
        return NativePeer(context, userId, port, local) { view ->
            publish(expected, predicate = { owner.get() != null && peer === owner.get() && peerWanted }) { it.copy(peer = view) }
        }.also { owner.set(it) }
    }

    private inner class MediaJob(val expected: Long, val listener: (MediaLoad) -> Unit) : Closeable {
        val cancelled = AtomicBoolean()
        fun check() { guard(expected); if (cancelled.get() || !foreground) throw CancellationException() }
        fun emit(value: MediaLoad) { main.post { if (!cancelled.get() && current(expected) && foreground) listener(value) } }
        override fun close() { cancelled.set(true); mediaJobs.remove(this) }
    }
    override fun loadMedia(media: MediaRef, listener: (MediaLoad) -> Unit): Closeable {
        val job = MediaJob(api.epoch(), listener)
        mediaJobs.add(job)
        val local = cache
        if (local == null || !foreground) { job.emit(MediaLoad(status = "error", error = "图片引用已不可用。")); return job }
        val token = local.token()
        job.emit(MediaLoad(status = "loading"))
        mediaWorkers.execute {
            try {
                job.check()
                if (!local.live(media)) throw ApiFailure(message = "图片引用已不可用。")
                local.file(media)?.let { cached -> job.check(); job.emit(MediaLoad(cached, "cached", "cache")); return@execute }
                val accelerated = try {
                    peer?.fetch(media) { job.cancelled.get() || !current(job.expected) || !foreground || !local.live(media) }
                } catch (_: CancellationException) {
                    job.check()
                    if (!local.live(media)) throw CancellationException()
                    null // Disabling only P2P preserves this foreground media request and its cloud fallback.
                }
                job.check()
                val (bytes, source) = accelerated?.let { it.bytes to "peer" } ?: api.withScope(job.expected) { api.download(media) }
                job.check(); ImageFiles.validate(bytes, media)
                if (local.commit(media, bytes, token)) {
                    job.check(); val file = local.file(media) ?: throw ApiFailure(message = "本机图片已被回收，请重试。")
                    job.emit(MediaLoad(file, "cached", source))
                } else {
                    job.check()
                    job.emit(MediaLoad(status = "temporary", source = source, error = "未写入本机缓存，可临时查看。", bytes = bytes))
                }
            } catch (_: CancellationException) { job.close() }
            catch (failure: Exception) {
                if (current(job.expected) && !job.cancelled.get()) job.emit(MediaLoad(status = "error", error = failure.message?.take(300) ?: "图片读取失败。"))
            }
        }
        return job
    }
    override fun close() {
        synchronized(lock) {
            if (closed) return
            closed = true; foreground = false; api.cancelRequests(); stopAccount(); api.close()
            worker.shutdownNow(); mediaWorkers.shutdownNow(); listeners.clear()
        }
    }
}
