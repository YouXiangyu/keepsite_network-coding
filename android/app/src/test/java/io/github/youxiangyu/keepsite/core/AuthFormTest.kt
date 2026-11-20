package io.github.youxiangyu.keepsite.core

import org.junit.Assert.assertEquals
import org.junit.Test

class AuthFormTest {
    @Test fun successfulRequestAdvancesOnceWithSubmittedEmailDespiteUnrelatedError() {
        for ((from, next) in listOf("register" to "verify", "forgot" to "reset")) {
            val form = AuthForm()
            form.open(from)
            val request = form.begin("a@example.test", next)
            form.email = "b@example.test"
            val success = AppState(completedAuth = request, error = "会话刷新失败")
            form.update(success.copy(busy = "处理中"))
            assertEquals(from, form.mode)
            form.update(success)
            assertEquals(next, form.mode)
            assertEquals("a@example.test", form.email)
            form.email = "edited@example.test"
            form.update(success)
            assertEquals("edited@example.test", form.email)
        }
    }

    @Test fun failureKeepsCurrentFormAndInput() {
        val form = AuthForm()
        form.open("register")
        form.email = "a@example.test"
        form.begin(form.email, "verify")
        form.update(AppState(error = "注册失败"))
        assertEquals("register", form.mode)
        assertEquals("a@example.test", form.email)
    }

    @Test fun leavingDiscardsLateSuccessEvenWhenReopeningSameForm() {
        val form = AuthForm()
        form.open("forgot")
        val previous = form.begin("a@example.test", "reset")
        form.open("login")
        form.open("forgot")
        form.update(AppState(completedAuth = previous))
        assertEquals("forgot", form.mode)
        val current = form.begin(previous.email, "reset")
        form.update(AppState(completedAuth = previous, busy = "处理中"))
        form.update(AppState(completedAuth = current))
        assertEquals("reset", form.mode)
    }
}
