package com.shadowagent

import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.app.ActivityCompat
import com.shadowagent.capture.CaptureService
import com.shadowagent.net.EventBus
import org.json.JSONArray
import org.json.JSONObject

/**
 * 唯一的 UI 入口：申请 MediaProjection + 悬浮窗 + 麦克风权限，拉起 CaptureService，
 * 并订阅 EventBus 把后端事件渲染到"实时动态"面板（理解问答 / 工具调用 / 工具结果）。
 *
 * 风格：浅色极简——白底 + 单一品牌强调色，圆角卡片，留白克制。
 */
class MainActivity : ComponentActivity() {

    private lateinit var feed: LinearLayout     // 动态卡片容器
    private lateinit var feedScroll: ScrollView
    private lateinit var emptyHint: TextView

    // 当前这一轮「画面理解」的流式气泡正文；为空表示还没开气泡
    private var streamBody: TextView? = null
    private val streamBuf = StringBuilder()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(buildUi())
        EventBus.subscribe { json -> onEvent(json) }
    }

    override fun onDestroy() {
        EventBus.unsubscribe()
        super.onDestroy()
    }

    // ---------- 权限 + 启动 ----------
    private val projectionLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode != RESULT_OK || result.data == null) {
            addBubble(AGENT, BUBBLE_AGENT, ERR, "未授权", "屏幕录制未授权，分身无法启动")
            return@registerForActivityResult
        }
        val svc = Intent(this, CaptureService::class.java).apply {
            putExtra("code", result.resultCode)
            putExtra("data", result.data)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(svc)
        else startService(svc)
    }

    private fun ensurePermissionsThenCapture() {
        if (!Settings.canDrawOverlays(this)) {
            startActivity(Intent(
                Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                Uri.parse("package:$packageName")))
            return
        }
        ActivityCompat.requestPermissions(
            this, arrayOf(android.Manifest.permission.RECORD_AUDIO), 1)
        val mpm = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        projectionLauncher.launch(mpm.createScreenCaptureIntent())
    }

    // ---------- 界面构建 ----------
    private fun buildUi(): View {
        val page = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(BG)
            setPadding(dp(20), dp(44), dp(20), dp(16))
        }

        // 品牌标题
        page.addView(TextView(this).apply {
            text = "Shadow"
            setTextColor(INK)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 30f)
            typeface = Typeface.create("sans-serif", Typeface.BOLD)
        })
        page.addView(TextView(this).apply {
            text = "你的数字分身，正在后台为你记忆与行动"
            setTextColor(INK_SUB)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
            setPadding(0, dp(4), 0, dp(20))
        })

        // 后端地址输入
        val backendInput = EditText(this).apply {
            hint = "后端地址，如 ws://192.168.1.10:8000/client/stream"
            setText(getSharedPreferences("cfg", Context.MODE_PRIVATE)
                .getString("backend_ws", "ws://10.0.2.2:8000/client/stream"))
            setTextColor(INK)
            setHintTextColor(INK_FAINT)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
            background = roundRect(CARD, dp(14))
            setPadding(dp(16), dp(14), dp(16), dp(14))
        }
        page.addView(backendInput, lp(matchW = true, bottom = dp(12)))

        // 启动按钮
        page.addView(Button(this).apply {
            text = "启动分身"
            isAllCaps = false
            setTextColor(Color.WHITE)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f)
            typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
            background = roundRect(BRAND, dp(14))
            stateListAnimator = null
            setOnClickListener {
                getSharedPreferences("cfg", Context.MODE_PRIVATE).edit()
                    .putString("backend_ws", backendInput.text.toString()).apply()
                ensurePermissionsThenCapture()
            }
        }, lp(matchW = true, height = dp(52), bottom = dp(24)))

        // 提问键已移到悬浮球上：在任何 App 之上点球上的「问」即可，不必切回这里

        // 状态看板标题
        page.addView(TextView(this).apply {
            text = "状态看板"
            setTextColor(INK)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f)
            typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
            setPadding(dp(2), 0, 0, dp(10))
        })

        // 滚动的卡片列表
        feed = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        emptyHint = TextView(this).apply {
            text = "等待分身的理解与工具调用…"
            setTextColor(INK_FAINT)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
            gravity = Gravity.CENTER
            setPadding(0, dp(40), 0, dp(40))
        }
        feed.addView(emptyHint)
        feedScroll = ScrollView(this).apply { addView(feed) }
        page.addView(feedScroll, lp(matchW = true, weight = 1f))

        return page
    }

    // ---------- 事件渲染 ----------
    private fun onEvent(json: String) {
        val o = runCatching { JSONObject(json) }.getOrNull() ?: return
        when (o.optString("type")) {
            "transcription" -> {
                sealStream()  // 用户新说一句话，先把上一轮理解封口
                addBubble(USER, BUBBLE_USER, Color.WHITE, "你", o.optString("text"))
            }
            "understanding" -> appendUnderstanding(o.optString("text"), o.optBoolean("final", false))
            "tool_call" -> {
                sealStream()
                val label = if (o.optString("name") == "comparison_prices") "比价工具" else "记忆检索"
                addBubble(AGENT, BUBBLE_TOOL, ACCENT, "调用 · $label",
                    o.optString("query").ifBlank { "（无参数）" })
            }
            "tool_result" -> {
                val name = o.optString("name")
                val label = if (name == "comparison_prices") "比价结果" else "记忆结果"
                if (name == "comparison_prices") {
                    addBubble(AGENT, BUBBLE_RESULT, OK, "返回 · $label", "")
                    renderPriceCards(o.optString("result"))
                } else {
                    addBubble(AGENT, BUBBLE_RESULT, OK, "返回 · $label", o.optString("result"))
                }
            }
            "status" -> addBubble(AGENT, BUBBLE_AGENT, INK_SUB, "状态", o.optString("msg"))
            "error" -> addBubble(AGENT, BUBBLE_AGENT, ERR, "错误", o.optString("msg"))
            "handoff" -> addBubble(AGENT, BUBBLE_AGENT, ERR, "需要你确认",
                o.optString("msg", "分身请求你介入"))
        }
    }

    /** 把同一轮的流式理解增量累加到同一个气泡；final 时封口。 */
    private fun appendUnderstanding(delta: String, final: Boolean) {
        if (delta.isEmpty() && !final) return
        if (streamBody == null) {
            streamBuf.setLength(0)
            streamBody = addBubble(AGENT, BUBBLE_AGENT, BRAND, "画面理解", "")
        }
        streamBuf.append(delta)
        streamBody?.text = streamBuf.toString()
        feedScroll.post { feedScroll.fullScroll(View.FOCUS_DOWN) }
        if (final) sealStream()
    }

    /** 结束当前流式气泡，下一条 understanding 会新开一个气泡。 */
    private fun sealStream() {
        streamBody = null
        streamBuf.setLength(0)
    }

    /**
     * 微信式消息气泡：user 靠右、agent 靠左；不同消息体用不同底色区分。
     * 返回正文 TextView，供流式更新复用同一个气泡。
     */
    private fun addBubble(side: Int, fill: Int, titleColor: Int,
                          title: String, body: String): TextView {
        if (emptyHint.parent != null) feed.removeView(emptyHint)
        val textColor = if (side == USER) Color.WHITE else INK
        val bubble = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = roundRect(fill, dp(14))
            setPadding(dp(14), dp(10), dp(14), dp(10))
        }
        bubble.addView(TextView(this).apply {
            text = title
            setTextColor(titleColor)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        })
        // 气泡最多约 80% 屏宽：给正文设 maxWidth，气泡 WRAP_CONTENT 就不会超过它
        val maxBubble = (resources.displayMetrics.widthPixels * 0.8f).toInt()
        val bodyView = TextView(this).apply {
            text = body
            setTextColor(textColor)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
            setPadding(0, dp(4), 0, 0)
            maxWidth = maxBubble
        }
        bubble.addView(bodyView)

        // 整行按 side 左右对齐：user 靠右、agent 靠左
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = if (side == USER) Gravity.END else Gravity.START
        }
        row.addView(bubble)
        feed.addView(row, lp(matchW = true, bottom = dp(10)))
        feedScroll.post { feedScroll.fullScroll(View.FOCUS_DOWN) }
        return bodyView
    }

    /** 把 JSON 比价结果数组渲染成可点击的商品卡片列表（每张卡片点击打开购买链接）。 */
    private fun renderPriceCards(result: String) {
        val arr = runCatching { JSONArray(result) }.getOrNull() ?: return
        for (i in 0 until arr.length()) {
            val item = arr.optJSONObject(i) ?: continue
            val platform = item.optString("platform")
            val title    = item.optString("title")
            val price    = item.optDouble("price", 0.0)
            val url      = item.optString("url")

            val card = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                background = roundRect(BUBBLE_RESULT, dp(14))
                setPadding(dp(14), dp(10), dp(14), dp(10))
            }

            // 平台名 + 价格横排
            val header = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            header.addView(TextView(this).apply {
                text = platform
                setTextColor(OK)
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
                typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
            }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            header.addView(TextView(this).apply {
                text = "¥${"%.0f".format(price)}"
                setTextColor(INK)
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
                typeface = Typeface.create("sans-serif", Typeface.BOLD)
            })
            card.addView(header)

            // 商品标题（最多两行）
            card.addView(TextView(this).apply {
                text = title
                setTextColor(INK_SUB)
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
                maxLines = 2
                setPadding(0, dp(4), 0, 0)
            })

            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.START
            }
            row.addView(card, lp(matchW = true))
            if (url.isNotBlank()) {
                row.setOnClickListener {
                    startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                }
            }
            feed.addView(row, lp(matchW = true, bottom = dp(8)))
            feedScroll.post { feedScroll.fullScroll(View.FOCUS_DOWN) }
        }
    }

    // ---------- 小工具 ----------
    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    private fun roundRect(fill: Int, radius: Int) = GradientDrawable().apply {
        setColor(fill)
        cornerRadius = radius.toFloat()
    }

    private fun lp(
        matchW: Boolean = false, height: Int = LinearLayout.LayoutParams.WRAP_CONTENT,
        bottom: Int = 0, weight: Float = 0f
    ) = LinearLayout.LayoutParams(
        if (matchW) LinearLayout.LayoutParams.MATCH_PARENT
        else LinearLayout.LayoutParams.WRAP_CONTENT, height, weight
    ).apply { bottomMargin = bottom }

    companion object {
        private const val BG = 0xFFF7F8FA.toInt()       // 页面底色，极浅灰白
        private const val CARD = 0xFFFFFFFF.toInt()      // 卡片白
        private const val INK = 0xFF1A1A1E.toInt()       // 主文字
        private const val INK_SUB = 0xFF6B7280.toInt()   // 次要文字
        private const val INK_FAINT = 0xFF9AA0AA.toInt() // 占位/提示
        private const val BRAND = 0xFF5B5BD6.toInt()     // 品牌紫
        private const val ACCENT = 0xFFE08600.toInt()    // 工具调用 琥珀
        private const val OK = 0xFF1E9E6A.toInt()        // 结果 绿
        private const val ERR = 0xFFDC4444.toInt()       // 错误 红

        // 气泡左右：user 靠右、agent 靠左
        private const val USER = 0
        private const val AGENT = 1

        // 不同消息体的气泡底色（微信式区分）
        private const val BUBBLE_USER = 0xFF5B5BD6.toInt()    // 你说的话：品牌紫，白字靠右
        private const val BUBBLE_AGENT = 0xFFFFFFFF.toInt()   // 画面理解 / 状态 / 错误：白卡
        private const val BUBBLE_TOOL = 0xFFFDF2E0.toInt()    // 工具调用：浅琥珀
        private const val BUBBLE_RESULT = 0xFFE7F6EE.toInt()  // 工具结果：浅绿
    }
}
