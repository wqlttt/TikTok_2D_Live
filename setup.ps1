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
function Fail($msg) {
    # 同时写成功流：PS 5.1 下 Write-Host 后紧跟 exit，输出被重定向时最后一笔
    # 会来不及刷出，表现为「退出码 1 但没有任何错误信息」的静默失败。
    Write-Host "ERROR: $msg" -ForegroundColor Red
    Write-Output "ERROR: $msg"
    exit 1
}

function Write-Utf8NoBom {
    param([string] $Path, [string] $Text)
    # .NET 的相对路径按「进程当前目录」解析，而 PowerShell 的 Set-Location 只改
    # 自己的 location，两者可能不一致（ISE、部分宿主、快捷方式启动时都会分叉）。
    # 统一以 $Root 为基准转成绝对路径，避免写到别处或报「找不到路径的一部分」。
    if (-not [System.IO.Path]::IsPathRooted($Path)) {
        $Path = Join-Path $Root $Path
    }
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Invoke-Native {
    <#
      执行外部命令并返回退出码。

      为什么需要这个包装：Windows PowerShell 5.1 在
      $ErrorActionPreference = "Stop" 时，会把原生命令写入 stderr 的内容包装成
      ErrorRecord 并当作**终止错误**抛出。而 git 的进度信息 "Cloning into ..."
      恰好写在 stderr —— 于是 `git clone ... 2>&1 | Out-Null` 会在克隆完全成功的
      情况下也把脚本打断（实测抛 RemoteException）。go build 同理。

      这里把 EAP 局部降级为 Continue，改用 $LASTEXITCODE 判断成败。
      -Quiet 用于丢弃 git 的进度噪声；不传则保留 stderr 便于排查失败原因。
    #>
    param(
        [scriptblock] $Command,
        [switch] $Quiet
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($Quiet) { & $Command 2>$null } else { & $Command }
        return $LASTEXITCODE
    } catch {
        # 兜底：任何漏网的终止错误都转成非零退出码，绝不让脚本无声中断
        Write-Host ("WARN: 外部命令执行异常：" + $_.Exception.Message) -ForegroundColor Yellow
        return 1
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Get-NativeOutput {
    <# 执行外部命令并只返回 stdout，隔离 stderr 与 EAP 终止错误。 #>
    param([scriptblock] $Command)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        return (& $Command 2>$null)
    } finally {
        $ErrorActionPreference = $prev
    }
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

$pyVer = Get-NativeOutput { & $PyCmd @PyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])" }
if (-not $pyVer) { Fail "Python 无法运行，请检查安装" }
$pyParts = "$pyVer".Trim().Split(".")
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
Info "安装 Python 依赖（requirements.txt）"
if ($DryRun) {
    Write-Host "    (dry-run) pip install -r requirements.txt" -ForegroundColor DarkGray
} else {
    $pipRc = Invoke-Native { & $PyCmd @PyArgs -m pip install -q --disable-pip-version-check -r requirements.txt }
    if ($pipRc -ne 0) { Warn "pip 安装失败，请手动执行：python -m pip install -r requirements.txt" }
}

# ---------------------------------------------------------------- 3. Live2D 运行库
Info "下载 Live2D 运行库 (PixiJS + Cubism Core + 渲染库)"
$libs = [ordered]@{
    "live2d/pixi.min.js"             = "https://cdn.jsdelivr.net/npm/pixi.js@6.5.10/dist/browser/pixi.min.js"
    "live2d/cubism4.min.js"          = "https://cdn.jsdelivr.net/npm/pixi-live2d-display@0.4.0/dist/cubism4.min.js"
    "live2d/live2dcubismcore.min.js" = "https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js"
}
$libFail = 0
foreach ($rel in $libs.Keys) {
    if ($DryRun) {
        Write-Host "    (dry-run) 下载 $rel" -ForegroundColor DarkGray
        continue
    }
    $dest = Join-Path $Root $rel
    New-Item -ItemType Directory -Path (Split-Path -Parent $dest) -Force | Out-Null
    try {
        Invoke-WebRequest -Uri $libs[$rel] -OutFile $dest -UseBasicParsing
        Info "  已下载 $rel"
    } catch {
        # 与脚本其余部分保持一致：单个资源失败只警告，不中断整体部署
        $libFail++
        Warn "下载 $rel 失败：$($_.Exception.Message)"
    }
}
if ($libFail -gt 0) {
    Warn "有 $libFail 个运行库未下载成功，虚拟人页面可能无法加载；重跑本脚本可补齐"
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
    try {
        if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
        $rc = Invoke-Native -Quiet { git clone --depth 1 --filter=blob:none --sparse https://github.com/Live2D/CubismWebSamples.git $tmp }
        if ($rc -ne 0) {
            Warn "克隆 CubismWebSamples 失败（退出码 $rc），请检查网络后重跑本脚本"
        } else {
            Push-Location $tmp
            $paths = $Models | ForEach-Object { "Samples/Resources/$_" }
            $rc = Invoke-Native -Quiet { git sparse-checkout set @paths }
            Pop-Location
            if ($rc -ne 0) { Warn "sparse-checkout 返回 $rc，将只复制已取到的模型" }

            $copied = 0
            foreach ($m in $Models) {
                $src = Join-Path $tmp "Samples/Resources/$m"
                if (Test-Path $src) {
                    Copy-Item -Recurse -Force $src (Join-Path $Root "live2d/$m")
                    $copied++
                } else {
                    Warn "模型 $m 未取到，已跳过"
                }
            }
            if ($copied -eq 0) { Warn "一个模型都没取到，虚拟人页面将无法加载" }
            else { Info "  已就位 $copied 个模型" }
        }
    } catch {
        Warn "模型下载阶段出错：$($_.Exception.Message)"
        Warn "可稍后重跑本脚本，或手动从 https://github.com/Live2D/CubismWebSamples 下载后放入 live2d/"
    } finally {
        if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
    }
}

# ---------------------------------------------------------------- 5. 弹幕采集依赖
Info "准备弹幕采集依赖（接真实抖音弹幕才需要）"

if (-not (Test-Path "douyinLive")) {
    if ($DryRun) {
        Write-Host "    (dry-run) git clone douyinLive + go build" -ForegroundColor DarkGray
    } elseif (-not $HasGit) {
        Warn "跳过 douyinLive：缺少 git"
    } else {
        $rc = Invoke-Native -Quiet { git clone --depth 1 https://github.com/jwwsjlm/douyinLive.git }
        if ($rc -ne 0) { Warn "克隆 douyinLive 失败（退出码 $rc）" }
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
        $buildRc = Invoke-Native { go build -o douyinLive.exe ./cmd/main }
        Pop-Location
        if ($buildRc -eq 0) { Info "  已编译 douyinLive.exe" } else { Warn "douyinLive 编译失败（退出码 $buildRc，需要 Go 1.20+）" }
    } else {
        Warn "  跳过 douyinLive 编译：未安装 Go。装好后执行：cd douyinLive; go build -o douyinLive.exe ./cmd/main"
    }
}

if (-not (Test-Path "cyber-xiaoya-danmaku")) {
    if ($DryRun) {
        Write-Host "    (dry-run) git clone cyber-xiaoya-danmaku" -ForegroundColor DarkGray
    } elseif ($HasGit) {
        $rc = Invoke-Native -Quiet { git clone https://github.com/cyber-xiaoya/cyber-xiaoya-danmaku.git }
        if ($rc -ne 0) { Warn "克隆 cyber-xiaoya-danmaku 失败（退出码 $rc）" }
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
