# 打包成 exe（PyInstaller，onedir）
# 用法：powershell -ExecutionPolicy Bypass -File desktop\build_exe.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here

python -m pip install --upgrade pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pyinstaller 安装失败" }

python -m PyInstaller --noconfirm --clean --windowed --onedir `
  --name TravelAgentShell `
  --add-data "$root\app.py;." `
  --add-data "$root\travel_agent;travel_agent" `
  --add-data "$root\components;components" `
  --add-data "$root\.streamlit;.streamlit" `
  --collect-all streamlit `
  --collect-all altair `
  --collect-all pyarrow `
  --collect-all PySide6 `
  "$here\tray_shell.py"

if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }
Write-Host "产物：$root\dist\TravelAgentShell\TravelAgentShell.exe"
