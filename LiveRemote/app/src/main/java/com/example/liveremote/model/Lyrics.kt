package com.example.liveremote.model

/** 原唱音高块:起始/时长(ms)+ 归一化音高 pitch∈[0,1](高=1)。来自 pc-service 解析的 `.note`。 */
data class Note(val startMs: Int, val durMs: Int, val pitch: Float)

/** 逐字:文本 + 起始/时长(ms)。来自 QRC 逐字时间轴。 */
data class LyricChar(val text: String, val startMs: Int, val durMs: Int)

/** 一行歌词。 */
data class LyricLine(val startMs: Int, val endMs: Int, val chars: List<LyricChar>) {
    val text: String get() = chars.joinToString("") { it.text }
}

/** 一首歌的卡拉OK数据(逐字歌词 + 音高线)。为空时演唱页显示占位。 */
data class SongLyrics(
    val mid: String,
    val lines: List<LyricLine>,
    val notes: List<Note>,
)
