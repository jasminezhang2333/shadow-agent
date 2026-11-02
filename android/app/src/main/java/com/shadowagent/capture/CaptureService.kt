package com.shadowagent.capture

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.IBinder
import android.util.DisplayMetrics
import com.shadowagent.R
import com.shadowagent.audio.MicRecorder
import com.shadowagent.net.EventBus
import com.shadowagent.net.StreamUploader
import com.shadowagent.overlay.StatusOverlay
import org.json.JSONObject
import java.io.ByteArrayOutputStream

/**
 * 前台服务：MediaProjection 截用户物理屏（display 0）→ 记忆流；麦克风 → 语音流。
 * 两路都通过 StreamUploader 推到 PC 后端 /client/stream。
 *
 * 注意：这里截的是"用户正在看的前台"，用于生成记忆（被动流）。
 * agent 操作淘宝的虚拟屏帧由 PC 侧 adb screencap 取，和这里无关（见 DESIGN.md §2）。
 */
class CaptureService : Service() {

    private var projection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var imageReader: ImageReader? = null
    private lateinit var overlay: StatusOverlay
    private lateinit var uploader: StreamUploader
    private var mic: MicRecorder? = null

    private var lastFrameMs = 0L
    private val frameIntervalMs = 1000L // 记忆流 ~1fps，低频省带宽

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        android.util.Log.d("CaptureService", "onCreate called")
        active = this
        startAsForeground()
        overlay = StatusOverlay(this)
        // 把提问键放在悬浮球上：任何 App 之上点一下即可放行下一轮语音，不必切回前台
        overlay.show("分身已启动") {
            if (::uploader.isInitialized) {
                uploader.sendAsk()
                overlay.update("请说出你的问题…")
            }
        }
        val wsUrl = getSharedPreferences("cfg", Context.MODE_PRIVATE)
            .getString("backend_ws", "ws://10.0.2.2:8000/client/stream")!!
        android.util.Log.d("CaptureService", "backend_ws from prefs: $wsUrl")
        uploader = StreamUploader(
            url = wsUrl,
            onMessage = { json ->
                android.util.Log.d("CaptureService", "uploader event: $json")
                // 状态类事件同步到右上角小窗；所有事件都广播给前台动态面板
                overlayLine(json)?.let { overlay.update(it) }
                EventBus.post(json)
            }
        )
        android.util.Log.d("CaptureService", "calling uploader.connect()")
        uploader.connect()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        android.util.Log.d("CaptureService", "onStartCommand called")
        val resultCode = intent?.getIntExtra("code", 0) ?: 0
        val data = if (Build.VERSION.SDK_INT >= 33)
            intent?.getParcelableExtra("data", Intent::class.java)
        else @Suppress("DEPRECATION") intent?.getParcelableExtra("data")

        android.util.Log.d("CaptureService", "projection code=$resultCode, data=$data")
        if (resultCode != 0 && data != null) {
            val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE)
                as MediaProjectionManager
            projection = mpm.getMediaProjection(resultCode, data)

            // 必须先注册回调再启动 capture（Android 14+ 强制要求）
            projection?.registerCallback(object : MediaProjection.Callback() {
                override fun onStop() {
                    android.util.Log.d("CaptureService", "MediaProjection stopped by system")
                    stopSelf()
                }
            }, null)

            android.util.Log.d("CaptureService", "MediaProjection obtained, starting capture")
            startScreenCapture()
            startAudioCapture()
        } else {
            android.util.Log.d("CaptureService", "projection NOT granted, no capture started")
        }
        return START_STICKY
    }

    private fun startScreenCapture() {
        val metrics = DisplayMetrics()
        val wm = getSystemService(Context.WINDOW_SERVICE) as android.view.WindowManager
        @Suppress("DEPRECATION") wm.defaultDisplay.getRealMetrics(metrics)
        // 缩小到 ~360 宽，省带宽；记忆只需看清画面大意
        val scale = 360f / metrics.widthPixels
        val w = (metrics.widthPixels * scale).toInt()
        val h = (metrics.heightPixels * scale).toInt()

        imageReader = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2)
        virtualDisplay = projection?.createVirtualDisplay(
            "mem-capture", w, h, metrics.densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            imageReader?.surface, null, null
        )
        imageReader?.setOnImageAvailableListener({ reader ->
            val now = System.currentTimeMillis()
            val image = reader.acquireLatestImage() ?: return@setOnImageAvailableListener
            try {
                if (now - lastFrameMs >= frameIntervalMs) {
                    lastFrameMs = now
                    val jpeg = imageToJpeg(image, w, h)
                    uploader.sendVideo(jpeg)
                }
            } finally {
                image.close()
            }
        }, null)
    }

    private fun imageToJpeg(image: android.media.Image, w: Int, h: Int): ByteArray {
        val plane = image.planes[0]
        val rowPadding = plane.rowStride - plane.pixelStride * w
        val bmp = Bitmap.createBitmap(
            w + rowPadding / plane.pixelStride, h, Bitmap.Config.ARGB_8888)
        bmp.copyPixelsFromBuffer(plane.buffer)
        val cropped = Bitmap.createBitmap(bmp, 0, 0, w, h)
        val out = ByteArrayOutputStream()
        cropped.compress(Bitmap.CompressFormat.JPEG, 60, out)
        return out.toByteArray()
    }

    private fun startAudioCapture() {
        mic = MicRecorder { pcm -> uploader.sendAudio(pcm) }
        mic?.start()
    }

    /** 把后端事件压缩成右上角小窗的一行状态；理解/工具类事件不占小窗，交给前台面板。 */
    private fun overlayLine(json: String): String? {
        val obj = runCatching { JSONObject(json) }.getOrNull() ?: return null
        return when (obj.optString("type")) {
            "status" -> obj.optString("msg")
            "error" -> "错误: ${obj.optString("msg")}"
            "handoff" -> obj.optString("msg", "需要你确认")
            else -> null
        }
    }

    private fun startAsForeground() {
        val channelId = "shadow_capture"
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(
                NotificationChannel(channelId, "Shadow Agent",
                    NotificationManager.IMPORTANCE_LOW))
        }
        val notif: Notification = Notification.Builder(this, channelId)
            .setContentTitle("分身运行中")
            .setContentText("正在后台为你生成记忆")
            .setSmallIcon(R.drawable.ic_ghost_foreground)
            .build()
        // Android 10+ 必须显式声明前台服务类型，否则 MediaProjection 会被系统立即作废
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                1, notif,
                android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION or
                    android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            )
        } else {
            startForeground(1, notif)
        }
    }

    override fun onDestroy() {
        if (active === this) active = null
        mic?.stop()
        imageReader?.close()
        virtualDisplay?.release()
        projection?.stop()
        uploader.close()
        if (::overlay.isInitialized) overlay.hide()
        super.onDestroy()
    }

    companion object {
        // 当前运行中的服务实例（保活/调试用）
        @Volatile private var active: CaptureService? = null
    }
}
