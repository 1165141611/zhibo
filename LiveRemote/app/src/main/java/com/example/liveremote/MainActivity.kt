package com.example.liveremote

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.Button
import android.widget.EditText
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

class MainActivity : AppCompatActivity() {

    private lateinit var ipInput: EditText
    private var autoStarted = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        ipInput = findViewById(R.id.ipInput)
        val prefs = getSharedPreferences("cfg", MODE_PRIVATE)
        val savedIp = prefs.getString("ip", "") ?: ""
        ipInput.setText(savedIp)

        findViewById<Button>(R.id.startBtn).setOnClickListener {
            val ip = ipInput.text.toString().trim()
            if (ip.isEmpty()) {
                toast("请先填电脑IP:端口")
                return@setOnClickListener
            }
            prefs.edit().putString("ip", ip).apply()

            // 需要“显示在其他应用上层”权限
            if (!Settings.canDrawOverlays(this)) {
                toast("请授予“显示在其他应用上层”权限,然后返回再点启动")
                startActivity(
                    Intent(
                        Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        Uri.parse("package:$packageName")
                    )
                )
                return@setOnClickListener
            }

            enterOverlayWithGate(normalizeUrl(ip))
        }

        findViewById<Button>(R.id.stopBtn).setOnClickListener {
            stopService(Intent(this, OverlayService::class.java))
            toast("已停止")
        }

        // 启动时:若已保存IP且已授权,则后台探测后端能否连通,
        // 能连通就直接进入悬浮状态,省去手动点击“启动”。
        if (savedIp.isNotEmpty() && Settings.canDrawOverlays(this)) {
            tryAutoStart(normalizeUrl(savedIp))
        }
    }

    private fun normalizeUrl(ip: String): String =
        if (ip.startsWith("http")) ip else "http://$ip"

    private fun tryAutoStart(url: String) {
        if (autoStarted) return
        Thread {
            val ok = ping(url)
            runOnUiThread {
                if (ok && !autoStarted && !isFinishing) {
                    autoStarted = true
                    enterOverlayWithGate(url)
                }
            }
        }.start()
    }

    /**
     * 进入悬浮态前的闸门:后台问 PC(/scrcpy/check)现在能否经无线 adb 连上手机投屏。
     * 能连 / 查不到(旧服务或网络问题)→ 直接进入;明确连不上 → 弹窗排查(可仍然进入)。
     */
    private fun enterOverlayWithGate(url: String) {
        Thread {
            var notReady = false
            var msg = ""
            try {
                val c = URL("$url/scrcpy/check").openConnection() as HttpURLConnection
                c.connectTimeout = 4000
                c.readTimeout = 8000   // PC 端要跑 adb connect,给足时间
                c.requestMethod = "GET"
                c.useCaches = false
                if (c.responseCode == 200) {
                    val body = c.inputStream.bufferedReader().use { it.readText() }
                    val j = JSONObject(body)
                    notReady = !j.optBoolean("reachable", false)
                    msg = j.optString("msg", "")
                }
                c.disconnect()
            } catch (_: Exception) {
                notReady = false   // 查不到就不拦,放行进入
            }
            runOnUiThread {
                if (isFinishing) return@runOnUiThread
                if (notReady) showScrcpyGateDialog(url, msg) else enterOverlay(url)
            }
        }.start()
    }

    private fun enterOverlay(url: String) {
        startOverlay(url)
        toast("悬浮控制台已启动")
        moveTaskToBack(true)   // 退到后台,让悬浮窗浮在K歌上层
    }

    private fun showScrcpyGateDialog(url: String, msg: String) {
        AlertDialog.Builder(this)
            .setTitle("电脑暂时连不上手机投屏")
            .setMessage(
                (if (msg.isNotEmpty()) "$msg\n\n" else "") +
                    "排查:\n" +
                    "· 手机刚重启过?用数据线连电脑执行一次 adb tcpip 5555\n" +
                    "· 手机与电脑是否在同一 WiFi\n\n" +
                    "也可先仍然进入,投屏状态见悬浮窗左上角圆点(可点它重试)。"
            )
            .setPositiveButton("仍然进入") { _, _ -> enterOverlay(url) }
            .setNegativeButton("取消", null)
            .show()
    }

    /** 短超时探测后端是否可达。 */
    private fun ping(url: String): Boolean = try {
        val c = URL(url).openConnection() as HttpURLConnection
        c.connectTimeout = 1200
        c.readTimeout = 1200
        c.requestMethod = "GET"
        c.useCaches = false
        val code = c.responseCode
        c.disconnect()
        code in 200..499   // 有响应即视为可达
    } catch (_: Exception) {
        false
    }

    private fun startOverlay(url: String) {
        val svc = Intent(this, OverlayService::class.java).putExtra("url", url)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(svc)
        } else {
            startService(svc)
        }
    }

    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_SHORT).show()
}
