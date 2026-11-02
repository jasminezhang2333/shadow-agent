package com.shadowagent.net

import android.util.Base64
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.Response
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * 把音频/视频帧用 JSON over WebSocket 推到 PC 后端 /client/stream。
 * 后端再转推 realtime-MLLM。字段与后端 server.py client_stream 约定一致：
 *   {"audio": base64_pcm?, "video": base64_jpeg?}
 */
class StreamUploader(
    private val url: String,
    private val onMessage: (String) -> Unit
) {
    private val client = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .build()
    private var ws: WebSocket? = null

    fun connect() {
        android.util.Log.d("StreamUploader", "connect() called with url=$url")
        val req = Request.Builder().url(url).build()
        ws = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                android.util.Log.d("StreamUploader", "onOpen: WS connected")
                onMessage("""{"type":"status","msg":"已连接后端"}""")
            }
            override fun onMessage(webSocket: WebSocket, text: String) {
                android.util.Log.d("StreamUploader", "onMessage: $text")
                // 原样上抛，由 UI 层按 type 分发（understanding/tool_call/tool_result/status/error/handoff）
                onMessage(text)
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, r: Response?) {
                android.util.Log.e("StreamUploader", "onFailure: ${t.message}", t)
                val msg = (t.message ?: "未知").replace("\"", "'")
                onMessage("""{"type":"error","msg":"连接断开: $msg"}""")
            }
            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                android.util.Log.d("StreamUploader", "onClosing: code=$code, reason=$reason")
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                android.util.Log.d("StreamUploader", "onClosed: code=$code, reason=$reason")
            }
        })
        android.util.Log.d("StreamUploader", "newWebSocket returned, waiting for callbacks")
    }

    fun sendVideo(jpeg: ByteArray) {
        val msg = JSONObject().put(
            "video", Base64.encodeToString(jpeg, Base64.NO_WRAP))
        ws?.send(msg.toString())
    }

    fun sendAudio(pcm: ByteArray) {
        val msg = JSONObject().put(
            "audio", Base64.encodeToString(pcm, Base64.NO_WRAP))
        ws?.send(msg.toString())
    }

    /** 用户按「提问」键：发一条控制消息，后端放行下一轮语音。 */
    fun sendAsk() {
        ws?.send("""{"type":"ask"}""")
    }

    fun close() {
        ws?.close(1000, "bye")
        ws = null
    }
}
