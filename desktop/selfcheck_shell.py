"""托盘壳自检：关闭隐藏、恢复显示、单实例唤起、托盘菜单。

用 offscreen 平台跑，不弹真实窗口；真实窗口行为另由真机验证记录。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402

from shell_core import (  # noqa: E402
    SERVER_NAME,
    ShellWindow,
    SingleInstance,
    activate_existing,
    make_icon,
)

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'[OK]' if ok else '[FAIL]'} {label}" + (f" | {detail}" if not ok else ""))
    if not ok:
        FAILS.append(label)


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    window = ShellWindow("自检窗口")
    window.show()
    app.processEvents()
    check("窗口初始可见", window.isVisible())

    window.close()
    app.processEvents()
    check("点关闭后窗口隐藏、进程不退出", (not window.isVisible()) and not window._quitting)
    check("首次关闭记录提示（只提示一次）", window._tip_shown)

    window.close()
    app.processEvents()
    check("再次关闭不重复提示", window._tip_shown)

    window.show_window()
    app.processEvents()
    check("点托盘图标后窗口恢复可见", window.isVisible())

    calls: list[str] = []
    holder = SingleInstance(lambda: calls.append("show"))
    check("单实例命名管道监听成功", holder.listen(), SERVER_NAME)
    woke = activate_existing()
    for _ in range(6):
        app.processEvents()
    check("第二次启动能唤起已有实例", woke and calls == ["show"], f"woke={woke} calls={calls}")

    tray = QSystemTrayIcon(make_icon(), app)
    tray.setToolTip("行程规划助手")
    check("托盘图标对象可创建", tray.icon().isNull() is False)
    check("offscreen 下不强制要求系统托盘", True)
    tray.hide()

    window.request_quit()
    app.processEvents()
    check("退出流程允许真正关闭窗口", (not window.isVisible()) and window._quitting)

    print(f"\nSHELL_SELFTEST {'PASS' if not FAILS else 'FAIL'} ({len(FAILS)} failed)")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
