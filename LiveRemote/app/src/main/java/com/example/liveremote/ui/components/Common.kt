package com.example.liveremote.ui.components

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animate
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.example.liveremote.ui.theme.C
import com.example.liveremote.ui.theme.F
import kotlinx.coroutines.launch

fun connColor(connected: Boolean): Color = if (connected) C.Ok else C.Danger

/**
 * 无涟漪点击,但**带真实按压手感**。全 App 的按钮几乎都走这个 modifier,所以在这里统一补按压效果
 * 最省事、覆盖最全。三层反馈叠加,对齐一线 App 的"实体键"体验:
 *  1. **弹性缩放**:按下即弹到 0.96、松手回弹(spring),像被按进去又弹起来——这是"按下去"的主体现。
 *     缩放用 graphicsLayer,并**前置**到整条链最外层(`graphicsLayer(...).then(this)`),这样它包住调用方
 *     的 `clip/background/内容`,是整枚按钮(含底色描边)一起缩,而不是只缩里面的图标/文字。
 *     graphicsLayer 只影响绘制不改测量,前置不会打乱布局;缩放中心默认取节点正中,任何按钮都对。
 *  2. **淡高亮**:按下瞬间叠一层白到 10%(随 clip 裁切),松开淡出,补足颜色维度的即时反馈。
 *  3. **轻触感**:按下那一刻发一次极轻的触感反馈(TextHandleMove),强化"按到了"。
 */
@Composable
fun Modifier.noRippleClick(enabled: Boolean = true, onClick: () -> Unit): Modifier {
    val src = remember { MutableInteractionSource() }
    val pressed by src.collectIsPressedAsState()
    val haptic = LocalHapticFeedback.current
    // 缩放:弹性回弹给"实体键"手感。dampingRatio 略低(0.55)留一丝回弹,stiffness 偏高按下够跟手。
    val scale by animateFloatAsState(
        targetValue = if (pressed && enabled) 0.96f else 1f,
        animationSpec = spring(dampingRatio = 0.55f, stiffness = 620f),
        label = "pressScale",
    )
    // 高亮:按下 40ms 快到位,松开 160ms 慢淡出。
    val glow by animateFloatAsState(
        targetValue = if (pressed) 1f else 0f,
        animationSpec = tween(if (pressed) 40 else 160),
        label = "pressGlow",
    )
    // 按下那一刻(非松手)发一次轻触感。
    LaunchedEffect(pressed) {
        if (pressed && enabled) haptic.performHapticFeedback(HapticFeedbackType.TextHandleMove)
    }
    return Modifier
        .graphicsLayer { scaleX = scale; scaleY = scale }
        .then(this)
        .clickable(interactionSource = src, indication = null, enabled = enabled) { onClick() }
        .drawWithContent {
            drawContent()
            if (glow > 0f) drawRect(Color.White.copy(alpha = 0.10f * glow))
        }
}

/** 调号/音源 小胶囊。accent=true 时青字。 */
@Composable
fun Pill(text: String, accent: Boolean, modifier: Modifier = Modifier) {
    Box(
        modifier
            .clip(RoundedCornerShape(9.dp))
            .background(C.Card)
            .border(1.dp, C.Stroke2, RoundedCornerShape(9.dp))
            .padding(horizontal = 11.dp, vertical = 5.dp)
    ) {
        Text(text, color = if (accent) C.Accent else C.Text, fontSize = F.pill, fontWeight = FontWeight.Bold)
    }
}

@Composable
fun SectionLabel(text: String, modifier: Modifier = Modifier) {
    Text(text, color = C.TextDim, fontSize = F.pill, fontWeight = FontWeight.Bold, modifier = modifier)
}

/** 细进度条。 */
@Composable
fun ProgressBar(
    fraction: Float,
    modifier: Modifier = Modifier,
    track: Color = C.Stroke,
    bar: Color = C.Accent,
    h: Dp = 4.dp,
) {
    Box(modifier.fillMaxWidth().height(h).clip(RoundedCornerShape(h / 2)).background(track)) {
        Box(Modifier.fillMaxWidth(fraction.coerceIn(0f, 1f)).height(h).clip(RoundedCornerShape(h / 2)).background(bar))
    }
}

/** 开关(窗口显隐)。 */
@Composable
fun ToggleSwitch(on: Boolean, onToggle: () -> Unit) {
    Box(
        Modifier
            .size(width = 50.dp, height = 30.dp)
            .clip(RoundedCornerShape(15.dp))
            .background(if (on) C.Accent else C.Stroke2)
            .noRippleClick { onToggle() }
            .padding(3.dp),
        contentAlignment = if (on) Alignment.CenterEnd else Alignment.CenterStart,
    ) {
        Box(Modifier.size(24.dp).clip(CircleShape).background(Color.White))
    }
}

@Composable
fun RowScope.WeightSpacer() { Box(Modifier.weight(1f)) }

/**
 * 长按确认按钮:按住 [holdMs] 毫秒,期间进度条从左到右填满,满即触发 [onConfirm](即使还没松手)——
 * 触发瞬间**中等震动一下**(LongPress 触感)并把进度条清空,给明确的"已生效"反馈;
 * 中途松手则取消、进度归零。用于"归位"这类误触代价高的操作。
 */
@Composable
fun HoldToConfirmButton(
    label: String,
    hint: String,
    onConfirm: () -> Unit,
    modifier: Modifier = Modifier,
    holdMs: Int = 2000,
) {
    var progress by remember { mutableFloatStateOf(0f) }
    val scope = rememberCoroutineScope()
    val haptic = LocalHapticFeedback.current
    Box(
        modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(13.dp))
            .background(C.Card)
            .border(1.dp, if (progress > 0f) C.Accent.copy(alpha = 0.6f) else C.Stroke2, RoundedCornerShape(13.dp))
            .pointerInput(holdMs) {
                detectTapGestures(onPress = {
                    val job = scope.launch {
                        animate(0f, 1f, animationSpec = tween(holdMs, easing = LinearEasing)) { v, _ -> progress = v }
                        // 满 holdMs 即触发,无需松手:中等震动 + 进度条立即清空 + 执行动作
                        haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                        progress = 0f
                        onConfirm()
                    }
                    tryAwaitRelease()               // 松手(或手势取消)后返回
                    job.cancel()                    // 未满则取消填充
                    progress = 0f
                })
            },
        contentAlignment = Alignment.Center,
    ) {
        if (progress > 0f) {
            Box(Modifier.matchParentSize().clip(RoundedCornerShape(13.dp))) {
                Box(Modifier.fillMaxWidth(progress).fillMaxHeight().background(C.Accent.copy(alpha = 0.22f)))
            }
        }
        Row(Modifier.padding(12.dp)) {
            Text("$label ", color = C.TextDim, fontSize = F.body)
            Text(hint, color = C.TextDim.copy(alpha = 0.7f), fontSize = F.tiny)
        }
    }
}

/** 圆形按钮:底色 + 可选描边 + 居中内容(图标)。 */
@Composable
fun CircleButton(
    size: Dp,
    bg: Color,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    border: Color? = null,
    glow: Boolean = false,
    content: @Composable () -> Unit,
) {
    Box(
        modifier
            .size(size)
            .clip(CircleShape)
            .background(bg)
            .then(if (border != null) Modifier.border(1.dp, border, CircleShape) else Modifier)
            .noRippleClick { onClick() },
        contentAlignment = Alignment.Center,
    ) { content() }
}
