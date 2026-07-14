package com.example.liveremote

import android.app.Application
import android.content.Context
import android.media.AudioManager
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.example.liveremote.model.AppState
import com.example.liveremote.model.Song
import com.example.liveremote.model.SongLyrics
import com.example.liveremote.net.RemoteClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

class RemoteViewModel(app: Application) : AndroidViewModel(app) {

    private val prefs = app.getSharedPreferences("cfg", Application.MODE_PRIVATE)

    private val _state = MutableStateFlow(AppState())
    val state = _state.asStateFlow()

    private val _library = MutableStateFlow<List<Song>>(emptyList())
    val library = _library.asStateFlow()

    private val _toast = MutableSharedFlow<String>(extraBufferCapacity = 4)
    val toast = _toast.asSharedFlow()

    private val _lyrics = MutableStateFlow<SongLyrics?>(null)
    val lyrics = _lyrics.asStateFlow()

    val savedIp: String get() = prefs.getString("ip", "") ?: ""

    private var prevSinging = false
    private var autoPausedByUs = false
    private var lastLyricMid = ""

    // 乐观更新抑制窗:本地刚改过 调/音源/进度 后,在这段时间内忽略服务端对该字段的回推,
    // 等播放器把新值上报上来再放行。否则"点击瞬间变了→下一帧 500ms 回推旧值→又变回来"的闪动。
    private var keyLockUntil = 0L
    private var vocalLockUntil = 0L
    private var seekLockUntil = 0L
    private var playLockUntil = 0L
    private var volLockUntil = 0L
    private val OPT_LOCK_MS = 1200L
    private fun now() = System.currentTimeMillis()

    // 刚连上(含断线重连)时,以**后端**的演唱音量为准:把手机媒体音量设成后端值(后端有持久缓存)。
    // 之后音量键/换歌才回到"手机音量 → 后端"的正常方向。
    private var volPullPending = false
    private var skipVolPushOnce = false

    private val client = RemoteClient(
        onState = { j -> reduce(j) },
        onConn = { ok ->
            _state.value = _state.value.copy(connected = ok)
            if (ok) { volPullPending = true; refreshLibrary() }
        },
    )

    init {
        if (savedIp.isNotBlank()) connect(savedIp)
    }

    // ───────────────────────── 连接 ─────────────────────────
    fun connect(host: String) {
        prefs.edit().putString("ip", host).apply()
        client.connect(host)
    }

    fun disconnect() = client.disconnect()

    fun currentHost(): String = client.currentHost().ifEmpty { savedIp }

    fun refreshLibrary() {
        viewModelScope.launch(Dispatchers.IO) {
            client.fetchLibrary()?.let { list ->
                withContext(Dispatchers.Main) { _library.value = list.sortedBy { it.title } }
            }
        }
    }

    private fun toast(msg: String) { _toast.tryEmit(msg) }

    // ───────────────────────── 状态归约 ─────────────────────────
    private fun reduce(j: JSONObject) {
        // 连接后的第一帧状态:先把后端演唱音量同步到手机媒体音量(方向:后端 → 手机),
        // 并跳过本帧可能触发的"换歌 push"(否则手机音量档位粗,会把后端精确值覆盖成近似值)。
        if (volPullPending && j.has("k_vol")) {
            volPullPending = false
            skipVolPushOnce = true
            applyBackendVolToPhone(j.optInt("k_vol", 100))
        }
        val now = j.optJSONObject("now")
        val hasSong = now != null
        val queueArr = j.optJSONArray("queue")
        val queue = if (queueArr == null) emptyList() else
            (0 until queueArr.length()).map {
                val s = queueArr.getJSONObject(it)
                Song(s.optString("mid"), s.optString("title"), s.optString("artist"))
            }
        val old = _state.value
        val ns = old.copy(
            hasSong = hasSong,
            nowMid = now?.optString("mid") ?: j.optString("k_mid", ""),
            nowTitle = now?.optString("title") ?: j.optString("k_title", ""),
            nowArtist = now?.optString("artist") ?: j.optString("k_artist", ""),
            playing = if (now() < playLockUntil) old.playing else j.optBoolean("k_playing", old.playing),
            posMs = if (now() < seekLockUntil) old.posMs else j.optInt("k_pos", old.posMs),
            durMs = j.optInt("k_dur", old.durMs),
            key = if (now() < keyLockUntil) old.key else j.optInt("k_key", old.key),
            vocal = if (now() < vocalLockUntil) old.vocal else j.optBoolean("k_vocal", old.vocal),
            kVol = if (now() < volLockUntil) old.kVol else j.optInt("k_vol", old.kVol),
            queue = queue,
            // scene=null(服务端归位后)→ 0=全部未选中;缺字段才保留旧值
            scene = if (!j.has("scene")) old.scene else if (j.isNull("scene")) 0 else j.optInt("scene"),
            bgmPlaying = j.optBoolean("bgm_playing", old.bgmPlaying),
            bgmVol = if (j.has("bgm_vol") && !j.isNull("bgm_vol")) j.optInt("bgm_vol") else old.bgmVol,
            bgmTitle = j.optString("bgm_title", old.bgmTitle),
            bgmArtist = j.optString("bgm_artist", old.bgmArtist),
            bgmPos = j.optInt("bgm_pos", old.bgmPos),
            bgmDur = j.optInt("bgm_dur", old.bgmDur),
            studioVisible = j.optBoolean("studio_visible", old.studioVisible),
            playerVisible = j.optBoolean("player_visible", old.playerVisible),
            pitchVisible = j.optBoolean("pitch_visible", old.pitchVisible),
            setlistVisible = j.optBoolean("setlist_visible", old.setlistVisible),
            libCount = j.optInt("lib_count", old.libCount),
        )
        _state.value = ns
        interlockBgm(ns)
        maybeFetchLyrics(ns.nowMid)
        // 换歌开唱时,把手机当前媒体音量百分比同步给伴奏(之后音量键的每次增减也会同步);
        // 连接首帧刚做过"后端 → 手机"同步,本帧跳过 push。
        if (ns.hasSong && ns.nowMid.isNotBlank() && ns.nowMid != old.nowMid && !skipVolPushOnce) {
            syncKaraokeVolFromPhone()
        }
        skipVolPushOnce = false
    }

    /** 把后端演唱音量百分比设为手机媒体音量(静默,不弹系统音量条)。 */
    private fun applyBackendVolToPhone(pct: Int) {
        try {
            val am = getApplication<Application>().getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
            val target = ((pct.coerceIn(0, 100) * max + 50) / 100).coerceIn(0, max)
            am.setStreamVolume(AudioManager.STREAM_MUSIC, target, 0)
        } catch (_: Exception) { }   // 个别机型免打扰/权限限制时忽略,不影响其它功能
    }

    /** 读手机媒体音量(STREAM_MUSIC)的百分比。 */
    private fun phoneVolPercent(): Int {
        val am = getApplication<Application>().getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
        val cur = am.getStreamVolume(AudioManager.STREAM_MUSIC)
        return if (max > 0) (cur * 100 / max).coerceIn(0, 100) else 100
    }

    /** 把手机媒体音量百分比同步为伴奏音量(音量键按下 / 开唱时调用)。 */
    fun syncKaraokeVolFromPhone() {
        val pct = phoneVolPercent()
        volLockUntil = now() + OPT_LOCK_MS
        _state.value = _state.value.copy(kVol = pct)
        client.send("cmd" to "kvol", "value" to pct)
    }

    /** 正在唱的歌切换时,拉它的卡拉OK数据(逐字+音高)供演唱页渲染。 */
    private fun maybeFetchLyrics(mid: String) {
        if (mid == lastLyricMid) return
        lastLyricMid = mid
        if (mid.isBlank()) { _lyrics.value = null; return }
        viewModelScope.launch(Dispatchers.IO) {
            val data = client.fetchKaraoke(mid)
            withContext(Dispatchers.Main) {
                if (lastLyricMid == mid) _lyrics.value = data
            }
        }
    }

    /** 演唱 ↔ 背景音乐联动:开唱自动暂停 QQ音乐,停唱自动恢复(仅恢复我们暂停过的)。 */
    private fun interlockBgm(s: AppState) {
        val singing = s.hasSong && s.playing
        if (singing == prevSinging) return
        prevSinging = singing
        if (singing) {
            if (s.bgmPlaying) {
                client.send("cmd" to "bgm", "action" to "playpause")
                autoPausedByUs = true
                toast("开始演唱 · 已暂停背景音乐")
            }
        } else {
            if (autoPausedByUs && !s.bgmPlaying) {
                client.send("cmd" to "bgm", "action" to "playpause")
                toast("演唱停止 · 已恢复背景音乐")
            }
            autoPausedByUs = false
        }
    }

    // ───────────────────────── K歌指令 ─────────────────────────
    fun enqueue(song: Song) {
        client.send("cmd" to "kqueue_add", "mid" to song.mid)
        toast("已加入队列")
    }
    fun removeAt(idx: Int) {
        // 乐观移除,避免等服务端回推的空窗
        _state.value = _state.value.copy(queue = _state.value.queue.filterIndexed { i, _ -> i != idx })
        client.send("cmd" to "kqueue_remove", "idx" to idx)
    }
    fun playNext() { client.send("cmd" to "kqueue_next"); toast("切下一首") }
    fun clearQueue() {
        if (_state.value.queue.isNotEmpty()) toast("已清空队列")
        client.send("cmd" to "kqueue_clear")
    }
    /** 队列重排(长按拖动 / 置顶)。后端 kqueue_move{from,to} 原子重排;本地乐观更新保证顺滑。 */
    fun moveInQueue(from: Int, to: Int) {
        val q = _state.value.queue.toMutableList()
        if (from !in q.indices || to !in q.indices || from == to) return
        val it = q.removeAt(from); q.add(to, it)
        _state.value = _state.value.copy(queue = q)
        client.send("cmd" to "kqueue_move", "from" to from, "to" to to)
    }
    fun moveTop(idx: Int) = moveInQueue(idx, 0)
    fun playPause() {
        playLockUntil = now() + OPT_LOCK_MS
        _state.value = _state.value.copy(playing = !_state.value.playing)   // 乐观,按钮立即切换
        client.send("cmd" to "kplaypause")
    }
    fun keyDelta(delta: Int) {
        val nk = (_state.value.key + delta).coerceIn(-6, 6)
        keyLockUntil = now() + OPT_LOCK_MS                   // 抑制回推,防闪动
        _state.value = _state.value.copy(key = nk)          // 乐观,立即显示新调
        client.send("cmd" to "kkey", "semi" to nk)
    }
    fun toggleVocal() {
        val nv = !_state.value.vocal
        vocalLockUntil = now() + OPT_LOCK_MS
        _state.value = _state.value.copy(vocal = nv)
        client.send("cmd" to "kvocal", "on" to nv)
    }
    fun seekDelta(deltaMs: Int) {
        val np = (_state.value.posMs + deltaMs).coerceIn(0, _state.value.durMs.coerceAtLeast(0))
        seekLockUntil = now() + OPT_LOCK_MS
        _state.value = _state.value.copy(posMs = np)
        client.send("cmd" to "kseek", "ms" to np)
    }

    // ───────────────────────── 声卡场景 ─────────────────────────
    fun setScene(id: Int) {
        _state.value = _state.value.copy(scene = id)
        client.send("cmd" to "scene", "id" to id)
    }
    fun resetScene() {
        _state.value = _state.value.copy(scene = 0)   // 乐观清零:所有场景按钮立即回未选中
        client.send("cmd" to "reset_scene")
        toast("已归位")
    }

    // ───────────────────────── QQ音乐 BGM ─────────────────────────
    fun bgmPrev() = Unit.also { client.send("cmd" to "bgm", "action" to "prev") }
    fun bgmNext() = Unit.also { client.send("cmd" to "bgm", "action" to "next") }
    fun bgmToggle() { autoPausedByUs = false; client.send("cmd" to "bgm", "action" to "playpause") }
    fun setVolume(v: Int) {
        _state.value = _state.value.copy(bgmVol = v)
        client.send("cmd" to "bgm_vol", "value" to v)
    }

    // ───────────────────────── 窗口开关 ─────────────────────────
    fun toggleStudio() = Unit.also { client.send("cmd" to "studio_toggle") }
    fun togglePlayerWindow() = Unit.also { client.send("cmd" to "player_toggle") }
    fun togglePitch() = Unit.also { client.send("cmd" to "pitch_toggle") }
    fun toggleSetlist() = Unit.also { client.send("cmd" to "setlist_toggle") }

    override fun onCleared() { client.disconnect() }
}
