package com.example.liveremote.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.example.liveremote.model.Song
import com.example.liveremote.ui.components.CheckIcon
import com.example.liveremote.ui.components.CloseIcon
import com.example.liveremote.ui.components.PlusIcon
import com.example.liveremote.ui.components.SearchIcon
import com.example.liveremote.ui.components.noRippleClick
import com.example.liveremote.ui.theme.C
import com.example.liveremote.ui.theme.F
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.tween
import androidx.compose.foundation.gestures.Orientation
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.draggable
import androidx.compose.foundation.gestures.rememberDraggableState
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.unit.IntOffset
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlin.math.roundToInt

@Composable
fun PickerSheet(
    library: List<Song>,
    onAdd: (Song) -> Unit,
    onClose: () -> Unit,
) {
    var query by remember { mutableStateOf("") }
    var added by remember { mutableStateOf(setOf<String>()) }
    val scope = rememberCoroutineScope()
    val filtered = remember(query, library) {
        val q = query.trim()
        if (q.isEmpty()) library else library.filter { it.title.contains(q, true) || it.artist.contains(q, true) }
    }

    // 下拉收起:dragY 用 Animatable 存位移,面板用 **lambda 版 offset{}** 读它——位移只在布局放置
    // 阶段读取,拖动全程零重组(符合"性能红线":高频值不进组合阶段);蒙层淡出同理走 graphicsLayer{}。
    // 手势只挂在头部(把手+标题行),不与列表滚动抢事件。
    val dragY = remember { Animatable(0f) }
    var sheetHeight by remember { mutableIntStateOf(0) }
    fun dismiss() {
        scope.launch {
            val h = if (sheetHeight > 0) sheetHeight.toFloat() else 2000f
            dragY.animateTo(h, tween(160))
            onClose()
        }
    }

    Box(Modifier.fillMaxSize()) {
        // 蒙层(随下拉按比例淡出)
        Box(Modifier.fillMaxSize()
            .graphicsLayer {
                alpha = if (sheetHeight > 0) (1f - dragY.value / sheetHeight).coerceIn(0f, 1f) else 1f
            }
            .background(Color.Black.copy(alpha = 0.55f))
            .pointerInput(Unit) { detectTapGestures { dismiss() } })
        // 底部面板
        Column(
            Modifier.align(Alignment.BottomCenter).fillMaxWidth().fillMaxHeight(0.84f)
                .onSizeChanged { sheetHeight = it.height }
                .offset { IntOffset(0, dragY.value.roundToInt().coerceAtLeast(0)) }
                .clip(RoundedCornerShape(topStart = 22.dp, topEnd = 22.dp)).background(Color(0xFF101219))
                // 吃掉面板内的空白点击,别穿透到背后蒙层导致误关闭
                .pointerInput(Unit) { detectTapGestures { } }
                .navigationBarsPadding(),
        ) {
            // 头部拖拽区:把手 + 标题行。下拉超 1/4 高度或快甩即收起,否则弹回。
            Column(
                Modifier.fillMaxWidth().draggable(
                    orientation = Orientation.Vertical,
                    state = rememberDraggableState { delta ->
                        scope.launch { dragY.snapTo((dragY.value + delta).coerceAtLeast(0f)) }
                    },
                    onDragStopped = { velocity ->
                        if (velocity > 1500f || (sheetHeight > 0 && dragY.value > sheetHeight * 0.25f)) {
                            dragY.animateTo(sheetHeight.toFloat(), tween(160)); onClose()
                        } else {
                            dragY.animateTo(0f, tween(180))
                        }
                    },
                ),
            ) {
                Box(Modifier.fillMaxWidth().padding(top = 10.dp), contentAlignment = Alignment.Center) {
                    Box(Modifier.size(width = 40.dp, height = 4.dp).clip(RoundedCornerShape(2.dp)).background(Color(0xFF3A3F4C)))
                }
                Row(Modifier.fillMaxWidth().padding(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 12.dp),
                    verticalAlignment = Alignment.CenterVertically) {
                    Text("点歌", color = C.Text, fontSize = 20.sp, fontWeight = FontWeight.Bold)
                    Box(Modifier.weight(1f))
                    Box(Modifier.size(36.dp).clip(CircleShape).background(C.Card).noRippleClick { dismiss() },
                        contentAlignment = Alignment.Center) { CloseIcon(C.Text, 18.dp) }
                }
            }
            // 搜索框
            Row(
                Modifier.fillMaxWidth().padding(horizontal = 16.dp).clip(RoundedCornerShape(14.dp)).background(C.Card)
                    .border(1.dp, C.Stroke2, RoundedCornerShape(14.dp)).padding(horizontal = 14.dp, vertical = 12.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Box(Modifier.weight(1f)) {
                    if (query.isEmpty()) Text("搜歌名或歌手…", color = C.TextDim, fontSize = F.body)
                    BasicTextField(
                        value = query, onValueChange = { query = it },
                        textStyle = TextStyle(color = C.Text, fontSize = 15.sp),
                        cursorBrush = SolidColor(C.Accent), singleLine = true, modifier = Modifier.fillMaxWidth(),
                    )
                }
                SearchIcon(C.TextDim, 20.dp)
            }
            // 列表
            Row(Modifier.fillMaxWidth().padding(start = 16.dp, top = 14.dp, bottom = 10.dp)) {
                Text("曲库 · ${filtered.size}", color = C.TextDim, fontSize = F.pill, fontWeight = FontWeight.Bold)
            }
            LazyColumn(Modifier.fillMaxWidth().weight(1f).padding(horizontal = 16.dp)) {
                items(filtered, key = { it.mid }) { s ->
                    SongRow(s, isAdded = added.contains(s.mid)) {
                        onAdd(s)
                        added = added + s.mid
                        scope.launch { delay(1200); added = added - s.mid }
                    }
                }
                item { Box(Modifier.height(22.dp)) }
            }
        }
    }
}

@Composable
private fun SongRow(s: Song, isAdded: Boolean, onAdd: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(bottom = 10.dp).clip(RoundedCornerShape(14.dp)).background(C.Card)
            .border(1.dp, C.Stroke, RoundedCornerShape(14.dp)).padding(horizontal = 14.dp, vertical = 13.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(s.title, color = C.Text, fontSize = F.rowTitle)
            Text(s.artist, color = C.TextDim, fontSize = F.sub, modifier = Modifier.padding(top = 2.dp))
        }
        Box(
            Modifier.size(40.dp).clip(CircleShape).background(if (isAdded) C.Ok else C.Accent).noRippleClick { onAdd() },
            contentAlignment = Alignment.Center,
        ) { if (isAdded) CheckIcon(C.OnAccent, 20.dp) else PlusIcon(C.OnAccent, 22.dp) }
    }
}
