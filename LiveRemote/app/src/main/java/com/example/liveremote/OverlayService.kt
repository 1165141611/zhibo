package com.example.liveremote

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.IBinder
import android.util.TypedValue
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.ViewGroup
import android.view.WindowManager
import android.webkit.JavascriptInterface
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.app.NotificationCompat

class OverlayService : Service() {

    private lateinit var wm: WindowManager
    private var root: FrameLayout? = null
    private var ball: View? = null
    private var panel: View? = null
    private var webView: WebView? = null
    private lateinit var params: WindowManager.LayoutParams
    private var isBall = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForegroundInternal()
        val url = intent?.getStringExtra("url") ?: "http://127.0.0.1:8765"
        if (root == null) {
            buildOverlay(url)
        } else {
            // 已在悬浮:重新加载最新页面(点"启动"即可刷新网页,免去停止再开)
            webView?.loadUrl(url)
            showBall(false)
        }
        return START_STICKY
    }

    private fun dp(v: Int): Int = TypedValue.applyDimension(
        TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), resources.displayMetrics
    ).toInt()

    private fun buildOverlay(url: String) {
        wm = getSystemService(WINDOW_SERVICE) as WindowManager

        // ── 展开面板 ──────────────────────────────────────────────
        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.parseColor("#14161a"))
        }

        // 顶部拖动条
        val header = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundColor(Color.parseColor("#2a2f37"))
            setPadding(dp(10), dp(5), dp(4), dp(5))
        }
        val title = TextView(this).apply {
            text = "遥控 ⠿ 拖动"
            setTextColor(Color.parseColor("#cdd3dc"))
            textSize = 12f
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        val btnCollapse = headerBtn("—")   // 收起为悬浮球
        header.addView(title)
        header.addView(btnCollapse)

        // 宽横条:宽度接近满屏,高度由网页内容通过 JS 桥回报,窗口不滚动
        val stripWidth = resources.displayMetrics.widthPixels - dp(12)
        webView = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.cacheMode = WebSettings.LOAD_NO_CACHE   // 每次都拉最新网页,避免看到旧页面
            isVerticalScrollBarEnabled = false
            overScrollMode = View.OVER_SCROLL_NEVER
            webViewClient = WebViewClient()
            layoutParams = LinearLayout.LayoutParams(stripWidth, dp(150))
            // 网页把自身内容高度(CSS px)回报回来,据此精确设定 WebView 高度 → 不出现内部滚动
            addJavascriptInterface(object {
                @JavascriptInterface
                fun reportHeight(cssPx: Float) {
                    val h = (cssPx * resources.displayMetrics.density).toInt()
                    webView?.post {
                        val lp = webView?.layoutParams ?: return@post
                        if (h in 1..2000 && lp.height != h) {
                            lp.height = h
                            webView?.layoutParams = lp
                            root?.let { runCatching { wm.updateViewLayout(it, params) } }
                        }
                    }
                }
            }, "Android")
            loadUrl(url)
        }

        container.addView(header)
        container.addView(webView)
        panel = container

        // ── 悬浮球(收起态)────────────────────────────────────────
        val ballView = TextView(this).apply {
            text = "遥控"
            setTextColor(Color.WHITE)
            textSize = 13f
            gravity = Gravity.CENTER
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(Color.parseColor("#2f6df0"))
                setStroke(dp(2), Color.parseColor("#66ffffff"))
            }
            layoutParams = FrameLayout.LayoutParams(dp(56), dp(56))
            alpha = 0.95f
            visibility = View.GONE
        }
        ball = ballView

        // ── 根容器:同时容纳面板与球,按可见性自适应窗口大小 ──────
        val frame = FrameLayout(this).apply {
            addView(container)
            addView(ballView)
        }
        root = frame

        // 手指触到窗口以外(失焦)→ 展开的面板自动收起为悬浮球
        frame.setOnTouchListener { _, e ->
            if (e.action == MotionEvent.ACTION_OUTSIDE && !isBall) {
                showBall(true)
            }
            false   // 不消费,底层 K歌 界面照常收到该触摸
        }

        params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or
                WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH,  // 收到窗口外触摸事件
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = dp(6)
            y = dp(80)
        }

        // 面板:按住顶部灰条拖动
        header.setOnTouchListener(dragListener(null))
        // 悬浮球:可拖动;未拖动的轻点 = 展开
        ballView.setOnTouchListener(dragListener { showBall(false) })

        btnCollapse.setOnClickListener { showBall(true) }

        wm.addView(root, params)
    }

    /**
     * 通用拖动监听:更新窗口位置。
     * 若 [onTap] 非空,则位移小于触摸阈值的抬起视为轻点。
     */
    private fun dragListener(onTap: (() -> Unit)?) = object : View.OnTouchListener {
        private val slop = ViewConfiguration.get(this@OverlayService).scaledTouchSlop
        private var startX = 0
        private var startY = 0
        private var downRawX = 0f
        private var downRawY = 0f
        private var moved = false

        override fun onTouch(v: View, e: MotionEvent): Boolean {
            when (e.action) {
                MotionEvent.ACTION_DOWN -> {
                    startX = params.x; startY = params.y
                    downRawX = e.rawX; downRawY = e.rawY
                    moved = false
                    return true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = (e.rawX - downRawX).toInt()
                    val dy = (e.rawY - downRawY).toInt()
                    if (!moved && (kotlin.math.abs(dx) > slop || kotlin.math.abs(dy) > slop)) {
                        moved = true
                    }
                    params.x = startX + dx
                    params.y = startY + dy
                    root?.let { wm.updateViewLayout(it, params) }
                    return true
                }
                MotionEvent.ACTION_UP -> {
                    if (!moved) onTap?.invoke()
                    return true
                }
            }
            return false
        }
    }

    private fun showBall(collapse: Boolean) {
        isBall = collapse
        ball?.visibility = if (collapse) View.VISIBLE else View.GONE
        panel?.visibility = if (collapse) View.GONE else View.VISIBLE
        root?.let { wm.updateViewLayout(it, params) }
    }

    private fun headerBtn(t: String): TextView = TextView(this).apply {
        text = t
        setTextColor(Color.WHITE)
        textSize = 17f
        gravity = Gravity.CENTER
        width = dp(34)
        height = dp(28)
    }

    private fun startForegroundInternal() {
        val chId = "live_remote_overlay"
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel(
                chId, "悬浮控制台", NotificationManager.IMPORTANCE_MIN
            )
            (getSystemService(NOTIFICATION_SERVICE) as NotificationManager)
                .createNotificationChannel(ch)
        }
        val n = NotificationCompat.Builder(this, chId)
            .setContentTitle("直播遥控悬浮台运行中")
            .setSmallIcon(android.R.drawable.ic_menu_compass)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            // Android 14+ 前台服务需指定类型
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(1, n)
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        root?.let { r -> runCatching { wm.removeView(r) } }
        root = null
        ball = null
        panel = null
        webView?.destroy()
        webView = null
    }
}
