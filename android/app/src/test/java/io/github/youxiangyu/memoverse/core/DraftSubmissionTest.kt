package io.github.youxiangyu.memoverse.core

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class DraftSubmissionTest {
    private val operation = "draft-operation"
    private fun receipt() = JSONObject().put("serverId", "server").put("operationId", operation).put("committedCursor", 7)

    @Test fun deletedTicketAndValidationRejectionReleasePendingWithoutConfirmingSave() {
        for (status in listOf(404, 422)) {
            var pending = true
            var confirmations = 0
            val rejection = ApiFailure(status, message = "Rejected before committing")
            val failure = assertThrows(ApiFailure::class.java) {
                confirmDraftSubmission(operation, write = { throw rejection }, rejected = { pending = false },
                    confirm = { confirmations++ })
            }
            assertSame(rejection, failure)
            assertFalse("An explicit mutation rejection must release the draft for editing/discard", pending)
            assertEquals(0, confirmations)
        }
    }

    @Test fun uncertainWriteAndServerFailureKeepPendingForIdempotentRecovery() {
        for (status in listOf(0, 409, 500, 503)) {
            var pending = true
            val uncertain = ApiFailure(status, message = "Outcome uncertain")
            val failure = assertThrows(ApiFailure::class.java) {
                confirmDraftSubmission(operation, write = { throw uncertain }, rejected = { pending = false },
                    confirm = { fail("An unsuccessful request cannot confirm a save") })
            }
            assertSame(uncertain, failure)
            assertTrue("Uncertain outcomes must retain the draft's operation and discard protection", pending)
        }
    }

    @Test fun successfulWriteFollowedBySync404Or422KeepsPending() {
        for (status in listOf(404, 422)) {
            var pending = true
            val syncFailure = ApiFailure(status, message = "Snapshot unavailable after commit")
            val failure = assertThrows(ApiFailure::class.java) {
                confirmDraftSubmission(operation, write = ::receipt, rejected = { pending = false }, confirm = {
                    assertEquals(Receipt("server", operation, 7), it)
                    throw syncFailure
                })
            }
            assertSame(syncFailure, failure)
            assertTrue("A committed write still needs its synchronization barrier", pending)
        }
    }

    @Test fun malformedOrMismatchedReceiptKeepsPendingWithoutConfirming() {
        for (response in listOf(JSONObject(), receipt().put("operationId", "different-operation"))) {
            var pending = true
            assertThrows(ApiFailure::class.java) {
                confirmDraftSubmission(operation, write = { response }, rejected = { pending = false },
                    confirm = { fail("An invalid receipt cannot confirm a save") })
            }
            assertTrue(pending)
        }
    }

    @Test fun validWriteConfirmsTheOriginalOperationAtItsCommitBarrier() {
        val events = mutableListOf<String>()
        confirmDraftSubmission(operation, write = { events.add("write"); receipt() },
            rejected = { fail("A successful write must not be rejected") }, confirm = {
                assertEquals(Receipt("server", operation, 7), it)
                events.add("confirmed")
            })
        assertEquals(listOf("write", "confirmed"), events)
    }
}
