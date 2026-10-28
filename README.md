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
- **礼物互动（最小闭环）**：礼物事件 → 规则导演 → Live2D 动作 / 特效 / 礼物浮层；支持优先级、冷却、打断、排队
- **大礼物纯特效演出**：使用开源 [canvas-confetti](https://github.com/catdad/canvas-confetti) 做烟花/彩带/玫瑰雨，不占用 Live2D 动作组
- **弹幕飘屏** + 回复字幕
- **OBS 浏览器源**接入，无需推流码

## 目录结构

```
├── bridge.py            # 数字人桥：/api/danmaku 收弹幕 → DeepSeek → TTS → 状态/音频服务
├── director.py          # 礼物导演：规则匹配 / 优先级 / 冷却 / 打断 / 队列
├── tts_backend.py       # 跨平台 TTS：piper / macOS say / Windows sapi，含诊断 CLI
├── inspect_live2d.py    # 查看模型的动作组/下标/表情
├── verify_pipeline.py   # 最小验证脚本（一次性走通链路 + 延迟统计）
├── avatar.html          # Live2D 播放器（渲染 + 口型 + 弹幕飘屏 + 字幕）
├── setup.sh             # macOS 一键部署（下载运行库/模型/弹幕依赖）
├── setup.ps1            # Windows 一键部署（同上，支持 -DryRun 预演）
├── setup_piper.sh       # 可选：安装本地 Piper TTS（音质好，仅 macOS/Linux）
├── requirements.txt     # Python 依赖（requests / python-dotenv）
├── tools/
│   └── win_sapi_tts.ps1 #   Windows SAPI5 合成助手（供 tts_backend 调用）
├── static/
│   ├── gift-effects.js  # 礼物特效（优先 canvas-confetti，CSS 兜底）
│   ├── gift-effects.css
│   ├── gift-player.js   # ActionPlan 播放器（时间轴 → motion/effect/video/toast）
│   ├── videos/          # 大礼物预渲染视频（可选）
│   └── vendor/
│       ├── confetti.browser.js    # canvas-confetti 1.9.3
│       └── canvas-confetti.LICENSE
├── config/
│   ├── config.example.yaml   # 主配置模板
│   ├── gift_actions.json     # 礼物 → 演出规则
│   └── persona_prompt.md     # DeepSeek 人设提示词
├── tests/
│   ├── test_bridge.py        # 桥的单元测试
│   ├── test_director.py      # 导演规则单测
│   ├── smoke_bridge.py       # 跨平台冒烟测试（无需 API Key）
│   └── access_control.py     # 访问控制矩阵测试
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

## 礼物互动（最小闭环）

礼物和普通弹幕走**两条并行链路**，这是关键：

```text
礼物事件
  ├─→ GiftDirector（立即）→ ActionPlan → avatar.html 播动作/特效/浮层
  └─→ DeepSeek + TTS（异步）→ 台词音频 → 字幕 + 口型
```

也就是说：礼物到达先让宠物“有反应”，不等 DeepSeek/TTS；感谢话术随后再由现有链路补上。

### 测试一条礼物

```bash
curl -X POST http://127.0.0.1:8000/api/danmaku \
  -H 'Content-Type: application/json' \
  -d '{"kind":"gift","nick":"测试观众","gift_item":"玫瑰","gift_num":1,"gift_value":1}'
```

`/api/state` 会多出：

- `actions`：最近动作列表
- `action_seq`：动作序号，前端只播新动作
- `director`：当前演出 / 队列长度 / 规则数

### 规则配置

编辑 [`config/gift_actions.json`](config/gift_actions.json)，默认有 6 条：

| 规则 | 匹配 | 效果 |
|---|---|---|
| `large_gift` | `value >= 100` | canvas-confetti 烟花 + 彩带 + 浮层，5 秒，不可打断；**不使用 Live2D 动作组** |
| `combo_10` | `combo >= 10` | 玫瑰雨 + 连击浮层 |
| `medium_gift` | `10 <= value <= 99` | 爱心 + 组合动作 |
| `heart_gift` | 名字含“小心心/爱心”且 `value <= 9` | 爱心 + 专属浮层 |
| `small_gift` | `value <= 9` | 星星 + 微反应 |
| `fallback` | 兜底 | 星星 + 感谢浮层 |

匹配按 `priority` 从高到低；`cooldown` / `user_cooldown` 控制频率；`interruptible` 控制能否被更大礼物打断。

**礼物不会因冷却被丢掉**：

- `user_cooldown` 命中（同一人刷屏）→ 跳过该规则，尝试下一条匹配规则
- `cooldown` 命中（同类演出刚播过）→ 看 `on_cooldown`：
  - `fallthrough`（默认）：降级到下一条匹配规则，最终落到 `fallback`
  - `queue`：仍用本规则，排队等当前演出结束（`large_gift` 用这个，保证每个大礼物都有完整演出）
- 排队动作超过 `queue_ttl` 秒（默认 10，大礼物 60）仍没轮到则丢弃，避免过时的感谢
- 请求里带 `event_id` 时按它去重（抖音连击会重复推送同一事件）

`/api/state?since=<action_seq>` 只返回该序号之后的动作，前端轮询用它减小负载。

`match` 支持：`names` / `exclude_names`（礼物名包含匹配）、`min_value` / `max_value`、`min_count` / `max_count`、`min_combo` / `max_combo`。

### 大礼物为什么不占 Live2D 动作组

大礼物用的是 `canvas-confetti` 的粒子特效，画布固定覆盖整个 OBS 浏览器源，不新增 Live2D motion。这样做的好处：

- 不需要为每个大礼物做一套 Live2D 动作，素材成本低
- 烟花/彩带/玫瑰雨的表现力比单个骨骼动作更强
- 大礼物播放时，Live2D 仍可继续待机/眨眼，不会冻结 5 秒
- 想换成预渲染视频时，时间轴直接加 `video` step，不需要改导演

当前大礼物的 `timeline` 只有 `effect` + `toast`，没有 `motion`；`GiftPlayer` 只有在时间轴包含 `motion` 时才暂停程序化待机手势。

大礼物也可以改成视频：

```json
{
  "at": 0,
  "type": "video",
  "src": "/static/videos/large_gift.webm",
  "duration": 5.0,
  "audio": false,
  "fit": "contain",
  "loop": false
}
```

视频放 [static/videos/](TikTok_2D_Live/static/videos/)，建议用 WebM + VP9 alpha 做透明背景；不透明视频用 MP4/H.264 也可以。`audio: false` 时静音，避免和主播/TTS 抢声音。

### ActionPlan 时间轴

导演输出的是与渲染器无关的 JSON：

```json
{
  "duration": 3.2,
  "timeline": [
    {"at": 0, "type": "motion", "group": "TapBody", "index": 0},
    {"at": 0.1, "type": "effect", "name": "hearts", "duration": 2.2},
    {"at": 0.2, "type": "toast", "text": "谢谢 {nick} 的 {gift}！", "duration": 2.6}
  ]
}
```

`at` 是相对动作开始的秒数，步骤可以并行，所以“复杂活动”可以写成一条时间轴，而不是一串 `if/else`。

### 扩展点

- **加礼物规则**：只改 `config/gift_actions.json`，不改 Python。
- **加特效**：优先在 `static/gift-effects.js` 的 `spawnWithConfetti()` 里加一个 `case`；需要离线兜底再补 `spawn()` 的 CSS 分支。
- **加步骤类型**：在 `static/gift-player.js` 的 `_exec()` 里加一个 `case`；未知类型会派发 `gift:step` 事件，方便外挂。
- **换视频演出**：把 `effect` step 换成 `video` step，文件放 `static/videos/`，支持 `fit` / `loop` / `audio`。
- **换渲染器**：任何能消费 `ActionPlan` 的运行时都可以接，不限于 Live2D。
- **改规则后**：当前版本重启 `bridge.py` 生效；`GiftDirector.reload()` 已预留，可后续接文件监听/后台热更新。
- **跑测试**：`python -m unittest discover -s tests -v`（只依赖标准库）。

> `gift_value` 口径：当前按 bridge 收到的字段直接匹配。如果 `reader.py` 传的是“单价”而不是“总价”，高价值礼物的 `value` 规则会偏小；请在采集侧乘 `gift_num`，或在 `director.py` 里统一换算。
>
> Live2D 动作组因模型而异。Hiyori 官方模型只有 `Idle`（9 个）和 `TapBody`（1 个，下标 0），所以默认配置全部用 `TapBody[0]`；不同礼物靠特效/浮层区分。换模型后先运行 `python inspect_live2d.py`，再按输出改配置里的 `group` / `index`。如果组不存在，motion 会静默失败，但特效和浮层仍会播放。

### 模块边界与失败降级

| 模块 | 文件 | 职责 |
|---|---|---|
| 规则/调度 | `director.py` | 匹配、优先级、冷却、打断、队列；不碰渲染 |
| 接入/发布 | `bridge.py` | 收礼物、发布 ActionPlan、后台 tick；不碰具体特效 |
| 时间轴播放 | `static/gift-player.js` | 消费 ActionPlan，调度 motion/effect/video/toast |
| 粒子/浮层 | `static/gift-effects.js` | canvas-confetti 粒子、CSS 兜底、视频 overlay |
| 规则配置 | `config/gift_actions.json` | 运营可改的礼物→演出映射 |
| 视频素材 | `static/videos/` | 大礼物 WebM/MP4 |

失败降级：

- `GiftDirector` 配置读取失败：保留旧规则，`/api/state.director.last_error` 可查
- 导演处理异常：不影响礼物进入原来的 DeepSeek/TTS 感谢链路
- `canvas-confetti` 加载失败：自动回退到 `gift-effects.js` 的 CSS 特效
- Live2D 动作组不存在：motion 静默失败，特效/浮层继续
- 视频加载失败：打印 `[GiftEffects] video failed`，按 `duration` 自动清理
- 前端动作播放异常：`GiftPlayer` 捕获单个 step 的异常，不阻塞后续动作

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
| `DEEPSEEK_STREAM` | `1`（默认）流式生成 + 按句合成，第一句生成完就开口；`0` 整段生成后再合成 |
| `BRIDGE_MIN_INTERVAL` | 两条回复最少间隔秒数（默认 3，在后台节奏控制里生效，礼物不受限） |
| `BRIDGE_MAX_QUEUE` | 排队上限（默认 20） |
| `BRIDGE_CHAT_TTL` / `BRIDGE_GIFT_TTL` | 弹幕 / 礼物感谢排队多久后放弃（默认 30s / 120s） |
| `BRIDGE_MAX_BODY` | 请求体上限字节数（默认 8192） |
| `BRIDGE_TOKEN` | **只读**视图令牌；绑非回环地址时必填且 ≥16 位 |
| `BRIDGE_ALLOW_UNAUTHENTICATED` | 绑非回环地址且不设令牌时，显式接受「局域网可只读访问」 |

### 回复调度

入口只负责入队，从不因「回得太快」丢弃消息；后台按以下规则挑下一条：

- **优先级**：礼物 > VIP（`priority: "vip"`）> 提问/点名（含 `？`、`吗`、`小雅`、`主播`、`@` 等）> 普通弹幕/播报，同级先来先回
- **过期**：排队超过 TTL 的直接丢弃，不回复过时弹幕
- **去重**：带 `event_id` 的按它去重（120s 窗口）；相同弹幕文本 10s 内只回一次（防刷屏）
- **合并**：同一观众排队中的礼物合并成一次感谢（同名礼物合计数量）

`/api/state` 的 `queue` 字段给出当前深度和 `expired` / `evicted` / `deduped` / `rejected` 计数；`timings` 里的 `first_audio` 是从开始处理到第一句语音合成完成的耗时。

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

换完模型后先看它到底有哪些动作组和下标：

```bash
python inspect_live2d.py
# 或只看某个模型
python inspect_live2d.py Hiyori
```

然后把 `config/gift_actions.json` 里的 `"group": "TapBody", "index": 0` 改成实际存在的组/下标。不同模型的组名和数量完全不同，不能直接照搬 Hiyori。

## 许可与致谢

- 本项目代码：MIT
- Live2D 模型（Hiyori）：Live2D Inc. 官方示例模型，遵循 Live2D 许可
- PixiJS：MIT · pixi-live2d-display：MIT · Live2D Cubism Core：Live2D Inc.
- 大礼物粒子特效：[canvas-confetti](https://github.com/catdad/canvas-confetti)，ISC License，见 `static/vendor/canvas-confetti.LICENSE`
- 弹幕采集依赖：[douyinLive](https://github.com/jwwsjlm/douyinLive)、[cyber-xiaoya-danmaku](https://github.com/cyber-xiaoya/cyber-xiaoya-danmaku)（各自遵循其许可证）

> ⚠️ 请遵守各平台用户协议，控制抓取频率，勿用于违规用途。
