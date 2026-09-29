"""Detail panel for the macro outlook: score, factor breakdown, AI read, calendar, news."""

import html
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import theme
from glass import GlassDialog, hint_label, section_label
from outlook import Outlook

_TONE_TAGS = {1: ("鸽", theme.UP), -1: ("鹰", theme.DOWN), 0: ("中", theme.TEXT_MUTED)}


def _tone_color(tone):
    # type: (int) -> str
    return theme.UP if tone > 0 else theme.DOWN if tone < 0 else theme.GOLD


def _when(ts, now):
    # type: (float, float) -> str
    moment = datetime.fromtimestamp(ts)
    days = (moment.date() - datetime.fromtimestamp(now).date()).days
    prefix = {0: "今天", 1: "明天", -1: "昨天"}.get(days, moment.strftime("%m-%d"))
    return f"{prefix} {moment:%H:%M}"


def _countdown(ts, now):
    # type: (float, float) -> str
    seconds = ts - now
    if seconds <= 0:
        return "待公布"
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} 分钟后"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)} 小时后"
    return f"{int(seconds // 86400)} 天后"


def _with_unit(text, unit):
    # type: (str, str) -> str
    if not text:
        return ""
    if unit == "%":
        return f"{text}%"
    if unit.startswith("万"):
        return f"{text}万"
    return text


def _chip(text, color, width=None):
    chip = QLabel(text)
    chip.setFont(theme.ui_font(11, QFont.Weight.Bold))
    chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
    if width:
        chip.setFixedSize(width, 17)
    else:
        chip.setFixedHeight(17)
    chip.setStyleSheet(
        f"color: {color}; background: {theme.css_rgba(theme.with_alpha(color, 36))}; "
        "border-radius: 4px; padding: 0 5px;")
    return chip


def _label(text, color=theme.TEXT_DIM, size=9.0, weight=QFont.Weight.Normal, mono=False, wrap=False):
    label = QLabel(text)
    label.setFont(theme.mono_font(size, weight) if mono else theme.ui_font(size, weight))
    label.setStyleSheet(f"color: {color};")
    label.setWordWrap(wrap)
    return label


class ScoreGauge(QWidget):
    """-100…+100 track, bearish green on the left and bullish red on the right."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(16)
        self._score = None  # type: Optional[int]

    def set_score(self, score):
        # type: (Optional[int]) -> None
        self._score = score
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(4, self.height() / 2 - 2.5, self.width() - 8, 5)
        gradient = QLinearGradient(track.left(), 0, track.right(), 0)
        gradient.setColorAt(0.0, theme.with_alpha(theme.DOWN, 200))
        gradient.setColorAt(0.5, QColor(255, 255, 255, 40))
        gradient.setColorAt(1.0, theme.with_alpha(theme.UP, 200))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(track, 2.5, 2.5)
        painter.setPen(QPen(QColor(255, 255, 255, 90), 1))
        center = track.center().x()
        painter.drawLine(int(center), int(track.top() - 3), int(center), int(track.bottom() + 3))
        if self._score is not None:
            x = center + max(-100, min(100, self._score)) / 100 * track.width() / 2
            painter.setPen(QPen(QColor(theme.TEXT), 2))
            painter.setBrush(QColor(_tone_color((self._score > 0) - (self._score < 0))))
            painter.drawEllipse(QRectF(x - 6, self.height() / 2 - 6, 12, 12))
        painter.end()


class FactorBar(QWidget):
    """Diverging bar for one factor signal in [-1, 1]."""

    def __init__(self, signal, parent=None):
        # type: (Optional[float], Optional[QWidget]) -> None
        super().__init__(parent)
        self.setFixedSize(84, 14)
        self._signal = signal

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        mid_y = self.height() / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 18))
        painter.drawRoundedRect(QRectF(0, mid_y - 3, self.width(), 6), 3, 3)
        center = self.width() / 2
        if self._signal is None:
            painter.setPen(QPen(QColor(theme.TEXT_FAINT), 1, Qt.PenStyle.DashLine))
            painter.drawLine(int(4), int(mid_y), int(self.width() - 4), int(mid_y))
        else:
            length = max(-1.0, min(1.0, self._signal)) * center
            color = QColor(theme.UP if self._signal > 0 else theme.DOWN)
            painter.setBrush(color)
            rect = QRectF(center, mid_y - 3, length, 6).normalized()
            painter.drawRoundedRect(rect, 3, 3)
        painter.setPen(QPen(QColor(255, 255, 255, 80), 1))
        painter.drawLine(int(center), int(mid_y - 5), int(center), int(mid_y + 5))
        painter.end()


class OutlookDialog(GlassDialog):
    refresh_requested = pyqtSignal()
    deep_read_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent, width=452, height=700, title="黄金宏观展望")
        # The owner can animate the close (the panel shrinks back into the card).
        self.close_handler = None
        self.close_button.clicked.disconnect()
        self.close_button.clicked.connect(self.request_close)
        # The card hides while the panel is open, so the panel carries the live quote.
        self.quote_label = QLabel("")
        self.quote_label.setFont(theme.ui_font(12))
        self.quote_label.setTextFormat(Qt.TextFormat.RichText)
        self.header.insertWidget(self.header.indexOf(self.close_button), self.quote_label)

        summary = QFrame()
        summary.setObjectName("card")
        summary.setStyleSheet(
            f"QFrame#card {{ background: {theme.CARD}; border: 1px solid {theme.CARD_BORDER}; border-radius: 12px; }}"
        )
        card = QVBoxLayout(summary)
        card.setContentsMargins(16, 12, 16, 12)
        card.setSpacing(6)
        top = QHBoxLayout()
        self.stance_label = _label("--", theme.GOLD, 24, QFont.Weight.Bold)
        self.score_label = _label("", theme.GOLD, 24, QFont.Weight.Bold, mono=True)
        top.addWidget(self.stance_label)
        top.addWidget(self.score_label)
        top.addStretch()
        self.confidence_label = _label("", theme.TEXT_DIM, 12)
        top.addWidget(self.confidence_label, 0, Qt.AlignmentFlag.AlignBottom)
        card.addLayout(top)
        self.gauge = ScoreGauge()
        card.addWidget(self.gauge)
        scale = QHBoxLayout()
        for text, align in (("偏空", Qt.AlignmentFlag.AlignLeft), ("中性", Qt.AlignmentFlag.AlignHCenter),
                            ("偏多", Qt.AlignmentFlag.AlignRight)):
            tick = _label(text, theme.TEXT_MUTED, 11)
            tick.setAlignment(align)
            scale.addWidget(tick, 1)
        card.addLayout(scale)
        self.meta_label = _label("", theme.TEXT_MUTED, 11, wrap=True)
        card.addWidget(self.meta_label)
        self.body.addWidget(summary)

        scroll = QScrollArea()
        self._scroll = scroll
        self._shown = None  # type: Optional[Outlook]
        self._pending_scroll = 0
        self._ai_frame = None  # type: Optional[QFrame]
        # A manual deep read is in flight: (deep.at shown when clicked, busy seen since).
        self._awaiting_deep = None  # type: Optional[list]
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._content = QWidget()
        self._content.setStyleSheet("background: transparent;")
        self._sections = QVBoxLayout(self._content)
        self._sections.setContentsMargins(0, 0, 6, 0)
        self._sections.setSpacing(8)
        scroll.setWidget(self._content)
        scroll.viewport().setStyleSheet("background: transparent;")
        self.body.addWidget(scroll, 1)

        footer = QHBoxLayout()
        self.source_label = hint_label("")
        self.source_label.setWordWrap(False)
        footer.addWidget(self.source_label, 1)
        self.deep_button = QPushButton("AI 深度解读")
        self.deep_button.setObjectName("secondary")
        self.deep_button.clicked.connect(self._on_deep_clicked)
        footer.addWidget(self.deep_button)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh_requested)
        footer.addWidget(self.refresh_button)
        self.body.addLayout(footer)
        self.set_outlook(None, loading=True)

    def set_quote(self, source, price, color, note="", note_color=theme.TEXT_MUTED):
        # type: (str, str, str, str, str) -> None
        parts = [f'<span style="color:{theme.TEXT_MUTED};">{html.escape(source)}</span>',
                 f'<span style="color:{color}; font-weight:600;">¥{html.escape(price)}</span>']
        if note:
            parts.append(f'<span style="color:{note_color};">{html.escape(note)}</span>')
        self.quote_label.setText("&nbsp;".join(parts))

    def request_close(self):
        if self.close_handler is not None:
            self.close_handler()
        else:
            self.force_close()

    def reject(self):
        # Esc and window close both land here; route them through the animation.
        self.request_close()

    def force_close(self):
        self.close_handler = None
        QDialog.reject(self)

    # ----------------------------------------------------------------- render
    def _on_deep_clicked(self):
        shown = self._shown.deep if self._shown is not None else None
        self._awaiting_deep = [shown.at if shown else None, False]
        self.deep_read_requested.emit()

    def set_outlook(self, outlook, loading=False, ai_ready=False, ai_busy=False, deep_wait=""):
        # type: (Optional[Outlook], bool, bool, bool, str) -> None
        """``deep_wait`` names why a manual deep read can't start now (cooldown, AI busy)."""
        self.refresh_button.setEnabled(not loading)
        self.refresh_button.setText("刷新中…" if loading else "刷新")
        self.deep_button.setEnabled(ai_ready and not ai_busy and not deep_wait and outlook is not None)
        self.deep_button.setText("解读中…" if ai_busy else deep_wait or "AI 深度解读")
        self.deep_button.setToolTip("" if ai_ready else "在设置中开启 DeepSeek 并配置 Key 后可用")
        reveal_ai = False
        if self._awaiting_deep is not None:
            deep = outlook.deep if outlook is not None else None
            if deep is not None and deep.at != self._awaiting_deep[0]:
                reveal_ai, self._awaiting_deep = True, None
            elif ai_busy:
                self._awaiting_deep[1] = True
            elif self._awaiting_deep[1] or deep_wait:
                self._awaiting_deep = None  # finished without a new read, or never started
        if outlook is not None and outlook is self._shown:
            self._set_meta(outlook, loading)  # only the busy flags changed
            return
        self._shown = outlook
        position = self._scroll.verticalScrollBar().value()
        while self._sections.count():
            item = self._sections.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
            elif item.layout() is not None:
                _clear_layout(item.layout())

        if outlook is None:
            self.stance_label.setText("--")
            self.score_label.setText("")
            self.confidence_label.setText("")
            self.gauge.set_score(None)
            self.meta_label.setText("正在获取利率预期、经济数据和新闻…" if loading else "暂无数据")
            self.source_label.setText("")
            self._sections.addStretch()
            return

        color = _tone_color(outlook.tone)
        self.stance_label.setText(outlook.stance)
        self.stance_label.setStyleSheet(f"color: {color};")
        score = "" if outlook.score is None else f"{outlook.score:+d}".replace("-", "−")
        self.score_label.setText(score)
        self.score_label.setStyleSheet(f"color: {color};")
        self.confidence_label.setText(
            f"置信度 {outlook.confidence} · {outlook.available_factors}/{len(outlook.factors)} 个因子")
        self.gauge.set_score(outlook.score)
        self._set_meta(outlook, loading)

        self._add_factors(outlook)
        self._add_ai(outlook, ai_ready)
        self._add_upcoming(outlook)
        self._add_headlines(outlook)

        sources = sorted({part for part in (
            outlook.fed.source.split("（")[0] if outlook.fed else "",
            "华尔街见闻" if outlook.releases or outlook.headlines else "",
            *(f.note.split(" · ")[0].split(" ")[0] for f in outlook.factors if f.key in ("rates", "usd") and f.available),
        ) if part})
        self._sections.addWidget(hint_label("数据源：" + " · ".join(sources) if sources else "数据源：无"))
        self.source_label.setText(f"⚠ {len(outlook.errors)} 个数据源异常" if outlook.errors else "")
        self.source_label.setToolTip("\n".join(outlook.errors))
        self._sections.addStretch()
        self._pending_scroll = position
        QTimer.singleShot(0, self._reveal_ai if reveal_ai else self._restore_scroll)

    def _restore_scroll(self):
        try:  # the dialog may already be gone when the deferred call runs
            bar = self._scroll.verticalScrollBar()
            bar.setValue(min(self._pending_scroll, bar.maximum()))
        except RuntimeError:
            pass

    def _reveal_ai(self):
        """Scroll the fresh AI read into view and flash it so the update is noticed."""
        try:
            frame = self._ai_frame
            if frame is None:
                return
            self._content.layout().activate()
            self._scroll.verticalScrollBar().setValue(max(0, frame.y() - 4))
            frame.setStyleSheet(
                f"QFrame#aiSection {{ background: rgba(232,196,124,0.12); border-radius: 10px; }}")
            QTimer.singleShot(1500, lambda: self._unflash(frame))
        except RuntimeError:
            pass

    @staticmethod
    def _unflash(frame):
        try:
            frame.setStyleSheet("QFrame#aiSection { background: transparent; }")
        except RuntimeError:
            pass  # the section was rebuilt in the meantime

    def _set_meta(self, outlook, loading):
        updated = datetime.fromtimestamp(outlook.generated_at).strftime("%H:%M:%S")
        self.meta_label.setText(
            f"更新于 {updated}{' · 刷新中…' if loading else ''} · 未来 1–5 个交易日的宏观方向 · 仅供参考，不构成投资建议")

    def _add_factors(self, outlook):
        self._sections.addWidget(section_label("驱动因子"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        row = 0
        for factor in outlook.factors:
            name = _label(factor.name, theme.TEXT, 13, QFont.Weight.DemiBold)
            name.setToolTip(f"权重 {factor.weight:.0%}")
            grid.addWidget(name, row, 0)
            grid.addWidget(FactorBar(factor.signal), row, 1)
            points = "--" if not factor.available else f"{factor.points:+.0f}".replace("-", "−")
            points_label = _label(points, theme.signed_color(factor.points if factor.available else None),
                                  13, QFont.Weight.Bold, mono=True)
            points_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            points_label.setToolTip("对总分的贡献（权重 × 信号 × 100）")
            grid.addWidget(points_label, row, 2)
            value = _label(factor.value or "不可用", theme.TEXT_DIM if factor.available else theme.TEXT_MUTED, 12,
                           wrap=True)
            grid.addWidget(value, row + 1, 0, 1, 3)
            if factor.note:
                note = _label(factor.note, theme.TEXT_MUTED, 11, wrap=True)
                grid.addWidget(note, row + 2, 0, 1, 3)
            spacer = QWidget()
            spacer.setFixedHeight(6)
            grid.addWidget(spacer, row + 3, 0)
            row += 4
        grid.setColumnStretch(0, 1)
        self._sections.addLayout(grid)

    def _add_ai(self, outlook, ai_ready):
        frame = QFrame()
        frame.setObjectName("aiSection")
        frame.setStyleSheet("QFrame#aiSection { background: transparent; }")
        section = QVBoxLayout(frame)
        section.setContentsMargins(0, 0, 0, 4)
        section.setSpacing(8)
        self._sections.addWidget(frame)
        self._ai_frame = frame
        section.addWidget(section_label("AI 解读"))
        deep = outlook.deep
        if deep is None:
            section.addWidget(hint_label(
                "还没有深度解读。" + ("点「AI 深度解读」立即生成；关键数据公布、利率预期大幅变化或多空翻转时会自动生成。"
                                   if ai_ready else "在设置中开启 DeepSeek 后，关键数据公布时会自动生成。")))
        else:
            head = QHBoxLayout()
            head.setSpacing(8)
            head.addWidget(_chip(deep.stance, _tone_color({"偏多": 1, "偏空": -1}.get(deep.stance, 0))))
            stamp = datetime.fromtimestamp(deep.at).strftime("%m-%d %H:%M")
            head.addWidget(_label(f"{deep.trigger} · {stamp} · {deep.model}", theme.TEXT_MUTED, 11), 1)
            section.addLayout(head)
            section.addWidget(_label(deep.summary, theme.TEXT, 12, wrap=True))
            for text in deep.drivers:
                section.addWidget(_label(f"▸ {text}", theme.TEXT_DIM, 12, wrap=True))
            for text in deep.risks:
                section.addWidget(_label(f"⚠ {text}", theme.WARN, 12, wrap=True))
        if outlook.ai_status:
            section.addWidget(_label(outlook.ai_status, theme.TEXT_MUTED, 11, wrap=True))

    def _add_upcoming(self, outlook):
        if not outlook.upcoming:
            return
        self._sections.addWidget(section_label("即将公布"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        for row, item in enumerate(outlook.upcoming):
            grid.addWidget(_label(_when(item.ts, outlook.generated_at), theme.TEXT_DIM, 12), row, 0)
            title = item.event if item.kind == "data" else item.title
            important = item.importance >= 4 or item.event in ("非农就业人口变动", "FOMC利率决议")
            grid.addWidget(_label(title, theme.GOLD if important else theme.TEXT, 12,
                                  QFont.Weight.DemiBold if important else QFont.Weight.Normal, wrap=True), row, 1)
            forecast = _with_unit(item.forecast_text, item.unit)
            expect = f"预期 {forecast}" if forecast else _countdown(item.ts, outlook.generated_at)
            detail = _label(expect, theme.TEXT_MUTED, 11)
            detail.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            previous = _with_unit(item.previous_text, item.unit)
            detail.setToolTip(_countdown(item.ts, outlook.generated_at) + (f" · 前值 {previous}" if previous else ""))
            grid.addWidget(detail, row, 2)
        grid.setColumnStretch(1, 1)
        self._sections.addLayout(grid)

    def _add_headlines(self, outlook):
        if not outlook.headlines:
            return
        self._sections.addWidget(section_label("相关快讯"))
        for headline in outlook.headlines[:7]:
            tag, color = _TONE_TAGS[headline.tone]
            row = QHBoxLayout()
            row.setSpacing(6)
            chip = _chip(tag, color, width=20)
            chip.setStyleSheet(chip.styleSheet().replace("padding: 0 5px;", "padding: 0;"))
            chip.setToolTip(f"{headline.by}判读：" + {1: "偏鸽，利多黄金", -1: "偏鹰，利空黄金", 0: "中性"}[headline.tone])
            row.addWidget(chip, 0, Qt.AlignmentFlag.AlignTop)
            stamp = datetime.fromtimestamp(headline.item.ts).strftime("%H:%M")
            text = html.escape(headline.item.text[:78] + ("…" if len(headline.item.text) > 78 else ""))
            if headline.item.url:
                text = (f'<a href="{html.escape(headline.item.url, quote=True)}" '
                        f'style="color:{theme.TEXT_DIM}; text-decoration:none;">{text}</a>')
            body = QLabel(f'<span style="color:{theme.TEXT_MUTED};">{stamp}</span>&nbsp; {text}')
            body.setFont(theme.ui_font(12))
            body.setWordWrap(True)
            body.setTextFormat(Qt.TextFormat.RichText)
            body.setOpenExternalLinks(True)
            body.setToolTip(headline.item.text)
            row.addWidget(body, 1)
            self._sections.addLayout(row)


def _clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget() is not None:
            item.widget().deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())
