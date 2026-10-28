<#
.SYNOPSIS
    TikTok 2D Live —— Windows 一键部署脚本（对应 macOS 的 setup.sh）。

.DESCRIPTION
    做四件事：
      1. 安装 Python 依赖（requests / python-dotenv）
      2. 下载 Live2D 运行库（PixiJS + Cubism Core + 渲染库）与 8 个官方示例模型
      3. 准备弹幕采集依赖（douyinLive + cyber-xiaoya-danmaku）
      4. 生成 .env 与转发器配置

    脚本可重复执行：已存在的目录会跳过，不会覆盖你的 .env。

    注意：本文件必须以「UTF-8 带 BOM」保存。Windows PowerShell 5.1 读取
    无 BOM 的 .ps1 时会按当前 ANSI 代码页（简体中文系统为 cp936）解码，
    中文会变乱码，甚至可能破坏语法解析。

.PARAMETER DryRun
    只打印将要执行的动作，不实际下载 / 克隆 / 写文件。用于验证脚本本身。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [switch] $DryRun
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Info($msg) { Write-Host "==> $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "WARN: $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

function Write-Utf8NoBom {
    param([string] $Path, [string] $Text)
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

Write-Host "=============================================="
Write-Host "  TikTok 2D Live · Windows 部署"
Write-Host "=============================================="
if ($DryRun) { Warn "DRY-RUN 模式：只打印动作，不实际执行" }

# ---------------------------------------------------------------- 1. 依赖检查
Info "检查依赖"

$PyCmd = $null
foreach ($cand in @("python", "py")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) { $PyCmd = $cand; break }
}
if (-not $PyCmd) { Fail "未找到 Python，请先安装 Python 3.10+ 并勾选 Add to PATH" }
$PyArgs = @()
if ($PyCmd -eq "py") { $PyArgs = @("-3") }

$pyVer = (& $PyCmd @PyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])") 2>$null
if (-not $pyVer) { Fail "Python 无法运行，请检查安装" }
$pyParts = $pyVer.Split(".")
$pyMajor = [int]$pyParts[0]
$pyMinor = [int]$pyParts[1]
if ($pyMajor -lt 3 -or ($pyMajor -eq 3 -and $pyMinor -lt 8)) {
    Fail "Python $pyVer 版本过低，本项目需要 3.8+（推荐 3.10+）"
}
if ($pyMajor -eq 3 -and $pyMinor -lt 10) {
    Warn "Python $pyVer 可用，但推荐 3.10+：本地 Piper TTS 需要 3.10 以上"
}
Info "Python $pyVer（命令：$PyCmd）"

$HasGit = [bool](Get-Command git -ErrorAction SilentlyContinue)
if (-not $HasGit) { Warn "未找到 git，Live2D 模型与弹幕采集器将无法自动下载" }
$HasGo = [bool](Get-Command go -ErrorAction SilentlyContinue)
if (-not $HasGo) { Warn "未找到 Go，douyinLive 将跳过编译（接真实弹幕前需自行安装 Go）" }

# ---------------------------------------------------------------- 2. Python 依赖
Info "安装 Python 依赖 (requests / python-dotenv)"
if ($DryRun) {
    Write-Host "    (dry-run) pip install requests python-dotenv" -ForegroundColor DarkGray
} else {
    & $PyCmd @PyArgs -m pip install -q --disable-pip-version-check requests python-dotenv
    if ($LASTEXITCODE -ne 0) { Warn "pip 安装失败，请手动执行：python -m pip install requests python-dotenv" }
}

# ---------------------------------------------------------------- 3. Live2D 运行库
Info "下载 Live2D 运行库 (PixiJS + Cubism Core + 渲染库)"
$libs = [ordered]@{
    "live2d/pixi.min.js"             = "https://cdn.jsdelivr.net/npm/pixi.js@6.5.10/dist/browser/pixi.min.js"
    "live2d/cubism4.min.js"          = "https://cdn.jsdelivr.net/npm/pixi-live2d-display@0.4.0/dist/cubism4.min.js"
    "live2d/live2dcubismcore.min.js" = "https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js"
}
foreach ($rel in $libs.Keys) {
    if ($DryRun) {
        Write-Host "    (dry-run) 下载 $rel" -ForegroundColor DarkGray
        continue
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $rel) -Force | Out-Null
    try {
        Invoke-WebRequest -Uri $libs[$rel] -OutFile $rel -UseBasicParsing
        Info "  已下载 $rel"
    } catch {
        Fail "下载 $rel 失败：$($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------- 4. Live2D 模型
Info "下载 Live2D 模型 (8 个官方示例，约 24MB)"
$Models = @("Hiyori", "Haru", "Mao", "Mark", "Natori", "Ren", "Rice", "Wanko")
if ($DryRun) {
    Write-Host "    (dry-run) git sparse-checkout 拉取 $($Models -join ', ')" -ForegroundColor DarkGray
} elseif (-not $HasGit) {
    Warn "跳过模型下载：缺少 git。可手动从 https://github.com/Live2D/CubismWebSamples 下载后放入 live2d/"
} else {
    $tmp = Join-Path $Root ".live2d_tmp"
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    git clone --depth 1 --filter=blob:none --sparse https://github.com/Live2D/CubismWebSamples.git $tmp 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "克隆 CubismWebSamples 失败，请检查网络" }
    Push-Location $tmp
    $paths = $Models | ForEach-Object { "Samples/Resources/$_" }
    git sparse-checkout set @paths 2>&1 | Out-Null
    Pop-Location
    foreach ($m in $Models) {
        $src = Join-Path $tmp "Samples/Resources/$m"
        if (Test-Path $src) {
            Copy-Item -Recurse -Force $src (Join-Path $Root "live2d/$m")
        } else {
            Warn "模型 $m 未取到，已跳过"
        }
    }
    Remove-Item -Recurse -Force $tmp
}

# ---------------------------------------------------------------- 5. 弹幕采集依赖
Info "准备弹幕采集依赖（接真实抖音弹幕才需要）"

if (-not (Test-Path "douyinLive")) {
    if ($DryRun) {
        Write-Host "    (dry-run) git clone douyinLive + go build" -ForegroundColor DarkGray
    } elseif (-not $HasGit) {
        Warn "跳过 douyinLive：缺少 git"
    } else {
        git clone --depth 1 https://github.com/jwwsjlm/douyinLive.git 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { Warn "克隆 douyinLive 失败" }
    }
}

if (Test-Path "douyinLive/config.example.yaml") {
    if (-not $DryRun -and -not (Test-Path "douyinLive/config.yaml")) {
        $yaml = Get-Content "douyinLive/config.example.yaml" -Raw -Encoding UTF8
        $yaml = $yaml -replace 'port:\s*"1088"', 'port: "1089"'
        Write-Utf8NoBom "douyinLive/config.yaml" $yaml
        Info "  已生成 douyinLive/config.yaml（端口 1089）"
    }
}

if ((Test-Path "douyinLive") -and -not (Test-Path "douyinLive/douyinLive.exe")) {
    if ($DryRun) {
        Write-Host "    (dry-run) go build -o douyinLive.exe ./cmd/main" -ForegroundColor DarkGray
    } elseif ($HasGo) {
        Push-Location "douyinLive"
        go build -o douyinLive.exe ./cmd/main
        $buildRc = $LASTEXITCODE
        Pop-Location
        if ($buildRc -eq 0) { Info "  已编译 douyinLive.exe" } else { Warn "douyinLive 编译失败（需要 Go 1.20+）" }
    } else {
        Warn "  跳过 douyinLive 编译：未安装 Go。装好后执行：cd douyinLive; go build -o douyinLive.exe ./cmd/main"
    }
}

if (-not (Test-Path "cyber-xiaoya-danmaku")) {
    if ($DryRun) {
        Write-Host "    (dry-run) git clone cyber-xiaoya-danmaku" -ForegroundColor DarkGray
    } elseif ($HasGit) {
        git clone https://github.com/cyber-xiaoya/cyber-xiaoya-danmaku.git 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { Warn "克隆 cyber-xiaoya-danmaku 失败" }
    }
}

# Windows 上必须删掉 macOS 用的 winsound 存根：
# 本地同名文件会遮蔽标准库 winsound，导致真正的 PlaySound 变成 no-op。
$stub = "cyber-xiaoya-danmaku/winsound.py"
if (Test-Path $stub) {
    if ($DryRun) {
        Write-Host "    (dry-run) 移除 winsound.py 存根" -ForegroundColor DarkGray
    } else {
        Remove-Item -Force $stub
        Warn "已移除 cyber-xiaoya-danmaku/winsound.py 存根（Windows 用系统自带 winsound）"
    }
}

# 生成转发器配置：只启用抖音，回传本机数字人桥
$cfgExample = "cyber-xiaoya-danmaku/config.example.json"
if ((Test-Path $cfgExample) -and -not (Test-Path "cyber-xiaoya-danmaku/config.json")) {
    if ($DryRun) {
        Write-Host "    (dry-run) 生成 cyber-xiaoya-danmaku/config.json" -ForegroundColor DarkGray
    } else {
        $c = Get-Content $cfgExample -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($c.PSObject.Properties.Name -contains "tts" -and $null -ne $c.tts) {
            $c.tts | Add-Member -NotePropertyName dh_url -NotePropertyValue "http://127.0.0.1:8000" -Force
        }
        foreach ($k in @("bilibili", "wechat")) {
            if (($c.PSObject.Properties.Name -contains $k) -and ($null -ne $c.$k)) {
                $c.$k | Add-Member -NotePropertyName enabled -NotePropertyValue $false -Force
            }
        }
        if (($c.PSObject.Properties.Name -contains "douyin") -and ($null -ne $c.douyin)) {
            $c.douyin | Add-Member -NotePropertyName enabled -NotePropertyValue $true -Force
        }
        Write-Utf8NoBom "cyber-xiaoya-danmaku/config.json" ($c | ConvertTo-Json -Depth 20)
        Info "  已生成 cyber-xiaoya-danmaku/config.json（只启用抖音）"
    }
}

# ---------------------------------------------------------------- 6. .env
Info "生成 .env"
if (Test-Path ".env") {
    Info "  .env 已存在，保留不动"
} elseif ($DryRun) {
    Write-Host "    (dry-run) 复制 .env.example -> .env" -ForegroundColor DarkGray
} else {
    Copy-Item ".env.example" ".env"
    Info "  已从 .env.example 生成 .env"
}

# ---------------------------------------------------------------- 完成
Write-Host @"

==============================================
  部署完成
==============================================

接下来手动做两件事：
  1) 编辑 .env 填入 DEEPSEEK_API_KEY
  2) 接真实抖音弹幕时，编辑 cyber-xiaoya-danmaku/config.json 填 douyin.room_id

自检 TTS（应打印生效后端与音色列表）：
  python tts_backend.py --list-voices

启动数字人桥：
  `$env:PYTHONUTF8=1; `$env:PYTHONIOENCODING="utf-8:replace"; python bridge.py
  然后打开 http://127.0.0.1:8000/

详细说明见 README.md 的「Windows」章节。
"@
