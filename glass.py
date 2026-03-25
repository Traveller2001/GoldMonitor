import os

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath
from PyQt6.QtWidgets import QApplication, QDialog

_DIR = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")


class GlassDialog(QDialog):
    """半透明毛玻璃风格对话框基类"""

    def __init__(self, parent=None, width=360, height=330):
        super().__init__(parent)
        self._drag_pos = None  # type: QPoint | None
        self._width = width
        self._height = height

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        self.setFixedSize(width, height)

        # 居中屏幕
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.move(
                geo.x() + (geo.width() - width) // 2,
                geo.y() + (geo.height() - height) // 2,
            )

        self.setStyleSheet(self._build_stylesheet())

    def _build_stylesheet(self):
        arrow_up = f"{_DIR}/arrow_up.svg"
        arrow_dn = f"{_DIR}/arrow_down.svg"
        return f"""
            QLabel {{
                color: rgba(255,255,255,0.7);
                font-size: 13px;
                background: transparent;
            }}
            QSpinBox, QDoubleSpinBox {{
                background: rgba(255,255,255,0.08);
                color: #e0e0e0;
                border: 1px solid rgba(255,255,255,0.15);
                border-radius: 6px;
                padding: 4px 18px 4px 6px;
                font-size: 13px;
            }}
            QSpinBox:focus, QDoubleSpinBox:focus {{
                border: 1px solid rgba(74,158,255,0.6);
            }}
            QSpinBox::up-button, QDoubleSpinBox::up-button {{
                subcontrol-origin: border;
                subcontrol-position: top right;
                width: 18px;
                border-top-right-radius: 5px;
                border-left: 1px solid rgba(255,255,255,0.1);
                background: rgba(255,255,255,0.05);
            }}
            QSpinBox::down-button, QDoubleSpinBox::down-button {{
                subcontrol-origin: border;
                subcontrol-position: bottom right;
                width: 18px;
                border-bottom-right-radius: 5px;
                border-left: 1px solid rgba(255,255,255,0.1);
                background: rgba(255,255,255,0.05);
            }}
            QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
            QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
                background: rgba(255,255,255,0.15);
            }}
            QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
                image: url({arrow_up});
                width: 7px;
                height: 7px;
            }}
            QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
                image: url({arrow_dn});
                width: 7px;
                height: 7px;
            }}
            QPlainTextEdit {{
                background: rgba(0,0,0,0.3);
                color: rgba(255,255,255,0.8);
                border: 1px solid rgba(255,255,255,0.1);
                border-radius: 8px;
                padding: 10px;
                font-family: Menlo, Consolas, monospace;
                font-size: 12px;
                selection-background-color: rgba(74,158,255,0.3);
            }}
            QPushButton {{
                background: rgba(74,158,255,0.8);
                color: white;
                border: none;
                border-radius: 8px;
                padding: 8px 24px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: rgba(74,158,255,1.0);
            }}
            QPushButton:pressed {{
                background: rgba(50,130,230,1.0);
            }}
            QPushButton#closeBtn {{
                background: rgba(255,255,255,0.1);
            }}
            QPushButton#closeBtn:hover {{
                background: rgba(255,255,255,0.2);
            }}
        """

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 16, 16)
        painter.setClipPath(path)

        # 半透明深色背景
        painter.fillRect(self.rect(), QColor(25, 25, 30, 200))

        # 顶部微光
        from PyQt6.QtGui import QLinearGradient
        gradient = QLinearGradient(0, 0, 0, 60)
        gradient.setColorAt(0, QColor(255, 255, 255, 15))
        gradient.setColorAt(1, QColor(255, 255, 255, 0))
        painter.fillRect(0, 0, self.width(), 60, gradient)

        # 边框
        painter.setPen(QColor(255, 255, 255, 25))
        painter.drawPath(path)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # Only start drag if clicking on the dialog background, not on child widgets
            child = self.childAt(event.position().toPoint())
            if child is None:
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            else:
                super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
