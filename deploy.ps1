# =============================================================================
# 一键把本项目推送到独立的 GitHub 仓库（中文提示，全程不回显你的 Token）
#
# 用法（在项目根目录打开 PowerShell）：
#     .\deploy.ps1                       # 默认仓库名 travel-agent（私有）
#     .\deploy.ps1 -RepoName travel-agent-cn -Public
#
# 前置条件：本机已安装 git；以及一个 GitHub「个人访问令牌（PAT）」
#   生成方式：GitHub → 右上角头像 → Settings → Developer settings
#             → Personal access tokens → Tokens (classic) → Generate new token
#             → 勾选 repo 权限 → 生成后立刻复制（只显示一次）
#
# 脚本不会把 Token 写进 .git/config，推送时只在内存里用一次。
# =============================================================================
param(
    [string]$RepoName = "travel-agent",
    [string]$UserName = "Dwy0418",
    [switch]$Public
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function 说([string]$文本) { Write-Host $文本 -ForegroundColor Cyan }
function 好([string]$文本) { Write-Host $文本 -ForegroundColor Green }
function 坏([string]$文本) { Write-Host $文本 -ForegroundColor Red }

$根目录 = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $根目录

# ---------- 1. 环境检查 ----------
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    坏 "没有找到 git，请先安装 Git for Windows：https://git-scm.com/download/win"
    exit 1
}

# ---------- 2. 取 Token ----------
说 "请粘贴 GitHub 个人访问令牌（PAT，输入时不显示）："
$安全串 = Read-Host -AsSecureString
$Token = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($安全串)
)
if ([string]::IsNullOrWhiteSpace($Token)) {
    坏 "没有输入令牌，已退出。"
    exit 1
}

$请求头 = @{ Authorization = "token $Token"; Accept = "application/vnd.github+json"; "User-Agent" = "travel-agent-deploy" }

# ---------- 3. 校验身份 ----------
try {
    $我 = Invoke-RestMethod -Uri "https://api.github.com/user" -Headers $请求头 -TimeoutSec 20
    好 "令牌有效，账号：$($我.login)"
    $UserName = $我.login
} catch {
    坏 "令牌无效或网络不通：$($_.Exception.Message)"
    坏 "提示：PAT 需要勾选 repo 权限；若开了双重验证，请用令牌而不是密码。"
    exit 1
}

# ---------- 4. 建仓库（已存在则复用） ----------
$已有 = $null
try {
    $已有 = Invoke-RestMethod -Uri "https://api.github.com/repos/$UserName/$RepoName" -Headers $请求头 -TimeoutSec 20
} catch {
    $已有 = $null
}

if ($已有) {
    说 "仓库已存在，直接复用：$($已有.html_url)"
} else {
    $体 = @{
        name        = $RepoName
        description = "国内旅行规划 Agent（DeepSeek + 高德 + Open-Meteo，代码级校验闭环）"
        private     = (-not $Public)
        has_issues  = $true
    } | ConvertTo-Json
    $新仓 = Invoke-RestMethod -Method Post -Uri "https://api.github.com/user/repos" `
        -Headers $请求头 -Body $体 -ContentType "application/json" -TimeoutSec 30
    好 "已创建仓库：$($新仓.html_url)"
}

# ---------- 5. 初始化本地仓库并提交 ----------
if (-not (Test-Path (Join-Path $根目录 ".git"))) {
    说 "初始化本地 Git 仓库（分支 main）……"
    git init -b main | Out-Null
}

git add -A | Out-Null
$待提交 = git status --porcelain
if ($待提交) {
    git -c user.name="$UserName" -c user.email="$UserName@users.noreply.github.com" `
        commit -m "部署：国内旅行规划 Agent（Streamlit 云端版）" | Out-Null
    好 "已提交本地改动。"
} else {
    说 "没有新的改动需要提交。"
}

# ---------- 6. 推送 ----------
$干净地址 = "https://github.com/$UserName/$RepoName.git"
git remote remove origin 2>$null | Out-Null
git remote add origin $干净地址 | Out-Null

说 "推送到 GitHub……"
$临时地址 = "https://$UserName`:$Token@github.com/$UserName/$RepoName.git"
git push $临时地址 "main:main" --force 2>&1 | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) {
    坏 "推送失败。常见原因：令牌没有 repo 权限 / 仓库名冲突 / 网络被墙。"
    exit 1
}

git branch --set-upstream-to=origin/main main 2>$null | Out-Null
$Token = $null   # 用完即丢，不落盘
好 "推送完成：$干净地址"

# ---------- 7. 打印下一步 ----------
Write-Host ""
说 "接下来在浏览器里完成最后 2 步（Streamlit Community Cloud）："
Write-Host "  1) 打开 https://share.streamlit.io ，用 GitHub 登录 → Create app"
Write-Host "     Repository: $UserName/$RepoName    Branch: main    Main file path: app.py"
Write-Host "  2) 进入 App → Settings → Secrets，把下面内容填上真实 Key 后 Save："
Write-Host ""
Write-Host '     DEEPSEEK_API_KEY = "sk-你的Key"' -ForegroundColor DarkGray
Write-Host '     AMAP_KEY = "你的高德Web服务Key"' -ForegroundColor DarkGray
Write-Host ""
说 "保存后云端会自动重启，网址形如 https://<你的子域名>.streamlit.app"
说 "详细图文说明见 部署指南.md"
