# Windows 托盘壳（travel-agent desktop shell）

不碰 Streamlit 应用本身：`app.py` 与 `travel_agent/` 的线上行为完全不变，壳只负责
「把网页装进一个原生窗口 + 托盘 + 单实例」。

## 选型

用 **PySide6（Qt 6）+ Qt WebEngine**。

- 本机已安装 PySide6 6.11.2 且带 WebEngine，无需再装额外运行时。
- 自带 `QSystemTrayIcon`、`QLocalServer`、`closeEvent`，关闭拦截、托盘、单实例都能用
  官方组件完成，不依赖 Win32 私有技巧。
- 代价：打包体积大。含 Qt WebEngine 的 onedir 产物大约 250–400 MB；如果只想要轻量，
  可改用 `pywebview + pystray`（产物约 20–40 MB），但需要机器上有 WebView2 运行时。

依赖：`desktop/requirements-desktop.txt` 只有一行 `PySide6>=6.8`。
Web 应用自身的依赖不变。

## 启动

主路径（本地 Streamlit）：

```bat
desktop\run_shell.bat
```

或调试时看控制台：

```powershell
python desktop\tray_shell.py
```

壳会自动挑一个空闲端口，后台执行 `python -m streamlit run app.py`，等端口就绪后用
Qt WebEngine 打开 `http://127.0.0.1:<port>`。

切换为线上地址（不启动本地进程）：

```powershell
set TRAVEL_AGENT_URL=https://<你的部署地址>
python desktop\tray_shell.py
```

## 行为

- **关闭（×）**：`ShellWindow.closeEvent` 里 `event.ignore()` + `hide()`，进程继续驻留。
  第一次关闭时通过托盘气泡提示「已最小化到托盘，右键图标可退出」，之后不再提示。
- **恢复**：左键单击 / 双击托盘图标，或右键菜单「显示主窗口」→ `showNormal() + raise_()
  + activateWindow()`；窗口状态由常驻进程保留，不重开会话。
- **退出**：只有托盘右键菜单「退出」会调用 `quit_all()`：隐藏托盘、允许窗口真正关闭、
  结束本地 Streamlit 子进程、退出 Qt 事件循环。
- **单实例**：启动时先 `QLocalSocket` 连命名管道 `travel-agent-shell-single-instance-v1`；
  连上就发 `show` 后直接退出（exit 0），已有实例收到连接后把窗口唤到前台。连不上才
  `QLocalServer.listen()` 成为主实例。

## 打包 exe

```powershell
powershell -ExecutionPolicy Bypass -File desktop\build_exe.ps1
```

产物在 `dist\TravelAgentShell\TravelAgentShell.exe`（onedir）。脚本会把 `app.py`、
`travel_agent/`、`components/`、`.streamlit/` 和 Streamlit 依赖一起收进去，exe 内用
`streamlit.web.cli.main()` 在线程里启动本地服务。首次打包会下载 PyInstaller，体积较大；
如果打包失败，直接分发 `run_shell.bat` + 项目目录也能用。

## 自检

```powershell
python desktop\selfcheck_shell.py
```

覆盖：初始可见、关闭后隐藏且不退出、只提示一次、恢复可见、单实例唤起、托盘图标对象、
退出流程。真实窗口另用 `WM_CLOSE` 做过真机验证（见提交说明）。
