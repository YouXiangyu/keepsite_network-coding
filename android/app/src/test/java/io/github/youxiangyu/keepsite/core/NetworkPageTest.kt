package io.github.youxiangyu.keepsite.core

import io.github.youxiangyu.keepsite.ui.NetworkPresentation
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Request
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.util.concurrent.CancellationException

class NetworkPageTest {
    private class ControlledExecutor(var reject: Boolean = false) : java.util.concurrent.Executor {
        val pending = ArrayDeque<Runnable>()
        override fun execute(command: Runnable) {
            if (reject) throw java.util.concurrent.RejectedExecutionException()
            pending.addLast(command)
        }
        fun drain() { while (pending.isNotEmpty()) pending.removeFirst().run() }
    }
    private class Destination {
        var opens = 0; var deletes = 0; var closed = false
        val results = mutableListOf<Boolean>()
        fun job(work: ControlledExecutor, cleanup: ControlledExecutor, current: () -> Boolean = { true },
            write: (java.io.OutputStream, () -> Boolean) -> Boolean = { output, cancelled -> output.write(1); !cancelled() }) =
            ReportFileJob(work, cleanup, current, {
                opens++
                object : ByteArrayOutputStream() { override fun close() { closed = true; super.close() } }
            }, { deletes++ }, write, { results += it })
    }
    @Test fun invalidTicketDeletesOnlyItsNewDestinationWithoutOpeningOnTheCaller() {
        val work = ControlledExecutor(); val cleanup = ControlledExecutor(); val destination = Destination()
        val job = destination.job(work, cleanup, { false }); job.start(); job.close(); job.close()
        assertEquals(0, destination.opens); assertEquals(0, destination.deletes)
        cleanup.drain(); work.drain(); assertEquals(1, destination.deletes); assertEquals(listOf(false), destination.results)
    }
    @Test fun rejectedSubmissionDeletesOnlyItsNewDestinationExactlyOnceWithoutOpening() {
        val work = ControlledExecutor(true); val cleanup = ControlledExecutor(); val destination = Destination()
        val job = destination.job(work, cleanup); job.start(); job.close()
        assertEquals(0, destination.opens); assertEquals(0, destination.deletes)
        cleanup.drain(); assertEquals(1, destination.deletes); assertEquals(listOf(false), destination.results)
    }
    @Test fun queuedCancellationAndOwnerCloseDeleteOnceEvenIfTheQueuedWorkNeverRuns() {
        val work = ControlledExecutor(); val cleanup = ControlledExecutor(); val destination = Destination()
        val job = destination.job(work, cleanup); job.start(); assertEquals(1, work.pending.size)
        job.close(); job.close(); assertEquals(0, destination.opens); assertEquals(0, destination.deletes)
        cleanup.drain(); assertEquals(1, destination.deletes)
        work.drain(); cleanup.drain(); assertEquals(0, destination.opens); assertEquals(1, destination.deletes)
        assertEquals(listOf(false), destination.results)
    }
    @Test fun successfulFileSurvivesLaterCancelWithoutAnyDelete() {
        val work = ControlledExecutor(); val cleanup = ControlledExecutor(); val destination = Destination()
        val job = destination.job(work, cleanup); job.start(); work.drain(); job.close(); cleanup.drain()
        assertEquals(1, destination.opens); assertTrue(destination.closed); assertEquals(0, destination.deletes)
        assertEquals(listOf(true), destination.results)
    }
    @Test fun invalidationDuringWriteClosesBeforeDeletingOnce() {
        val work = ControlledExecutor(); val cleanup = ControlledExecutor(); val destination = Destination(); var current = true
        val job = destination.job(work, cleanup, { current }) { output, cancelled ->
            output.write(1); current = false; assertTrue(cancelled()); false
        }
        job.start(); work.drain(); assertTrue(destination.closed); assertEquals(0, destination.deletes)
        job.close(); cleanup.drain(); assertEquals(1, destination.deletes); assertEquals(listOf(false), destination.results)
    }
    @Test fun writeFailureAndConcurrentCancelHaveOneFailedCompletionAndDelete() {
        val work = ControlledExecutor(); val cleanup = ControlledExecutor(); val destination = Destination()
        lateinit var job: ReportFileJob
        job = destination.job(work, cleanup) { _, cancelled ->
            job.close(); assertTrue(cancelled()); throw java.io.IOException("fixture failure")
        }
        job.start(); work.drain(); job.close(); cleanup.drain()
        assertTrue(destination.closed); assertEquals(1, destination.opens); assertEquals(1, destination.deletes)
        assertEquals(listOf(false), destination.results)
    }
    private val environment = NetworkReportEnvironment("https://keepsite.space", "test-1", 35)
    private fun ready(): Pair<NetworkDiagnostics, NetworkPageOwner> {
        val d = NetworkDiagnostics(); d.openAccount(3)
        return d to d.openPage()!!
    }
    @Test fun authenticatedInitialSyncFailureStillHasPageOwnerAndLightFacts() {
        assertTrue(NetworkPresentation.canOpen(true, false)); assertFalse(NetworkPresentation.canOpen(false, true))
        val d = NetworkDiagnostics(); assertNull(d.openPage()); d.openAccount(3)
        val page = d.openPage()!!; assertTrue(page.current()); assertNotNull(d.pageSnapshot(page))
        assertFalse(d.pageSnapshot(page)!!.observations.detailed)
    }
    @Test fun stalePageButtonsExportAndCleanupNeverEnterAReplacementAccount() {
        val (d, old) = ready(); d.pageDetailed(old, true)
        val oldReport = d.prepareReport(old, environment)!!
        d.openAccount(3); val current = d.openPage()!!
        val before = d.pageSnapshot(current)!!
        d.pageDetailed(old, true); d.pagePause(old, true); d.pageClear(old)
        assertNull(d.pageServerAction(old, 3, false)); assertNull(d.prepareReport(old, environment)); assertNull(d.pageSnapshot(old))
        old.close()
        assertEquals(before, d.pageSnapshot(current)); assertFalse(oldReport.current()); assertNull(oldReport.render(NetworkReportFormat.TEXT))
        d.pageDetailed(current, true); assertTrue(d.pageSnapshot(current)!!.observations.detailed)
        val beforeClear = d.prepareReport(current, environment)!!
        d.pageClear(current)
        assertTrue(current.current()); assertFalse(beforeClear.current())
        assertNotNull(d.prepareReport(current, environment)!!.render(NetworkReportFormat.JSON))
    }
    @Test fun currentHttpItemsSurviveOffClearAndPauseAndFinallyReleasesOnlyTheirOwner() {
        val (d, page) = ready(); val origin = "https://keepsite.space".toHttpUrl()
        val request = Request.Builder().url("https://keepsite.space/api/media/" + "a".repeat(64) + "?token=PRIVATE").build()
        val original = CancellationException("PRIVATE failure")
        fun nested(depth: Int) {
            d.http(3, origin, request, true) { _, observation ->
                observation?.response(200)
                if (depth > 1) nested(depth - 1) else {
                    val state = d.pageSnapshot(page)!!
                    assertEquals(25, state.api.active); assertEquals(16, state.httpActivities.size); assertEquals(9, state.httpOmitted)
                    val ids = state.httpActivities.map { it.id }
                    d.pageClear(page); d.pagePause(page, true)
                    assertEquals(ids, d.pageSnapshot(page)!!.httpActivities.map { it.id })
                    assertTrue(d.pageSnapshot(page)!!.observations.events.isEmpty())
                    assertFalse(state.httpActivities.toString().contains("PRIVATE")); assertFalse(state.httpActivities.toString().contains("a".repeat(64)))
                    throw original
                }
            }
        }
        assertSame(original, assertThrows(CancellationException::class.java) { nested(25) })
        assertEquals(0, d.pageSnapshot(page)!!.api.active); assertTrue(d.pageSnapshot(page)!!.httpActivities.isEmpty())
    }
    @Test fun websocketDurationUsesOneInstanceMonotonicLifetimeAndFreezesWhenClosed() {
        val (d, _) = ready()
        val owner = d.openAccount(3)!!; val socket = d.socket(owner, 3, 1)!!
        socket.opened(); val first = d.snapshot()!!.socket.durationMs!!
        Thread.sleep(20)
        assertTrue(d.snapshot()!!.socket.durationMs!! >= first)
        socket.closed(error = MonitorError.TIMEOUT)
        val ended = d.snapshot()!!.socket.durationMs
        Thread.sleep(10)
        assertEquals(ended, d.snapshot()!!.socket.durationMs); assertEquals(MonitorError.TIMEOUT, d.snapshot()!!.socket.error)
        d.socket(owner, 3, 2)!!; assertNull(d.snapshot()!!.socket.durationMs)
    }
    @Test fun allThreeReportFormatsAreWhitelistedAndAnInvalidatedWriteCannotFinish() {
        val (d, page) = ready(); d.pageDetailed(page, true)
        val request = Request.Builder().url("https://keepsite.space/api/media/" + "b".repeat(64) + "?signed=PRIVATE").build()
        d.http(3, "https://keepsite.space".toHttpUrl(), request, true) { _, observation -> observation!!.response(200); observation.read(0) }
        val report = d.prepareReport(page, environment)!!
        val json = report.render(NetworkReportFormat.JSON)!!
        assertFalse(json.contains("PRIVATE")); assertFalse(json.contains("b".repeat(64))); assertTrue(json.contains("routeTemplate"))
        assertEquals("since_last_clear", JSONObject(json).getJSONObject("scope").getString("localCounters"))
        val lines = report.render(NetworkReportFormat.JSONL)!!.trim().lines().map(::JSONObject)
        assertEquals("metadata", lines.first().getString("kind")); assertTrue(lines.drop(1).all { it.getString("kind") == "event" })
        assertTrue(report.render(NetworkReportFormat.TEXT)!!.contains("API最近可达"))
        val output = object : ByteArrayOutputStream() {
            override fun write(bytes: ByteArray, start: Int, count: Int) { super.write(bytes, start, count); d.pageClear(page) }
        }
        assertFalse(report.write(NetworkReportFormat.JSON, output) { false })
        assertFalse(report.current()); assertNull(report.render(NetworkReportFormat.JSON))
        val fresh = d.prepareReport(page, environment)!!
        assertTrue(fresh.write(NetworkReportFormat.JSON, ByteArrayOutputStream()) { false })
        assertFalse(fresh.write(NetworkReportFormat.JSON, ByteArrayOutputStream()) { true })
    }
    @Test fun clearingStateBlocksReadAndRepeatedDeleteUntilItsOwnCompletion() {
        val (d, page) = ready(); d.pageDetailed(page, true)
        val clearing = d.pageServerAction(page, 3, true)!!
        assertTrue(d.pageSnapshot(page)!!.serverRead.clearing)
        assertNull(d.pageServerAction(page, 3, false)); assertNull(d.pageServerAction(page, 3, true))
        clearing.finished(MonitorError.NONE, 200)
        assertFalse(d.pageSnapshot(page)!!.serverRead.clearing)
        val newer = d.pageServerAction(page, 3, true)!!
        clearing.finished(MonitorError.NETWORK)
        assertTrue(newer.current()); assertTrue(d.pageSnapshot(page)!!.serverRead.clearing)
        d.openAccount(3)
        newer.finished(MonitorError.NONE)
        assertEquals(ServerReadState(), d.snapshot()!!.serverRead)
    }
}
