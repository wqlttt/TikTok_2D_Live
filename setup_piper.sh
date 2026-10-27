#!/usr/bin/env bash
# ============================================================
#  可选：安装本地 Piper TTS（音质优于 macOS say，需下载约 280MB 模型）
#  用法：bash setup_piper.sh
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

MIRROR="${HF_MIRROR:-https://hf-mirror.com}"   # 国内镜像；可 HF_MIRROR=https://huggingface.co 直连

info() { echo -e "\033[1;32m==>\033[0m $*"; }

info "安装 piper-tts + 中文注音依赖 (g2pw)..."
python3 -m pip install -q "piper-tts[zh]" unicode-rbnf

info "下载中文音色 zh_CN-xiao_ya（小雅，~63MB）..."
mkdir -p piper_voices
BASE="$MIRROR/rhasspy/piper-voices/resolve/main/zh/zh_CN/xiao_ya/medium"
curl -L --fail -C - -o piper_voices/zh_CN-xiao_ya-medium.onnx \
  "$BASE/zh_CN-xiao_ya-medium.onnx?download=true"
curl -L --fail -C - -o piper_voices/zh_CN-xiao_ya-medium.onnx.json \
  "$BASE/zh_CN-xiao_ya-medium.onnx.json?download=true"

info "下载 g2pw 多音字模型（~160MB）..."
mkdir -p piper_voices/g2pW
curl -L --fail -C - -o /tmp/g2pw.tar.gz \
  "$MIRROR/datasets/rhasspy/piper-checkpoints/resolve/main/zh/zh_CN/_resources/g2pw.tar.gz?download=true"
tar -xzf /tmp/g2pw.tar.gz -C piper_voices/g2pW
rm -f /tmp/g2pw.tar.gz

info "写入离线模式（避免 bert tokenizer 联网卡住）..."
grep -q "HF_HUB_OFFLINE" .env 2>/dev/null || cat >> .env <<'EOF'

# 让 transformers/piper 走本地缓存不联网
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
EOF
grep -q "^TTS_BACKEND=" .env 2>/dev/null && sed -i '' 's/^TTS_BACKEND=.*/TTS_BACKEND=piper/' .env 2>/dev/null || sed -i 's/^TTS_BACKEND=.*/TTS_BACKEND=piper/' .env 2>/dev/null || echo "TTS_BACKEND=piper" >> .env

echo ""
echo "✅ Piper TTS 就绪。TTS_BACKEND 已设为 piper。"
