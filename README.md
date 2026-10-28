# TikTok 2D Live · 抖音 AI 数字人直播

在本地运行的抖音弹幕互动数字人，支持 **macOS（Apple Silicon）与 Windows 10/11**：

**抖音弹幕 → DeepSeek 生成回复（带人设+记忆+昵称）→ 本地 TTS → Live2D 口型 → OBS/直播伴侣推流**

重型渲染全部在本地浏览器完成（Live2D WebGL），**不需要 CUDA、不需要云端渲染**，一台 Mac Mini/M4 或一台普通 Windows 主机即可 24 小时运行。

## 架构

```
抖音直播间 ──弹幕──▶ douyinLive(:1089, Go) ──▶ reader.py ──▶ 数字人桥 bridge(:8000)
                                                                   │
                                         ┌─────────────────────────┤
                                         ▼                         ▼
                              DeepSeek(带8轮记忆+昵称)        本地 TTS(macOS say)
                                         │                         │
                                         └──────▶ 回复文本+音频 ──────┘
                                                       │
                                       浏览器 Live2D 播放器(avatar.html)
                                       ├ 音频振幅 → 驱动 ParamMouthOpenY 口型
                                       ├ 弹幕飘屏 + 字幕
                                       └ OBS 浏览器源捕获 → 虚拟摄像头 → 抖音直播伴侣
```

## 特性

- **抖音实时弹幕**：douyinLive（Go，本地签名）+ cyber-xiaoya-danmaku 转发
- **DeepSeek 回复**：可配人设提示词，带最近 8 轮对话记忆 + 观众昵称点名
- **跨平台本地 TTS**：macOS 用自带 `say`，Windows 用系统 SAPI5（均零安装）；装了 Piper 则自动优先 Piper
- **Live2D Cubism 4**：官方示例模型 Hiyori，音频实时驱动口型，内置自动眨眼/呼吸/头发物理
- **弹幕飘屏** + 回复字幕
- **OBS 浏览器源**接入，无需推流码

## 目录结构

```
├── bridge.py            # 数字人桥：/api/danmaku 收弹幕 → DeepSeek → TTS → 状态/音频服务
├── tts_backend.py       # 跨平台 TTS：piper / macOS say / Windows sapi，含诊断 CLI
├── verify_pipeline.py   # 最小验证脚本（一次性走通链路 + 延迟统计）
├── avatar.html          # Live2D 播放器（渲染 + 口型 + 弹幕飘屏 + 字幕）
├── setup.sh             # macOS 一键部署（下载运行库/模型/弹幕依赖）
├── setup.ps1            # Windows 一键部署（同上，支持 -DryRun 预演）
├── setup_piper.sh       # 可选：安装本地 Piper TTS（音质好，仅 macOS/Linux）
├── requirements.txt     # Python 依赖（requests / python-dotenv）
├── tools/
│   └── win_sapi_tts.ps1 #   Windows SAPI5 合成助手（供 tts_backend 调用）
├── config/
│   ├── config.example.yaml   # 主配置模板
│   └── persona_prompt.md     # DeepSeek 人设提示词
├── patches/winsound.py       # macOS 兼容补丁（仅 macOS 需要，见下）
├── live2d/                   # Live2D 模型与运行库（由 setup 脚本下载）
│   ├── pixi.min.js           #   PixiJS v6
│   ├── live2dcubismcore.min.js  # Live2D Cubism Core
│   ├── cubism4.min.js        #   pixi-live2d-display
│   └── Hiyori/               #   官方示例模型
├── 部署指南.md               # ⚠️ 已过期：上一代 3D + Vidu 云渲染方案
└── 风险与备选说明.md          # ⚠️ 已过期：同上
```

## 快速开始

### 0. 一键部署（推荐）

**macOS / Linux**

```bash
bash setup.sh
```

**Windows（PowerShell，建议先加 `-DryRun` 预演一遍）**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 -DryRun
powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
```

两个脚本做同样的事：下载 Live2D 运行库（PixiJS + Cubism Core + 渲染库）、下载 8 个官方模型、克隆并编译弹幕采集依赖、生成 `.env`。均支持重复执行，不会覆盖已有的 `.env`。

> 模型约 24MB、不在本仓库内，由脚本从官方仓库拉取；嫌慢也可手动只下你需要的几个（见 §换 Live2D 模型）。

### 1. 环境

| 依赖 | macOS | Windows |
|---|---|---|
| 系统 | macOS（Apple Silicon） | Windows 10 / 11 |
| Python | 3.10+（conda `usually` 实测） | 3.10+（安装时勾选 Add to PATH） |
| Python 包 | `pip install -r requirements.txt` | 同左 |
| Go 1.20+ | 编译 douyinLive 用 | 同左（仅接真实抖音弹幕时需要） |
| TTS | 系统自带 `say` | 系统自带 SAPI5，无需安装 |
| 推流 | OBS（Apple Silicon 版）+ 抖音直播伴侣 | OBS + 抖音直播伴侣 |

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env，填入：
#   DEEPSEEK_API_KEY      DeepSeek 平台申请的 Key
#   （可选）PERSONA_PROMPT  自定义人设，默认已内置
```

Windows 上 `setup.ps1` 已自动完成这一步。

### 3. 启动数字人桥

**macOS**

```bash
source /opt/anaconda3/etc/profile.d/conda.sh
conda activate usually          # 或你自己的 Python 环境
python bridge.py
```

**Windows**

```powershell
# 先自检 TTS 后端与音色，应打印 "生效后端 : sapi" 和中文音色
python tts_backend.py --list-voices

$env:PYTHONUTF8=1
$env:PYTHONIOENCODING="utf-8:replace"
python bridge.py
```

> Windows 上设置这两个环境变量，是为了避免第三方转发器 `reader.py` 打印 emoji 时因 GBK 代码页抛 `UnicodeEncodeError`。

打开 http://127.0.0.1:8000/ 即可看到 Live2D 虚拟人。

此时可先用 curl 模拟弹幕验证：

```bash
curl -X POST http://127.0.0.1:8000/api/danmaku \
  -H 'Content-Type: application/json' \
  -d '{"nick":"测试","content":"主播你好"}'
```

> Windows PowerShell 里 `curl` 是 `Invoke-WebRequest` 的别名，请用 `curl.exe` 或改用：
> ```powershell
> Invoke-RestMethod -Uri http://127.0.0.1:8000/api/danmaku -Method Post `
>   -ContentType "application/json" -Body '{"nick":"测试","content":"主播你好"}'
> ```

### 4. 接真实抖音弹幕（第三方依赖）

弹幕采集需要两个第三方仓库（各自独立 clone，不在本仓库内）：

```bash
# ① 采集服务（Go）
git clone --depth 1 https://github.com/jwwsjlm/douyinLive.git
cd douyinLive && go build -o douyinLive ./cmd/main && cd ..

# ② 弹幕转发（Python）
git clone https://github.com/cyber-xiaoya/cyber-xiaoya-danmaku.git
```

**macOS 兼容补丁**（否则 reader.py 会因 `import winsound` 失败）：

```bash
cp patches/winsound.py cyber-xiaoya-danmaku/winsound.py
```

> ⚠️ **Windows 上不要做这一步**。`winsound` 是 Windows 标准库模块，把同名存根放进
> `cyber-xiaoya-danmaku/` 会遮蔽真正的模块。`setup.ps1` 检测到该文件会自动删除。

**douyinLive 配置**（`douyinLive/config.yaml`）：

```yaml
port: "1089"
cookie:
  use_stored: true
  douyin: ""          # 可选，填抖音登录 Cookie 更稳定
```

**reader.py 配置**（`cyber-xiaoya-danmaku/config.json`，可复制 `config.example.json` 改）：

```json
{
  "douyin": { "enabled": true, "room_id": "你的直播间号", "ws_base": "ws://127.0.0.1:1089" },
  "tts": { "output": "dh", "dh_url": "http://127.0.0.1:8000" }
}
```

**启动顺序**：

```bash
# macOS / Linux
./douyinLive/douyinLive --config ./douyinLive/config.yaml     # ① 采集服务
python bridge.py                                             # ② 数字人桥
cd cyber-xiaoya-danmaku && python reader.py                  # ③ 弹幕转发
```

```powershell
# Windows（注意产物是 .exe）
.\douyinLive\douyinLive.exe --config .\douyinLive\config.yaml  # ① 采集服务
python bridge.py                                              # ② 数字人桥
cd cyber-xiaoya-danmaku; python reader.py                     # ③ 弹幕转发
```

### 5. OBS / 抖音直播伴侣

1. OBS → 来源 `+` → 「浏览器」→ URL `http://127.0.0.1:8000/`（勾选「控制音频」）
2. OBS「工具 → 虚拟摄像机」→ 启动
3. 抖音直播伴侣 → 添加素材 → 「摄像头」→ 选 `OBS Virtual Camera`

> 声音回传需要虚拟声卡：
> - **macOS**：BlackHole（`brew install blackhole-2ch`）
> - **Windows**：VB-Audio Virtual Cable（装完在 OBS「音频 → 高级音频属性」把浏览器源输出到 CABLE Input）

## 配置速查

### 本地 TTS：Piper（推荐，音质好）

```bash
bash setup_piper.sh   # 安装 piper-tts + 下载音色(小雅) + g2pw 模型(~280MB)
```

> 脚本会自动把 `TTS_BACKEND=piper` 写入 `.env`。嫌模型大可继续用系统原生后端
> （`TTS_BACKEND=auto` 即可，macOS 走 `say`、Windows 走 `sapi`）。
>
> **Windows 暂无对应脚本**，需手动安装（Python 3.10+）：
>
> ```powershell
> python -m pip install "piper-tts[zh]" unicode-rbnf
> # 再按 setup_piper.sh 里的地址下载 zh_CN-xiao_ya-medium.onnx 与 g2pw，
> # 放进 piper_voices/ 目录。装好后 TTS_BACKEND=auto 会自动优先用 Piper。
> ```

| 变量 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API Key（必填） |
| `DEEPSEEK_MODEL` | `deepseek-chat`（快）/ `deepseek-reasoner`（慢） |
| `DEEPSEEK_MAX_TOKENS` / `DEEPSEEK_TEMPERATURE` | 回复长度 / 随机性 |
| `HISTORY_TURNS` | 记忆轮数（默认 8） |
| `TTS_BACKEND` | `auto`（默认，按平台自动选）/ `piper` / `say`（macOS）/ `sapi`（Windows）/ `vidu`（云端对口型） |
| `SAY_VOICE` | macOS `say` 音色（默认 `Tingting`） |
| `SAY_VOICE_MAP` | 弹幕关键词→macOS 音色（JSON） |
| `WIN_VOICE` | Windows SAPI 音色，留空自动选第一个中文音色 |
| `WIN_VOICE_MAP` | 弹幕关键词→Windows 音色（JSON） |
| `SAPI_TTS_MAX_CHARS` | 单条朗读文本上限（默认 800） |
| `BRIDGE_HOST` / `BRIDGE_PORT` | 桥监听地址/端口 |
| `BRIDGE_MAX_BODY` | 请求体上限字节数（默认 8192） |
| `BRIDGE_TOKEN` | **只读**视图令牌；绑非回环地址时必填且 ≥16 位 |
| `BRIDGE_ALLOW_UNAUTHENTICATED` | 绑非回环地址且不设令牌时，显式接受「局域网可只读访问」 |

### 局域网访问与安全边界

默认只绑 `127.0.0.1`，本机以外无法访问。如果需要从手机/平板看虚拟人页面，把
`BRIDGE_HOST` 改成 `0.0.0.0` 并设置 `BRIDGE_TOKEN`（至少 16 位、只能是 ASCII），
然后用 `http://<本机IP>:8000/?token=<令牌>` 打开。

令牌请用随机串生成，别手打——手打的短词长度够了但熵极低，16 位要求形同虚设：

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

边界说清楚，避免误判防护强度：

- **写入口 `/api/danmaku` 只允许回环访问**，远程一律 403。因此无论令牌是否泄漏，
  别人都无法替你消耗 DeepSeek 额度。
- 令牌只保护**读**（页面 / 状态 / 音频 / 视频）。它随 URL 出现，可能进入浏览器
  历史或转发出去——所以刻意不赋予它写权限，泄漏的最坏后果只是「别人看到你的弹幕」。
- 页面响应带 `Referrer-Policy: no-referrer`，避免带 `?token=` 的 URL 通过 Referer
  泄漏给外部资源。
- **不支持反向代理**。Host 校验会拒绝 `evil.example.com` 这类域名，所以请勿把本服务
  挂在 Nginx / Caddy 后面用域名访问，否则会 403。这层校验只防 DNS rebinding，
  不是鉴权。

### 排查 TTS 问题

```bash
python tts_backend.py                 # 打印平台、生效后端、默认音色
python tts_backend.py --list-voices   # 列出本机可用音色
python tts_backend.py --say "你好呀"   # 合成一段试听，输出 wav 路径
```

## Windows 支持说明

本项目原为 macOS 单平台，现已支持 Windows。与 macOS 的行为差异：

| 项目 | macOS | Windows |
|---|---|---|
| TTS 后端 | `say`（系统自带） | `sapi`（PowerShell + System.Speech，系统自带） |
| 默认音色 | `Tingting` | 自动选第一个中文音色（通常是 `Microsoft Huihui Desktop`） |
| 音色配置 | `SAY_VOICE` / `SAY_VOICE_MAP` | `WIN_VOICE` / `WIN_VOICE_MAP` |
| 一键部署 | `bash setup.sh` | `powershell -File setup.ps1` |
| 弹幕采集器产物 | `douyinLive` | `douyinLive.exe` |
| `winsound` 补丁 | 需要（`patches/winsound.py`） | **不能打**，会遮蔽标准库 |
| 虚拟声卡 | BlackHole | VB-Audio Virtual Cable |
| 单条 TTS 延迟 | 约 0.2~0.5s | 约 0.7~1.0s（PowerShell 冷启动开销） |

### Windows 常见问题

| 现象 | 原因 | 处置 |
|---|---|---|
| 启动报 `TTS_BACKEND=say 仅 macOS 可用` | `.env` 里写死了 `say` | 改成 `auto` 或 `sapi` |
| 合成无声 / 日志有 `voice not installed` | 音色名写错 | `python tts_backend.py --list-voices` 查准确名字 |
| 控制台中文乱码 | 代码页不是 UTF-8 | 启动前设 `$env:PYTHONUTF8=1` |
| `reader.py` 抛 `UnicodeEncodeError` | emoji 无法用 GBK 编码 | 设 `$env:PYTHONIOENCODING="utf-8:replace"` |
| `setup.ps1` 报中文乱码或语法错误 | 文件被存成无 BOM 的 UTF-8 | 用编辑器另存为「UTF-8 with BOM」 |
| 浏览器能开但收不到弹幕 | 转发器配置未生成 | 检查 `cyber-xiaoya-danmaku/config.json` 的 `douyin.room_id` |

## 换 Live2D 模型

官方还提供 Haru / Mao / Natori / Ren / Rice 等模型。把模型目录放进 `live2d/`，改 `avatar.html` 里一处路径即可：

```js
PIXI.live2d.Live2DModel.from("/live2d/你的模型/model3.json", ...)
```

## 许可与致谢

- 本项目代码：MIT
- Live2D 模型（Hiyori）：Live2D Inc. 官方示例模型，遵循 Live2D 许可
- PixiJS：MIT · pixi-live2d-display：MIT · Live2D Cubism Core：Live2D Inc.
- 弹幕采集依赖：[douyinLive](https://github.com/jwwsjlm/douyinLive)、[cyber-xiaoya-danmaku](https://github.com/cyber-xiaoya/cyber-xiaoya-danmaku)（各自遵循其许可证）

> ⚠️ 请遵守各平台用户协议，控制抓取频率，勿用于违规用途。
