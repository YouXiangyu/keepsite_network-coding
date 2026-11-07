package io.github.youxiangyu.keepsite.core

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.lang.management.ManagementFactory

/** Optional evidence output; all clocks and collector IDs are the real production implementations. */
class NetworkMeasurementTest {
    @Test fun sameWorkloadAlternatesRealCollectorModesAndMeasuresReportFormatting() {
        val batches = JSONArray(); val operations = JSONArray()
        val progress = MonitorValues(bytes = 32); val completed = MonitorValues(bytes = 64, verified = true)
        val cpu = ManagementFactory.getThreadMXBean()
        fun cpuNow(): Long? = if (cpu.isCurrentThreadCpuTimeSupported && cpu.isThreadCpuTimeEnabled) cpu.currentThreadCpuTime.takeIf { it >= 0 } else null
        fun batch(detail: Boolean, pair: Int, measured: Boolean) {
            val monitor = NetworkMonitor(); val owner = monitor.openAccount(); monitor.setDetailed(owner, detail)
            val lane = monitor.connect(owner, MonitorLane.HTTP)!!
            val times = LongArray(500); val runtime = Runtime.getRuntime()
            val heapBefore = runtime.totalMemory() - runtime.freeMemory(); val cpuBefore = cpuNow(); val start = System.nanoTime()
            repeat(500) { i ->
                val t = System.nanoTime()
                val operation = monitor.begin(lane, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorTemplate.MEDIA)!!
                assertTrue(monitor.observe(operation, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorStatus.TRANSFERRING, progress))
                assertTrue(monitor.observe(operation, MonitorProtocol.HTTP, MonitorEvent.REQUEST, MonitorStatus.COMPLETED, completed))
                times[i] = System.nanoTime() - t
            }
            val end = System.nanoTime(); val cpuAfter = cpuNow(); val heapAfter = runtime.totalMemory() - runtime.freeMemory()
            val snapshot = monitor.snapshot()
            assertEquals(0, snapshot.active); assertTrue(snapshot.events.size <= 200); assertTrue(snapshot.historyBytes <= 128 * 1024)
            assertEquals(if (detail) 1500L else 0L, snapshot.recorded)
            if (detail) assertTrue(snapshot.dropped >= 1300)
            if (measured) {
                batches.put(JSONObject().put("pair", pair).put("mode", if (detail) "on" else "off").put("operations", 500)
                    .put("startNs", start).put("endNs", end).put("elapsedNs", end - start).put("cpuNs", if (cpuBefore != null && cpuAfter != null) cpuAfter - cpuBefore else JSONObject.NULL)
                    .put("heapUsedBefore", heapBefore).put("heapUsedAfter", heapAfter).put("retained", snapshot.events.size).put("historyBytes", snapshot.historyBytes)
                    .put("recorded", snapshot.recorded).put("dropped", snapshot.dropped).put("captureCostNs", snapshot.captureCostNs).put("active", snapshot.active))
                times.forEachIndexed { index, ns -> operations.put(JSONObject().put("pair", pair).put("mode", if (detail) "on" else "off").put("operation", index).put("elapsedNs", ns)) }
            }
        }
        batch(false, -1, false); batch(true, -1, false)
        repeat(5) { pair -> listOf(pair % 2 == 1, pair % 2 == 0).forEach { batch(it, pair, true) } }
        val d = NetworkDiagnostics(); val account = d.openAccount(9)!!; d.setDetailed(true)
        repeat(50) { val media = d.media(account, 9, 64)!!; val attempt = media.attempt(MonitorRoute.CACHE, MonitorProtocol.CACHE)!!
            attempt.read(64); attempt.complete(true); media.validating { Unit }; media.complete(64, MonitorRoute.CACHE) }
        val page = d.openPage()!!; val formats = JSONArray()
        for (format in NetworkReportFormat.entries) repeat(5) { sample ->
            val report = d.prepareReport(page, NetworkReportEnvironment("https://keepsite.space", "measurement", 35))!!
            val t = System.nanoTime(); val text = report.render(format)!!; val elapsed = System.nanoTime() - t
            formats.put(JSONObject().put("format", format.name).put("sample", sample).put("elapsedNs", elapsed).put("utf8Bytes", text.toByteArray(Charsets.UTF_8).size))
            assertFalse(text.contains("private")); assertTrue(text.isNotEmpty())
        }
        val evidence = JSONObject().put("scope", "JVM production collector light-state off versus detailed history on; includes real ID generation and identical assertion/timing overhead")
            .put("clock", "System.nanoTime").put("warmupBatchesPerMode", 1).put("operationsPerBatch", 500).put("batchesPerMode", 5)
            .put("runtime", System.getProperty("java.runtime.version")).put("vm", System.getProperty("java.vm.name")).put("os", System.getProperty("os.name"))
            .put("cpuScope", "current JUnit worker thread only when supported").put("heapScope", "Runtime used-heap boundary samples; not allocation, peak, retained memory, Android or total process")
            .put("batches", batches).put("operations", operations).put("formatScope", "sealed report whitelist build+format only, 200 bounded local events; excludes UI/provider/disk")
            .put("formatSamples", formats).put("socketHttp", "not measured by this runner").put("rtc", "not measured")
        System.getenv("KEEPSITE_N4_OUTPUT")?.let { root -> File(root, "jvm-measurements.json").writeText(evidence.toString(2) + "\n") }
    }
}
