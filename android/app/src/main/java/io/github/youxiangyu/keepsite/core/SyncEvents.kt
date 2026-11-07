package io.github.youxiangyu.keepsite.core

import io.github.youxiangyu.keepsite.peer.EventPort
import io.github.youxiangyu.keepsite.peer.EventState
import io.github.youxiangyu.keepsite.peer.sendPeerMessage
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import org.json.JSONObject
import java.io.Closeable
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

class SyncEvents(
    private val api: HttpApi, private val userId: String, private val hint: (String, Long) -> Unit,
    private val changed: (Boolean) -> Unit, private val authCheck: () -> Unit,
    private val diagnostics: NetworkDiagnostics? = null, private val diagnosticAccount: NetworkMonitor.Account? = null,
) : EventPort, Closeable {
    private val executor = Executors.newSingleThreadScheduledExecutor()
    private val connections = CopyOnWriteArrayList<() -> Unit>()
    private val messages = CopyOnWriteArrayList<(JSONObject) -> Unit>()
    private val epoch = api.epoch()
    @Volatile private var ended = false
    @Volatile private var view = EventState(false, "", 0)
    @Volatile private var socket: WebSocket? = null
    private var alignedServer = ""
    private var ready = false
    private var retryMs = 1000L
    private var watchdog: ScheduledFuture<*>? = null
    private var retry: ScheduledFuture<*>? = null
    private var observation: SocketObservation? = null

    init { executor.execute(::connect) }
    override fun state() = view
    override fun subscribeConnection(listener: () -> Unit): Closeable { connections.add(listener); return Closeable { connections.remove(listener) } }
    override fun subscribeMessages(listener: (JSONObject) -> Unit): Closeable { messages.add(listener); return Closeable { messages.remove(listener) } }
    private fun valid() = !ended && epoch == api.epoch()
    private fun publish() {
        view = view.copy(connected = valid() && ready && view.serverId == alignedServer && alignedServer.isNotEmpty())
        observation?.aligned(view.connected)
        connections.forEach { it() }; changed(view.connected)
    }
    fun align(serverId: String) { dispatch { alignedServer = serverId; publish() } }
    private fun dispatch(block: () -> Unit) {
        if (!ended) try { executor.execute { if (valid()) block() } } catch (_: java.util.concurrent.RejectedExecutionException) { }
    }
    override fun send(message: JSONObject, observation: PeerCommand?): Boolean {
        val current = socket
        if (!view.connected || !valid() || current == null) return false
        val state = view
        return sendPeerMessage(message, state, diagnostics, observation, current::queueSize) { raw ->
            if (socket !== current || view.generation != state.generation || !view.connected || !valid()) false else current.send(raw)
        }
    }
    private fun connect() {
        if (!valid() || socket != null) return
        val generation = view.generation + 1
        ready = false; observation = diagnostics?.socket(diagnosticAccount, epoch, generation)
        view = EventState(false, "", generation); publish()
        val listener = object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) = dispatch {
                if (socket === webSocket) observation?.opened()
            }
            override fun onMessage(webSocket: WebSocket, text: String) = dispatch {
                if (socket !== webSocket) return@dispatch
                try {
                    val bytes = text.toByteArray().size
                    if (bytes > 65536) throw IllegalArgumentException()
                    val value = JSONObject(text)
                    if (Wire.number(value, "v") != 1L || Wire.text(value, "userId", 200) != userId) throw IllegalArgumentException()
                    val server = Wire.text(value, "serverId", 200)
                    val type = Wire.text(value, "type", 60)
                    if (type in setOf("sync.ready", "sync.hint")) {
                        if (bytes > 4096) throw IllegalArgumentException()
                        val cursor = Wire.number(value, "cursor")
                        if (ready && view.serverId != server) {
                            hint(server, cursor); disconnect(webSocket); return@dispatch
                        }
                        val initial = !ready
                        val capabilities = SocketCapabilities.read(value)
                        ready = true; retryMs = 1000; view = view.copy(serverId = server,
                            heartbeatMs = capabilities.heartbeatMs, diagnosticsVersion = capabilities.diagnosticsVersion)
                        observation?.received(type, bytes, capabilities, server, cursor)
                        watchdog?.cancel(false)
                        watchdog = executor.schedule({ disconnect(webSocket, error = MonitorError.TIMEOUT) }, 45, TimeUnit.SECONDS)
                        publish()
                        // Every initial handshake also repairs a lost notification.
                        hint(server, if (initial) maxOf(0L, cursor) else cursor)
                    } else {
                        val maximum = if (type == "peer.signal" && value.optString("kind") != "ice") 65536 else 4096
                        if (bytes > maximum || !ready || server != view.serverId) throw IllegalArgumentException()
                        observation?.received(type, bytes)
                        if (view.connected) messages.forEach { it(value) }
                    }
                } catch (_: Exception) { disconnect(webSocket, error = MonitorError.PROTOCOL) }
            }
            override fun onMessage(webSocket: WebSocket, bytes: ByteString) = dispatch { disconnect(webSocket) }
            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) = dispatch {
                if (code == 1008) authCheck()
                disconnect(webSocket, code)
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = dispatch { disconnect(webSocket, code) }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) = dispatch {
                if (response?.code in setOf(401, 403)) authCheck()
                val error = when (response?.code) { 401 -> MonitorError.AUTH_REQUIRED; 403 -> MonitorError.AUTH_DENIED; else -> MonitorError.NETWORK }
                response?.close(); disconnect(webSocket, error = error)
            }
        }
        socket = api.events(listener, epoch)
        val current = socket
        watchdog?.cancel(false)
        watchdog = executor.schedule({ if (!ready && current != null) disconnect(current, error = MonitorError.TIMEOUT) }, 8, TimeUnit.SECONDS)
    }
    private fun disconnect(current: WebSocket, code: Int? = null, error: MonitorError = MonitorError.NONE) {
        if (socket !== current) return
        socket = null; current.cancel(); ready = false; watchdog?.cancel(false)
        observation?.closed(code, if (valid()) retryMs else null, error); observation = null
        view = view.copy(connected = false, generation = view.generation + 1, heartbeatMs = null, diagnosticsVersion = null); publish()
        if (valid() && retry?.isDone != false) {
            retry = executor.schedule({ retry = null; connect() }, retryMs, TimeUnit.MILLISECONDS)
            retryMs = minOf(30_000, retryMs * 2)
        }
    }
    override fun close() {
        ended = true; socket?.cancel(); socket = null; watchdog?.cancel(false); retry?.cancel(false)
        observation?.closed(); observation = null
        view = view.copy(connected = false, generation = view.generation + 1, heartbeatMs = null, diagnosticsVersion = null)
        connections.forEach { it() }; changed(false)
        connections.clear(); messages.clear(); executor.shutdownNow()
    }
}
