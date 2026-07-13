package com.example.liveremote.net

import com.example.liveremote.model.LyricChar
import com.example.liveremote.model.LyricLine
import com.example.liveremote.model.Note
import com.example.liveremote.model.Song
import com.example.liveremote.model.SongLyrics
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit

/**
 * pc-service 客户端:OkHttp WebSocket 连 `ws://<host>/ws`,解析 `state` 消息;断线自动重连。
 * 命令通过 [send] 发 JSON。曲库走 HTTP `GET /library`。
 * 协议见 live-remote/README.md「K歌 API」。所有回调在 OkHttp 线程,交给 StateFlow 消费(线程安全)。
 */
class RemoteClient(
    private val onState: (JSONObject) -> Unit,
    private val onConn: (Boolean) -> Unit,
) {
    private val http = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.SECONDS)   // WS 长连
        .build()

    private val sched: ScheduledExecutorService = Executors.newSingleThreadScheduledExecutor { r ->
        Thread(r, "remote-reconnect").apply { isDaemon = true }
    }

    @Volatile private var host: String = ""          // 规范化后的 host:port
    @Volatile private var ws: WebSocket? = null
    @Volatile private var wantOpen = false
    @Volatile private var generation = 0             // 防旧连接的回调污染新连接

    /** host 可带 http:// 或 ws:// 前缀、可带端口;缺端口补 8765。 */
    fun connect(rawHost: String) {
        val h = normalize(rawHost)
        host = h
        wantOpen = true
        generation++
        openNow()
    }

    fun disconnect() {
        wantOpen = false
        generation++
        ws?.close(1000, null)
        ws = null
        onConn(false)
    }

    fun currentHost(): String = host

    private fun openNow() {
        if (!wantOpen || host.isEmpty()) return
        val gen = generation
        ws?.cancel()
        val req = Request.Builder().url("ws://$host/ws").build()
        ws = http.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (gen != generation) return
                onConn(true)
            }
            override fun onMessage(webSocket: WebSocket, text: String) {
                if (gen != generation) return
                try {
                    val j = JSONObject(text)
                    if (j.optString("type") == "state") onState(j)
                } catch (_: Exception) { }
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (gen != generation) return
                onConn(false); scheduleReconnect(gen)
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (gen != generation) return
                onConn(false); scheduleReconnect(gen)
            }
        })
    }

    private fun scheduleReconnect(gen: Int) {
        if (!wantOpen || gen != generation) return
        sched.schedule({ if (wantOpen && gen == generation) openNow() }, 2, TimeUnit.SECONDS)
    }

    /** 发命令 JSON。返回是否已投递到通道(不代表服务端已处理)。 */
    fun send(obj: JSONObject): Boolean = ws?.send(obj.toString()) ?: false

    fun send(vararg kv: Pair<String, Any?>): Boolean {
        val o = JSONObject()
        for ((k, v) in kv) o.put(k, v)
        return send(o)
    }

    /** 拉曲库(阻塞,调用方放 IO 线程)。失败返回 null。 */
    fun fetchLibrary(): List<Song>? {
        val h = host
        if (h.isEmpty()) return null
        return try {
            val req = Request.Builder().url("http://$h/library").build()
            http.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return null
                val body = resp.body?.string() ?: return null
                val arr: JSONArray = JSONObject(body).optJSONArray("songs") ?: JSONArray()
                (0 until arr.length()).map {
                    val s = arr.getJSONObject(it)
                    Song(s.optString("mid"), s.optString("title"), s.optString("artist"))
                }
            }
        } catch (_: Exception) { null }
    }

    /** 拉某首歌的卡拉OK数据(逐字歌词 + 音高线)。阻塞,调用方放 IO 线程。失败/无返回 null。 */
    fun fetchKaraoke(mid: String): SongLyrics? {
        val h = host
        if (h.isEmpty() || mid.isEmpty()) return null
        return try {
            val req = Request.Builder().url("http://$h/song/$mid/karaoke").build()
            http.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return null
                val body = resp.body?.string() ?: return null
                val j = JSONObject(body)
                val la = j.optJSONArray("lines") ?: JSONArray()
                val lines = (0 until la.length()).map { i ->
                    val lo = la.getJSONObject(i)
                    val ca = lo.optJSONArray("chars") ?: JSONArray()
                    val chars = (0 until ca.length()).map { c ->
                        val co = ca.getJSONObject(c)
                        LyricChar(co.optString("text"), co.optInt("start"), co.optInt("dur"))
                    }
                    LyricLine(lo.optInt("start"), lo.optInt("end"), chars)
                }
                val na = j.optJSONArray("notes") ?: JSONArray()
                val notes = (0 until na.length()).map { i ->
                    val no = na.getJSONObject(i)
                    Note(no.optInt("start"), no.optInt("dur"), no.optDouble("pitch").toFloat())
                }
                SongLyrics(mid, lines, notes)
            }
        } catch (_: Exception) { null }
    }

    private fun normalize(raw: String): String {
        var h = raw.trim()
        h = h.removePrefix("ws://").removePrefix("wss://")
            .removePrefix("http://").removePrefix("https://")
        h = h.trimEnd('/')
        if (h.isEmpty()) return ""
        if (!h.contains(':')) h = "$h:8765"
        return h
    }
}
