package io.github.youxiangyu.memoverse.ui

import org.junit.Assert.*
import org.junit.Test

class PresentationTest {
    @Test fun nativeCallbackAcceptsOnlyExactAppPath() {
        assertTrue(Presentation.callback("io.github.youxiangyu.memoverse:/oauth/callback?ticket=t&state=s"))
        listOf("https://keepsite.space/oauth/callback", "io.github.youxiangyu.memoverse://evil/oauth/callback", "io.github.youxiangyu.memoverse:/other", "io.github.youxiangyu.memoverse:/oauth/callback#fake").forEach {
            assertFalse(it, Presentation.callback(it))
        }
    }
    @Test fun attributionNeverOpensScriptOrCleartextLinks() {
        assertTrue(Presentation.safeExternalUrl("https://commons.wikimedia.org/wiki/File:Example.jpg"))
        listOf("javascript:alert(1)", "http://example.com", "https://user:pass@example.com", "file:///private/image.jpg", "not a URL").forEach {
            assertFalse(it, Presentation.safeExternalUrl(it))
        }
    }
    @Test fun quotaAndUnknownStatesDoNotClaimPersistenceOrSuccess() {
        assertTrue(Presentation.media("temporary", "peer").contains("临时"))
        assertFalse(Presentation.media("temporary", "peer").contains("已缓存"))
        assertTrue(Presentation.media("cached", "cache").contains("本机缓存"))
        assertTrue(Presentation.processing("future_state").contains("future_state"))
    }
}
