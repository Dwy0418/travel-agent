"""托盘壳的核心部件：图标、单实例握手、可收起的窗口。

不依赖 Streamlit，也不改 Web 应用；tray_shell.py 负责把网页装进来。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QMainWindow, QSystemTrayIcon

SERVER_NAME = "travel-agent-shell-single-instance-v1"
TRAY_TIP = "已最小化到托盘，右键图标可退出。"


def make_icon(size: int = 64) -> QIcon:
    """运行时画一个「旅」字图标，避免额外二进制资源。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#2f6fed"))
    painter.drawRoundedRect(2, 2, size - 4, size - 4, size * 0.22, size * 0.22)
    painter.setPen(QColor("#ffffff"))
    font = QFont()
    font.setPixelSize(int(size * 0.52))
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "旅")
    painter.end()
    return QIcon(pixmap)


def activate_existing(timeout_ms: int = 400) -> bool:
    """已有实例在跑：发一个 show 指令后返回 True。"""
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(b"show\n")
    sock.flush()
    sock.waitForBytesWritten(timeout_ms)
    sock.disconnectFromServer()
    return True


class ShellWindow(QMainWindow):
    """关闭按钮只隐藏窗口；只有 request_quit() 才真正退出。"""

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setWindowTitle(title)
        self.resize(1180, 820)
        self._tray: QSystemTrayIcon | None = None
        self._tip_shown = False
        self._quitting = False

    def attach_tray(self, tray: QSystemTrayIcon) -> None:
        self._tray = tray

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._quitting:
            event.accept()
            return
        event.ignore()
        self.hide()
        if not self._tip_shown:
            self._tip_shown = True
            if self._tray is not None and QSystemTrayIcon.isSystemTrayAvailable():
                self._tray.showMessage(
                    "行程规划助手", TRAY_TIP, QSystemTrayIcon.Information, 4000
                )

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def request_quit(self) -> None:
        self._quitting = True
        self.close()


class SingleInstance:
    """Windows 命名管道（QLocalServer）实现的单实例唤起。"""

    def __init__(self, on_activate) -> None:
        self._on_activate = on_activate
        self._server = QLocalServer()
        QLocalServer.removeServer(SERVER_NAME)
        self._server.newConnection.connect(self._handle)

    def listen(self) -> bool:
        return self._server.listen(SERVER_NAME)

    def _handle(self) -> None:
        while self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            if conn is None:
                continue
            conn.disconnected.connect(conn.deleteLater)
            self._on_activate()
            conn.disconnectFromServer()
