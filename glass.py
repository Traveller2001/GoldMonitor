"""Frameless translucent dialog base shared by settings, logs and the outlook panel."""

import os
from typing import Optional

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath
from PyQt6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget

import theme
from widgets import IconButton

_DIR = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")


def section_label(text):
    # type: (str) -> QLabel
    label = QLabel(text)
    label.setObjectName("section")
    return label


def hint_label(text):
    # type: (str) -> QLabel
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    return label


class GlassDialog(QDialog):
    """Dark rounded dialog with a draggable header and a close button.

    Subclasses add their content to ``self.body``.
    """

    def __init__(self, parent=None, width=360, height=330, title=""):
        # type: (Optional[QWidget], int, int, str) -> None
        super().__init__(parent)
        self._drag_pos = None  # type: Optional[QPoint]
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        self.setFixedSize(width, height)
        self.setWindowTitle(title or "GoldMonitor")
        self.setFont(theme.ui_font(13))
        self.setStyleSheet(self._build_stylesheet())

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 18)
        root.setSpacing(10)
        header = QHBoxLayout()
        header.setSpacing(8)
        self.header = header
        self.title_label = QLabel(title)
        self.title_label.setFont(theme.ui_font(16, QFont.Weight.DemiBold))
        self.title_label.setStyleSheet(f"color: {theme.TEXT};")
        header.addWidget(self.title_label)
        header.addStretch()
        self.close_button = IconButton("close", "关闭", self, size=24)
        self.close_button.clicked.connect(self.reject)
        header.addWidget(self.close_button)
        root.addLayout(header)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        root.addLayout(self.body, 1)

        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.x() + (geo.width() - width) // 2, geo.y() + (geo.height() - height) // 2)

    def _build_stylesheet(self):
        arrow_up = f"{_DIR}/arrow_up.svg"
        arrow_dn = f"{_DIR}/arrow_down.svg"
        check = f"{_DIR}/check.svg"
        return f"""
            QLabel {{ color: {theme.TEXT_DIM}; background: transparent; }}
            QLabel#section {{ color: {theme.GOLD}; font-size: 12px; font-weight: 600; padding-top: 4px; }}
            QLabel#hint {{ color: {theme.TEXT_MUTED}; font-size: 11px; }}
            QLabel#error {{ color: #ffb5b5; font-size: 11px; }}
            QSpinBox, QDoubleSpinBox, QLineEdit, QComboBox {{
                background: rgba(255,255,255,0.07); color: #e6e8ec;
                border: 1px solid rgba(255,255,255,0.13); border-radius: 6px;
                padding: 4px 18px 4px 7px; font-size: 13px; min-height: 18px;
                selection-background-color: rgba(91,157,255,0.35);
            }}
            QLineEdit {{ padding-right: 7px; }}
            QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus, QComboBox:focus {{
                border: 1px solid rgba(232,196,124,0.65);
            }}
            QSpinBox::up-button, QDoubleSpinBox::up-button {{
                subcontrol-origin: border; subcontrol-position: top right; width: 18px;
                border-top-right-radius: 5px; border-left: 1px solid rgba(255,255,255,0.1);
                background: rgba(255,255,255,0.05);
            }}
            QSpinBox::down-button, QDoubleSpinBox::down-button {{
                subcontrol-origin: border; subcontrol-position: bottom right; width: 18px;
                border-bottom-right-radius: 5px; border-left: 1px solid rgba(255,255,255,0.1);
                background: rgba(255,255,255,0.05);
            }}
            QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
            QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
                background: rgba(255,255,255,0.15);
            }}
            QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({arrow_up}); width: 7px; height: 7px; }}
            QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QComboBox::down-arrow {{
                image: url({arrow_dn}); width: 7px; height: 7px;
            }}
            QComboBox::drop-down {{ border: none; width: 18px; }}
            QComboBox QAbstractItemView {{
                background: #23272f; color: #e6e8ec; border: 1px solid rgba(255,255,255,0.12);
                selection-background-color: rgba(232,196,124,0.25); outline: none;
            }}
            QCheckBox {{ color: {theme.TEXT_DIM}; font-size: 13px; spacing: 8px; background: transparent; }}
            QCheckBox::indicator {{
                width: 14px; height: 14px; border-radius: 4px;
                border: 1px solid rgba(255,255,255,0.28); background: rgba(255,255,255,0.05);
            }}
            QCheckBox::indicator:checked {{
                background: {theme.GOLD}; border: 1px solid {theme.GOLD}; image: url({check});
            }}
            QCheckBox::indicator:disabled {{ border: 1px solid rgba(255,255,255,0.12); }}
            QCheckBox::indicator:checked:disabled {{ background: rgba(232,196,124,0.35); }}
            QCheckBox:disabled {{ color: {theme.TEXT_FAINT}; }}
            QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{ color: {theme.TEXT_FAINT}; }}
            QPlainTextEdit {{
                background: rgba(0,0,0,0.28); color: rgba(255,255,255,0.82);
                border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 10px;
                font-family: Menlo, Consolas, monospace; font-size: 12px;
                selection-background-color: rgba(91,157,255,0.3);
            }}
            QPushButton {{
                background: rgba(232,196,124,0.92); color: #2a1f0b; border: none;
                border-radius: 8px; padding: 7px 22px; font-size: 13px; font-weight: 600;
            }}
            QPushButton:hover {{ background: #f0d08f; }}
            QPushButton:pressed {{ background: #d7b168; }}
            QPushButton:disabled {{ background: rgba(255,255,255,0.08); color: {theme.TEXT_FAINT}; }}
            QPushButton#secondary {{ background: rgba(255,255,255,0.09); color: {theme.TEXT}; }}
            QPushButton#secondary:hover {{ background: rgba(255,255,255,0.16); }}
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{ background: transparent; width: 6px; margin: 2px; }}
            QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.16); border-radius: 3px; min-height: 24px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
            QToolTip {{ background: #2a2e36; color: #eef0f4; border: 1px solid rgba(255,255,255,0.12); }}
        """

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, self.width() - 1, self.height() - 1, 16, 16)
        painter.setClipPath(path)
        painter.fillRect(self.rect(), theme.DIALOG)
        sheen = QLinearGradient(0, 0, 0, 64)
        sheen.setColorAt(0, theme.SHEEN)
        sheen.setColorAt(1, QColor(255, 255, 255, 0))
        painter.fillRect(0, 0, self.width(), 64, sheen)
        painter.setClipping(False)
        painter.setPen(theme.BORDER)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.childAt(event.position().toPoint()) in (
                None, self.title_label):
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)
