package io.github.youxiangyu.keepsite

import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class TicketSearchTest {
    @Test fun supportsContinuousSubsequencePinyinAndInitialsForTitlesAndCinemas() {
        val cases = listOf(
            Triple("星际穿越", "星穿", true),
            Triple("星际穿越", "xingjichuanyue", true),
            Triple("星际穿越", "xjcy", true),
            Triple("万达影城", "万影", true),
            Triple("万达影城", "wandayingcheng", true),
            Triple("万达影城", "wdyc", true),
            Triple("InterStellar", "INTER", true),
            Triple("InterStellar", "stellar", true),
            Triple("星际穿越", "穿星", false),
            Triple("万达影城", "wdyx", false),
        )
        cases.forEach { (field, query, expected) ->
            assertEquals("$query against $field", expected, TicketSearch.matches(query, field))
        }
        assertEquals(true, TicketSearch.matches("万影", "星际穿越", "万达影城"))
        assertEquals(true, TicketSearch.matches("xjcy", "万达影城", "星际穿越"))
        assertEquals(true, TicketSearch.matches("  ", "任意片名"))
        assertEquals(true, TicketSearch.matches("xing-ji-chuan-yue", "星际穿越"))
    }
}
