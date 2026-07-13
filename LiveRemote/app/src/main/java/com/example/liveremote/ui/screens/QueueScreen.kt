package com.example.liveremote.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.detectDragGesturesAfterLongPress
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.zIndex
import com.example.liveremote.model.AppState
import com.example.liveremote.ui.components.ArrowUpIcon
import com.example.liveremote.ui.components.CircleButton
import com.example.liveremote.ui.components.CloseIcon
import com.example.liveremote.ui.components.DragHandleIcon
import com.example.liveremote.ui.components.PlusIcon
import com.example.liveremote.ui.components.ProgressBar
import com.example.liveremote.ui.components.SkipNextIcon
import com.example.liveremote.ui.components.connColor
import com.example.liveremote.ui.components.noRippleClick
import com.example.liveremote.ui.theme.C
import com.example.liveremote.ui.theme.F
import kotlin.math.roundToInt

private val ROW = 66.dp

@Composable
fun QueueScreen(
    st: AppState,
    onOpenPicker: () -> Unit,
    onSkipNext: () -> Unit,
    onClear: () -> Unit,
    onRemove: (Int) -> Unit,
    onMove: (Int, Int) -> Unit,
    onMoveTop: (Int) -> Unit,
    onOpenSettings: () -> Unit,
) {
    val density = LocalDensity.current
    val rowPx = with(density) { ROW.toPx() }
    var dragIdx by remember { mutableIntStateOf(-1) }
    var dragDy by remember { mutableFloatStateOf(0f) }

    Column(Modifier.fillMaxSize()) {
        // 吸顶标题行(不随列表滚动)
        Row(Modifier.fillMaxWidth().padding(start = 16.dp, end = 16.dp, top = 14.dp, bottom = 10.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Text("队列", color = C.Text, fontSize = F.h1, fontWeight = FontWeight.Bold)
            Box(Modifier.padding(start = 9.dp).size(11.dp).clip(CircleShape).background(connColor(st.connected))
                .noRippleClick { onOpenSettings() })
            Box(Modifier.weight(1f))
            Text("清空队列", color = C.TextDim, fontSize = F.sub, modifier = Modifier.noRippleClick { onClear() }.padding(4.dp))
        }

      Column(Modifier.fillMaxWidth().weight(1f).verticalScroll(rememberScrollState())
          .padding(start = 16.dp, end = 16.dp, bottom = 20.dp)) {
        // 演唱中卡片
        if (st.hasSong) {
            Row(
                Modifier.fillMaxWidth().padding(bottom = 14.dp).clip(RoundedCornerShape(16.dp))
                    .background(Brush.verticalGradient(listOf(C.Accent.copy(alpha = 0.10f), C.Accent.copy(alpha = 0.02f))))
                    .border(1.5.dp, C.Accent, RoundedCornerShape(16.dp)).padding(horizontal = 15.dp, vertical = 14.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(Modifier.weight(1f)) {
                    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(bottom = 5.dp)) {
                        Box(Modifier.size(7.dp).clip(CircleShape).background(C.Warn))
                        Text("  演唱中", color = C.Warn, fontSize = F.tiny, fontWeight = FontWeight.Bold)
                    }
                    Text(st.nowTitle, color = C.Text, fontSize = F.cardTitle, fontWeight = FontWeight.Bold, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    Text(st.nowArtist, color = C.TextDim, fontSize = F.sub, modifier = Modifier.padding(top = 2.dp))
                    ProgressBar(
                        fraction = if (st.durMs > 0) st.posMs.toFloat() / st.durMs else 0f,
                        modifier = Modifier.padding(top = 10.dp), track = Color.White.copy(alpha = 0.10f),
                    )
                }
                CircleButton(46.dp, C.Accent.copy(alpha = 0.15f), onSkipNext, border = C.Accent.copy(alpha = 0.4f)) {
                    SkipNextIcon(C.Accent, 22.dp)
                }
            }
        }

        // 点歌按钮
        Row(
            Modifier.fillMaxWidth().padding(bottom = 18.dp).clip(RoundedCornerShape(14.dp))
                .background(C.Accent.copy(alpha = 0.06f)).border(1.5.dp, C.Accent.copy(alpha = 0.5f), RoundedCornerShape(14.dp))
                .noRippleClick { onOpenPicker() }.padding(14.dp),
            horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically,
        ) {
            PlusIcon(C.Accent, 20.dp)
            Text("  点歌 · 从曲库加歌", color = C.Accent, fontSize = F.body, fontWeight = FontWeight.Bold)
        }

        if (st.queue.isEmpty()) {
            Column(Modifier.fillMaxWidth().padding(vertical = 40.dp), horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("队列空了", color = C.Text, fontSize = androidx.compose.ui.unit.TextUnit(20f, androidx.compose.ui.unit.TextUnitType.Sp), fontWeight = FontWeight.Bold)
                Text("用上面的\"点歌\"把想唱的排上", color = C.TextDim, fontSize = F.sub)
            }
        } else {
            Row(Modifier.fillMaxWidth().padding(horizontal = 2.dp, vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                Text("等待队列 · ${st.queue.size}", color = C.TextDim, fontSize = F.pill, fontWeight = FontWeight.Bold)
                Box(Modifier.weight(1f))
                Text("长按 ≡ 拖动重排", color = C.TextFaint2, fontSize = F.tiny)
            }
            Column {
                st.queue.forEachIndexed { i, song ->
                    val dragging = i == dragIdx
                    Box(
                        Modifier.fillMaxWidth().padding(vertical = 5.dp)
                            .then(if (dragging) Modifier.zIndex(1f) else Modifier)
                            .then(if (dragging) Modifier.offset { androidx.compose.ui.unit.IntOffset(0, dragDy.roundToInt()) } else Modifier),
                    ) {
                        QueueRow(
                            idx = i + 1, title = song.title, artist = song.artist, dragging = dragging,
                            onMoveTop = { onMoveTop(i) }, onDel = { onRemove(i) },
                            handleModifier = Modifier.pointerInput(st.queue.size) {
                                detectDragGesturesAfterLongPress(
                                    onDragStart = { dragIdx = i; dragDy = 0f },
                                    onDragEnd = { dragIdx = -1; dragDy = 0f },
                                    onDragCancel = { dragIdx = -1; dragDy = 0f },
                                    onDrag = { _, amt ->
                                        if (dragIdx >= 0) {
                                            dragDy += amt.y
                                            val shift = (dragDy / rowPx).roundToInt()
                                            val target = (dragIdx + shift).coerceIn(0, st.queue.lastIndex)
                                            if (target != dragIdx) {
                                                onMove(dragIdx, target)
                                                dragDy -= shift * rowPx
                                                dragIdx = target
                                            }
                                        }
                                    },
                                )
                            },
                        )
                    }
                }
            }
        }
      }
    }
}

@Composable
private fun QueueRow(
    idx: Int,
    title: String,
    artist: String,
    dragging: Boolean,
    onMoveTop: () -> Unit,
    onDel: () -> Unit,
    handleModifier: Modifier,
) {
    Row(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp))
            .background(if (dragging) Color(0xFF232733) else C.Card)
            .border(1.dp, if (dragging) C.Accent else C.Stroke, RoundedCornerShape(14.dp))
            .padding(start = 6.dp, end = 12.dp, top = 12.dp, bottom = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(handleModifier.size(width = 26.dp, height = 44.dp), contentAlignment = Alignment.Center) {
            DragHandleIcon(Color(0xFF4A4F5C), 18.dp)
        }
        Text("$idx", color = C.TextFaint, fontSize = F.rowTitle, fontWeight = FontWeight.Bold,
            modifier = Modifier.padding(end = 6.dp))
        Column(Modifier.weight(1f)) {
            Text(title, color = C.Text, fontSize = F.rowTitle, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(artist, color = C.TextDim, fontSize = F.sub, modifier = Modifier.padding(top = 2.dp))
        }
        Box(Modifier.size(38.dp).clip(RoundedCornerShape(10.dp)).background(C.CardAlt).noRippleClick { onMoveTop() },
            contentAlignment = Alignment.Center) { ArrowUpIcon(C.Accent, 20.dp) }
        Box(Modifier.padding(start = 8.dp).size(38.dp).clip(RoundedCornerShape(10.dp)).background(C.CardAlt).noRippleClick { onDel() },
            contentAlignment = Alignment.Center) { CloseIcon(C.TextDim, 18.dp) }
    }
}
