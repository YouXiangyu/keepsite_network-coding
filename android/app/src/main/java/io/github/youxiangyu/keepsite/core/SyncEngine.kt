package io.github.youxiangyu.keepsite.core

import org.json.JSONObject
import java.net.URLEncoder

data class Synced(val serverId: String, val cursor: Long, val tickets: List<Ticket>, val capabilities: Capabilities)

/** One serial consumer. A partially fetched snapshot is never published. */
class SyncEngine(private val userId: String, private val read: (String) -> JSONObject) {
    var current: Synced? = null
        private set

    fun invalidate() { current = null }

    private fun identity(page: JSONObject, serverId: String? = null): String {
        if (Wire.text(page, "userId", 200) != userId) throw ApiFailure(401, message = "账号状态已改变，请重新登录。")
        val server = Wire.text(page, "serverId", 200)
        if (serverId != null && server != serverId) gap("server_changed")
        return server
    }

    private fun snapshot(): Synced {
        val tickets = LinkedHashMap<String, Ticket>()
        val tokens = HashSet<String>()
        var first: JSONObject? = null
        var token: String? = null
        var bytes = 0
        do {
            val page = read("/api/sync/snapshot?limit=100" + (token?.let { "&pageToken=" + URLEncoder.encode(it, "UTF-8") } ?: ""))
            identity(page)
            val initial = first ?: page.also { first = it }
            val total = Wire.number(page, "total", 0, 10_000)
            if (listOf("serverId", "snapshotId").any { Wire.text(page, it) != Wire.text(initial, it) }
                || listOf("cursor", "total", "expiresAt").any { Wire.number(page, it) != Wire.number(initial, it) }
                || Wire.number(page, "offset") != tickets.size.toLong()) gap("snapshot_expired")
            val items = Wire.array(page, "tickets")
            if (items.length() > 200) Wire.invalid()
            bytes += items.toString().toByteArray(Charsets.UTF_8).size
            if (bytes > 16 * 1024 * 1024) throw ApiFailure(message = "票夹快照超过容量限制。")
            for (i in 0 until items.length()) {
                val ticket = Wire.ticket(items.opt(i) as? JSONObject ?: Wire.invalid())
                if (tickets.put(ticket.id, ticket) != null) Wire.invalid()
            }
            token = Wire.nullable(page, "nextPageToken", 200)
            if (token != null && (token.isEmpty() || items.length() == 0 || !tokens.add(token))) Wire.invalid()
            if (tickets.size > total || (tickets.size < total) != (token != null)) Wire.invalid()
        } while (token != null)
        val start = first ?: Wire.invalid()
        return Synced(Wire.text(start, "serverId"), Wire.number(start, "cursor"), tickets.values.toList(), Wire.capabilities(Wire.obj(start, "capabilities")))
    }

    fun sync(receipt: Receipt? = null): Synced {
        var restarts = 0
        while (true) {
            try {
                if (receipt != null && current?.serverId != receipt.serverId) current = null
                val start = current ?: snapshot()
                if (receipt != null && start.serverId != receipt.serverId) throw ApiFailure(message = "同步空间已变化，请核对操作结果。")
                val tickets = LinkedHashMap(start.tickets.associateBy { it.id })
                val versions = HashMap(start.tickets.associate { it.id to it.version })
                var cursor = start.cursor
                var capabilities = start.capabilities
                var target = receipt?.committedCursor ?: cursor
                var passes = 0
                var more: Boolean
                do {
                    if (++passes > 256) throw ApiFailure(message = "同步积压较多，请重新检查。")
                    val page = read("/api/sync/changes?limit=100&serverId=${URLEncoder.encode(start.serverId, "UTF-8")}&after=$cursor")
                    identity(page, start.serverId)
                    val changes = Wire.array(page, "changes")
                    val head = Wire.number(page, "headCursor")
                    val next = Wire.number(page, "nextCursor")
                    more = Wire.bool(page, "hasMore")
                    if (Wire.number(page, "fromCursor") != cursor || changes.length() > 200 || next != cursor + changes.length()
                        || head < next || more != (next < head) || more && changes.length() == 0) gap()
                    if (passes == 1) target = maxOf(target, head)
                    for (i in 0 until changes.length()) {
                        val change = changes.opt(i) as? JSONObject ?: Wire.invalid()
                        if (Wire.number(change, "cursor") != cursor + 1) gap()
                        val id: String
                        val version: Long
                        when (Wire.text(change, "type", 20)) {
                            "upsert" -> {
                                val ticket = Wire.ticket(Wire.obj(change, "ticket"))
                                id = ticket.id; version = ticket.version
                                tickets[id] = ticket
                            }
                            "delete" -> {
                                id = Wire.id(change, "id"); version = Wire.number(change, "version", 1)
                                Wire.number(change, "deletedAt"); tickets.remove(id)
                            }
                            else -> Wire.invalid()
                        }
                        if (version < (versions[id] ?: 0)) gap()
                        versions[id] = version
                        cursor++
                    }
                    capabilities = Wire.capabilities(Wire.obj(page, "capabilities"))
                    if (cursor < target && !more) throw ApiFailure(message = "尚未追上已确认操作，请再次核对。")
                } while (more && cursor < target)
                return Synced(start.serverId, cursor, tickets.values.toList(), capabilities).also { current = it }
            } catch (failure: ApiFailure) {
                if (failure.status == 410 && ++restarts <= 3) { current = null; continue }
                throw failure
            }
        }
    }

    private fun gap(code: String = "cursor_gap"): Nothing = throw ApiFailure(410, code, "同步记录不连续，正在重新校准。")
}
