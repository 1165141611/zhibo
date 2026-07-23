package com.example.liveremote.model

/** 一首歌(曲库项 / 队列项 / 正在唱)。dur 为格式化时长字符串,曲库项才有。 */
data class Song(
    val mid: String,
    val title: String,
    val artist: String,
    val dur: String = "",
    val plays: Int = 0,      // 点歌次数(pc-service 记录);点歌列表默认按它倒序
)

/** App 全局状态。由 pc-service 的 WS `state` 消息 + 本地 UI 聚合。字段映射见 UI_SPEC.md 第四节。 */
data class AppState(
    val connected: Boolean = false,
    // —— 正在唱 ——
    val hasSong: Boolean = false,
    val nowMid: String = "",
    val nowTitle: String = "",
    val nowArtist: String = "",
    val playing: Boolean = false,
    val posMs: Int = 0,
    val durMs: Int = 0,
    val key: Int = 0,              // 半音,[-6,6]
    val vocal: Boolean = false,    // true=原唱, false=伴奏
    val kVol: Int = 100,           // 伴奏音量 0-100(手机音量键同步)
    // —— 队列 ——
    val queue: List<Song> = emptyList(),
    // —— 声卡场景 ——
    val scene: Int = 0,            // 1..5(1聊天/2湿唱/3干唱/4喇叭/5闭麦),0=未知
    // —— QQ音乐 BGM ——
    val bgmPlaying: Boolean = true,
    val bgmVol: Int = 50,
    val bgmTitle: String = "",
    val bgmArtist: String = "",
    val bgmPos: Int = 0,           // 秒
    val bgmDur: Int = 0,           // 秒
    // —— 窗口 ——
    val studioVisible: Boolean = true,
    val playerVisible: Boolean = false,
    val pitchVisible: Boolean = true,   // 音准线显隐(遥控页开关,后端缓存)
    val setlistVisible: Boolean = true, // 顶端滚动歌单显隐(遥控页开关,后端缓存)
    val libCount: Int = 0,
) {
    val keyLabel: String get() = when {
        key == 0 -> "原调"
        key > 0 -> "+$key"
        else -> "$key"
    }
    val sourceLabel: String get() = if (vocal) "原唱" else "伴奏"
    /** BGM 只读展示行:`歌名 - 歌手  0:43 / 4:27`。 */
    val bgmNowLine: String get() {
        val name = listOf(bgmTitle, bgmArtist).filter { it.isNotBlank() }.joinToString(" - ")
        val t = if (bgmDur > 0) "  ${fmt(bgmPos)} / ${fmt(bgmDur)}" else ""
        return if (name.isBlank() && t.isBlank()) "未在播放" else name + t
    }
}

/** 秒 → m:ss */
fun fmt(sec: Int): String {
    val s = sec.coerceAtLeast(0)
    return "${s / 60}:${(s % 60).toString().padStart(2, '0')}"
}

/** 毫秒 → m:ss */
fun fmtMs(ms: Int): String = fmt(ms / 1000)
