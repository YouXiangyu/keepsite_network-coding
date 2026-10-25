package io.github.youxiangyu.keepsite.core

import org.json.JSONObject

/** Upload rejections never resolve a ticket mutation that may already have committed. */
internal fun <T> prepareDraftUpload(prepare: () -> T, rejected: () -> Unit): T = try { prepare() }
catch (failure: ApiFailure) {
    if (failure.status in setOf(409, 413, 415, 422)) rejected()
    throw failure
}

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
