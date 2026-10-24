package io.github.youxiangyu.memoverse.core

import org.json.JSONObject

/** Only a rejected mutation can release a pending draft; receipt/sync failures remain uncertain. */
internal fun confirmDraftSubmission(
    operationId: String,
    write: () -> JSONObject,
    rejected: () -> Unit,
    confirm: (Receipt) -> Unit,
) {
    val response = try { write() }
    catch (failure: ApiFailure) {
        if (failure.status == 404 || failure.status == 422) rejected()
        throw failure
    }
    confirm(Wire.receipt(response, operationId))
}
