#!/usr/bin/env bash
# ============================================================
#  赛博小雅 · 一键部署脚本
#  用法：bash setup.sh
#  作用：下载 Live2D 运行库 + 8 个官方模型 + 弹幕采集依赖 + 生成配置
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

info()  { echo -e "\033[1;32m==>\033[0m $*"; }
warn()  { echo -e "\033[1;33m⚠️\033[0m  $*"; }
err()   { echo -e "\033[1;31m❌\033[0m $*"; exit 1; }

echo "=============================================="
echo "  赛博小雅 · 一键部署"
echo "=============================================="

# ---------- 1. 依赖检查 ----------
info "检查依赖..."
command -v python3 >/dev/null || err "缺少 python3"
command -v git     >/dev/null || err "缺少 git"
command -v curl    >/dev/null || err "缺少 curl"
command -v go      >/dev/null || warn "缺少 go（编译 douyinLive 需要，可稍后装）"
command -v say     >/dev/null || warn "缺少 say（本地 TTS，仅 macOS）"
echo "   依赖检查完成"

# ---------- 2. Python 依赖 ----------
info "安装 Python 依赖 (requests / python-dotenv)..."
python3 -m pip install -q requests python-dotenv 2>/dev/null || \
  warn "pip 安装失败，请手动执行：pip install requests python-dotenv"

# ---------- 3. 下载 Live2D 运行库 ----------
info "下载 Live2D 运行库 (PixiJS + Cubism Core + 渲染库)..."
mkdir -p live2d
curl -L --fail -sS -o live2d/pixi.min.js \
  "https://cdn.jsdelivr.net/npm/pixi.js@6.5.10/dist/browser/pixi.min.js"
curl -L --fail -sS -o live2d/cubism4.min.js \
  "https://cdn.jsdelivr.net/npm/pixi-live2d-display@0.4.0/dist/cubism4.min.js"
curl -L --fail -sS -o live2d/live2dcubismcore.min.js \
  "https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js"
echo "   运行库下载完成"

# ---------- 4. 下载 Live2D 模型 ----------
info "下载 Live2D 模型 (8 个官方示例，约 24MB)..."
MODELS="Hiyori Haru Mao Mark Natori Ren Rice Wanko"
TMP=".live2d_tmp"
rm -rf "$TMP"
git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/Live2D/CubismWebSamples.git "$TMP" 2>&1 | tail -1
(cd "$TMP" && git sparse-checkout set $(for m in $MODELS; do echo "Samples/Resources/$m"; done))
for m in $MODELS; do
  cp -r "$TMP/Samples/Resources/$m" "live2d/$m"
done
rm -rf "$TMP"
echo "   模型下载完成"

# ---------- 5. 弹幕采集依赖（可选） ----------
info "准备弹幕采集依赖 (可选，接真实抖音弹幕才需要)..."
if [ ! -d douyinLive ]; then
  git clone --depth 1 https://github.com/jwwsjlm/douyinLive.git 2>&1 | tail -1
  (cd douyinLive && go build -o douyinLive ./cmd/main 2>&1 | tail -1 || warn "douyinLive 编译失败（需要 go）")
  cp -f douyinLive/config.example.yaml douyinLive/config.yaml
  # 端口改成 1089（reader 期望的端口）
  python3 - <<'PY'
p = "douyinLive/config.yaml"
s = open(p).read().replace('port: "1088"', 'port: "1089"')
open(p, "w").write(s)
PY
fi
if [ ! -d cyber-xiaoya-danmaku ]; then
  git clone https://github.com/cyber-xiaoya/cyber-xiaoya-danmaku.git 2>&1 | tail -1
  # macOS 兼容补丁（winsound 是 Windows 专有模块）
  cp -f patches/winsound.py cyber-xiaoya-danmaku/winsound.py
  # 生成 reader 配置：转发到本机桥 :8000，只启用抖音
  (cd cyber-xiaoya-danmaku && python3 - <<'PY'
import json
c = json.load(open("config.example.json"))
c["tts"]["dh_url"] = "http://127.0.0.1:8000"
c["bilibili"]["enabled"] = False
c["wechat"]["enabled"] = False
c["douyin"]["enabled"] = True
json.dump(c, open("config.json", "w"), ensure_ascii=False, indent=2)
PY
  )
fi
echo "   弹幕采集依赖准备完成"

# ---------- 6. 生成 .env ----------
info "生成 .env ..."
[ -f .env ] || cp .env.example .env

echo ""
echo "=============================================="
echo "  ✅ 部署完成！"
echo "=============================================="
echo ""
echo "接下来手动做两件事："
echo "  1) 编辑 .env 填入 DEEPSEEK_API_KEY"
echo "  2) (接真实抖音弹幕) 编辑 cyber-xiaoya-danmaku/config.json 填 douyin.room_id"
echo ""
echo "启动："
echo "  python bridge.py          # 打开 http://127.0.0.1:8000/"
echo "  详细见 README.md"
