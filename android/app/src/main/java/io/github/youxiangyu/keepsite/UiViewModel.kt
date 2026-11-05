package io.github.youxiangyu.keepsite

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import io.github.youxiangyu.keepsite.core.AppController
import io.github.youxiangyu.keepsite.core.AppRepository

class UiViewModel(application: Application) : AndroidViewModel(application) {
    val controller: AppController = AppRepository(application)
    var route = "library"
    var detailId: String? = null
    var detailFrom = "library"
    var month: String? = null
    var mineScroll = 0
    var authMode = "login"
    var authEmail = ""
    var awaitingDraft = false
    var editorWasOpen = false
    var afterDraftRoute = "library"
    var libraryAccount: String? = null
    var libraryQuery = ""
    var libraryOldestFirst = false
    override fun onCleared() { controller.close() }
}
