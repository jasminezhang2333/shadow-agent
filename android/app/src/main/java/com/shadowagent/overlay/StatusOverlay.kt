package com.shadowagent.overlay

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.PixelFormat
import android.os.Build
import android.view.Gravity
import android.view.LayoutInflater
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.TextView
import com.shadowagent.R
import kotlin.math.abs

/**
 * 不抢焦点的状态小窗，现在还带一个「问」键——agent 唯一可见的痕迹（见 docs/DESIGN.md §4）。
 *
 * 为什么把提问键放这：悬浮窗浮在所有 App 之上，用户在别的 App 里（刷视频等）
 * 想提问时直接点球上的「问」即可，不必切回 ShadowAgent 前台。
 *
 * 关键 flag —— 保证"不打扰"的核心：
 *  - FLAG_NOT_FOCUSABLE   : 永不获得输入焦点，用户键盘/IME 不受影响（但窗口自身仍可接收触摸）
 *  - FLAG_NOT_TOUCH_MODAL : 窗口外的触摸正常穿透给用户前台 App，只有球自身区域吃触摸
 *  - TYPE_APPLICATION_OVERLAY : 悬浮在所有 App 之上但不切换前台 Activity
 */
class StatusOverlay(private val context: Context) {

    private val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
    private var view: View? = null
    private var textView: TextView? = null
    private var params: WindowManager.LayoutParams? = null

    private val layoutFlags =
        WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
        WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
        WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS

    private val windowType =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        else
            @Suppress("DEPRECATION")
            WindowManager.LayoutParams.TYPE_PHONE

    /**
     * @param onAsk 点「问」键的回调：放行下一轮语音提问。null 表示暂不可提问。
     */
    @SuppressLint("ClickableViewAccessibility")
    fun show(initial: String = "分身待命", onAsk: (() -> Unit)? = null) {
        if (view != null) return
        val v = LayoutInflater.from(context).inflate(R.layout.overlay_status, null)
        textView = v.findViewById(R.id.status_text)
        textView?.text = initial

        val lp = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            windowType,
            layoutFlags,
            PixelFormat.TRANSLUCENT
        ).apply {
            // 用 TOP|START 让 x/y 变成从左上角起算的绝对坐标，拖动时好算
            gravity = Gravity.TOP or Gravity.START
            x = 24
            y = 160
        }
        params = lp

        // 「问」键：按一下放行下一轮语音
        v.findViewById<TextView>(R.id.ask_btn).setOnClickListener {
            onAsk?.invoke()
        }

        // 状态文字充当拖动句柄：按住它拖整个球
        v.findViewById<TextView>(R.id.status_text)
            .setOnTouchListener(DragHandle(lp))

        wm.addView(v, lp)
        view = v
    }

    fun update(text: String) {
        textView?.post { textView?.text = text }
    }

    fun hide() {
        view?.let { wm.removeView(it) }
        view = null
        textView = null
        params = null
    }

    /** 拖动句柄：记录按下点，move 时把窗口挪到新位置。移动很小则当作点击，不吞掉。 */
    private inner class DragHandle(
        private val lp: WindowManager.LayoutParams
    ) : View.OnTouchListener {
        private var startX = 0
        private var startY = 0
        private var touchX = 0f
        private var touchY = 0f
        private var moved = false

        override fun onTouch(view: View, event: MotionEvent): Boolean {
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    startX = lp.x
                    startY = lp.y
                    touchX = event.rawX
                    touchY = event.rawY
                    moved = false
                    return true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = (event.rawX - touchX).toInt()
                    val dy = (event.rawY - touchY).toInt()
                    if (abs(dx) > TOUCH_SLOP || abs(dy) > TOUCH_SLOP) moved = true
                    lp.x = startX + dx
                    lp.y = startY + dy
                    this@StatusOverlay.view?.let { wm.updateViewLayout(it, lp) }
                    return true
                }
                MotionEvent.ACTION_UP -> {
                    // 几乎没动就当点击（句柄本身无点击动作，这里仅避免误吞）
                    if (!moved) view.performClick()
                    return true
                }
            }
            return false
        }
    }

    private companion object {
        const val TOUCH_SLOP = 12
    }
}
