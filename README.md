# TikTok 2D Live · 抖音 AI 数字人直播

在 macOS（Apple Silicon）本地运行的抖音弹幕互动数字人：

**抖音弹幕 → DeepSeek 生成回复（带人设+记忆+昵称）→ 本地 TTS → Live2D 口型 → OBS/直播伴侣推流**

重型渲染全部在本地浏览器完成（Live2D WebGL），**不需要 CUDA、不需要云端渲染**，一台 Mac Mini/M4 即可 24 小时运行。

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
- **本地 TTS**：macOS 自带 `say`，零依赖（可替换 fireredtts3-onnx）
- **Live2D Cubism 4**：官方示例模型 Hiyori，音频实时驱动口型，内置自动眨眼/呼吸/头发物理
- **弹幕飘屏** + 回复字幕
- **OBS 浏览器源**接入，无需推流码

## 目录结构

```
├── bridge.py            # 数字人桥：/api/danmaku 收弹幕 → DeepSeek → TTS → 状态/音频服务
├── verify_pipeline.py   # 最小验证脚本（一次性走通链路 + 延迟统计）
├── avatar.html          # Live2D 播放器（渲染 + 口型 + 弹幕飘屏 + 字幕）
├── setup.sh             # 一键部署脚本（下载运行库/模型/弹幕依赖）
├── config/
│   ├── config.example.yaml   # 主配置模板
│   └── persona_prompt.md     # DeepSeek 人设提示词
├── patches/winsound.py       # macOS 兼容补丁（见下）
├── live2d/                   # Live2D 模型与运行库（本地已就绪）
│   ├── pixi.min.js           #   PixiJS v6
│   ├── live2dcubismcore.min.js  # Live2D Cubism Core
│   ├── cubism4.min.js        #   pixi-live2d-display
│   └── Hiyori/               #   官方示例模型
├── 部署指南.md
└── 风险与备选说明.md
```

## 快速开始

### 0. 一键部署（推荐）

```bash
bash setup.sh
```

脚本会自动：下载 Live2D 运行库（PixiJS + Cubism Core + 渲染库）、下载 8 个官方模型、克隆并编译弹幕采集依赖、生成 `.env`。

> 模型约 24MB、不在本仓库内，由脚本从官方仓库拉取；嫌慢也可手动只下你需要的几个（见 §换 Live2D 模型）。

### 1. 环境

- macOS（Apple Silicon）
- Python 3.10+（本项目在 conda `usually` 环境实测），依赖 `requests`、`python-dotenv`
- Go 1.26+（编译 douyinLive 用）
- 可选：OBS（Apple Silicon 版）、抖音直播伴侣

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env，填入：
#   DEEPSEEK_API_KEY      DeepSeek 平台申请的 Key
#   （可选）PERSONA_PROMPT  自定义人设，默认已内置
```

### 3. 启动数字人桥

```bash
source /opt/anaconda3/etc/profile.d/conda.sh
conda activate usually          # 或你自己的 Python 环境
python bridge.py
# 打开 http://127.0.0.1:8000/ 即可看到 Live2D 虚拟人
```

此时可先用 curl 模拟弹幕验证：

```bash
curl -X POST http://127.0.0.1:8000/api/danmaku \
  -H 'Content-Type: application/json' \
  -d '{"nick":"测试","content":"主播你好"}'
```

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
./douyinLive/douyinLive --config ./douyinLive/config.yaml     # ① 采集服务
python bridge.py                                             # ② 数字人桥
cd cyber-xiaoya-danmaku && python reader.py                  # ③ 弹幕转发
```

### 5. OBS / 抖音直播伴侣

1. OBS → 来源 `+` → 「浏览器」→ URL `http://127.0.0.1:8000/`（勾选「控制音频」）
2. OBS「工具 → 虚拟摄像机」→ 启动
3. 抖音直播伴侣 → 添加素材 → 「摄像头」→ 选 `OBS Virtual Camera`

> 声音回传可用 BlackHole 虚拟声卡（`brew install blackhole-2ch`）。

## 配置速查

| 变量 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API Key（必填） |
| `DEEPSEEK_MODEL` | `deepseek-chat`（快）/ `deepseek-reasoner`（慢） |
| `DEEPSEEK_MAX_TOKENS` / `DEEPSEEK_TEMPERATURE` | 回复长度 / 随机性 |
| `HISTORY_TURNS` | 记忆轮数（默认 8） |
| `TTS_BACKEND` | `local`（macOS say）/ `vidu`（云端对口型，可选） |
| `SAY_VOICE` | macOS `say` 音色（默认 `Tingting`） |
| `SAY_VOICE_MAP` | 弹幕关键词→音色（JSON） |
| `BRIDGE_HOST` / `BRIDGE_PORT` | 桥监听地址/端口 |

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
