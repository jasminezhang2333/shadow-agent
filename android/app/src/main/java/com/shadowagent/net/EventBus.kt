package com.shadowagent.net

import android.os.Handler
import android.os.Looper

/**
 * 进程内事件总线：CaptureService（后台）收到后端推来的事件后，
 * 经这里转发给 MainActivity（前台）的动态面板。回调统一切回主线程。
 *
 * 极简实现，只支持单个订阅者（当前只有一个前台 Activity）。
 */
object EventBus {
    private val main = Handler(Looper.getMainLooper())
    private var listener: ((String) -> Unit)? = null

    fun subscribe(l: (String) -> Unit) {
        listener = l
    }

    fun unsubscribe() {
        listener = null
    }

    fun post(json: String) {
        main.post { listener?.invoke(json) }
    }
}
