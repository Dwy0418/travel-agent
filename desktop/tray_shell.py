"""travel-agent 的 Windows 托盘壳。

主路径：本地启动 `streamlit run app.py`，再用 Qt WebEngine 打开 localhost。
切换：设置环境变量 TRAVEL_AGENT_URL=https://... 直接开线上地址，不启本地进程。
关闭行为：点 × 只隐藏到托盘；右键托盘图标「退出」才结束进程。
单实例：重复双击时通过命名管道唤起已有窗口，不再开第二个。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMenu,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from shell_core import (
    ShellWindow,
    SingleInstance,
    activate_existing,
    make_icon,
)

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
WINDOW_TITLE = "行程规划助手"
FROZEN = bool(getattr(sys, "frozen", False))


def free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_streamlit(port: int):
    """源码运行时开子进程；exe 里没有独立解释器，改为进程内线程启动。"""
    args = [
        "streamlit", "run", str(ROOT / "app.py"),
        "--server.port", str(port),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]
    if FROZEN:
        from streamlit.web import cli as stcli

        def run() -> None:
            old = sys.argv
            sys.argv = args
            try:
                stcli.main()
            finally:
                sys.argv = old

        threading.Thread(target=run, daemon=True).start()
        return None

    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.Popen(
        [sys.executable, "-m", *args], cwd=str(ROOT), creationflags=flags
    )


def wait_ready(url: str, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.2) as resp:
                if resp.status < 500:
                    return True
        except Exception:  # noqa: BLE001 - 服务未就绪时继续等
            time.sleep(0.4)
    return False


def build_content(url: str) -> QWidget:
    """测试时可切到纯文本，避免拉起 Chromium。"""
    if os.environ.get("TRAVEL_AGENT_SHELL_CONTENT") == "label":
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.addWidget(QLabel(f"travel-agent shell\n{url}"))
        return holder
    from PySide6.QtWebEngineWidgets import QWebEngineView

    view = QWebEngineView()
    view.load(QUrl(url))
    return view


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(WINDOW_TITLE)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(make_icon())

    if activate_existing():
        print("已有实例，已唤起")
        return 0

    remote = (os.environ.get("TRAVEL_AGENT_URL") or "").strip()
    process: subprocess.Popen | None = None
    if remote:
        url = remote
    else:
        if not (ROOT / "app.py").exists():
            print(f"找不到 app.py：{ROOT}", file=sys.stderr)
            return 2
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        process = start_streamlit(port)
        if not wait_ready(url):
            print("streamlit 启动超时，仍尝试打开窗口", file=sys.stderr)

    window = ShellWindow(WINDOW_TITLE)
    window.setCentralWidget(build_content(url))

    tray = QSystemTrayIcon(make_icon(), app)
    tray.setToolTip(WINDOW_TITLE)
    menu = QMenu()
    show_action = QAction("显示主窗口", menu)
    quit_action = QAction("退出", menu)
    menu.addAction(show_action)
    menu.addSeparator()
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    window.attach_tray(tray)

    def quit_all() -> None:
        tray.hide()
        window.request_quit()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        app.quit()

    show_action.triggered.connect(window.show_window)
    quit_action.triggered.connect(quit_all)
    tray.activated.connect(
        lambda reason: window.show_window()
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick)
        else None
    )
    tray.show()

    holder = SingleInstance(window.show_window)
    if not holder.listen():
        print("单实例监听失败，仍继续运行", file=sys.stderr)

    window.show_window()

    shot = (os.environ.get("TRAVEL_AGENT_SHELL_SCREENSHOT") or "").strip()
    if shot:
        def grab() -> None:
            window.grab().save(shot)
            print(f"screenshot saved: {shot}")
        QTimer.singleShot(2500, grab)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
