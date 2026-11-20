package io.github.youxiangyu.keepsite.core

class AuthSubmission(val email: String, val next: String)

class AuthForm {
    var mode = "login"
        private set
    var email = ""
    private var pending: AuthSubmission? = null

    fun open(mode: String) {
        this.mode = mode
        pending = null
    }

    fun begin(email: String, next: String) = AuthSubmission(email, next).also { pending = it }

    fun update(state: AppState) {
        val request = pending ?: return
        if (state.session?.authenticated == true) {
            pending = null
            return
        }
        if (state.busy.isNotEmpty()) return
        pending = null
        if (state.completedAuth === request) {
            email = request.email
            mode = request.next
        }
    }
}
