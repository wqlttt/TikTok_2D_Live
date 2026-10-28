#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台 TTS 后端。

把「文本 -> wav 文件」从 bridge.py 里抽出来，按平台选择实现：

  piper  本地 Piper ONNX，跨平台，音质最好（需额外下载模型，见 setup_piper.sh）
  say    macOS 原生 `say` 命令，零依赖
  sapi   Windows 原生 SAPI5，经 PowerShell 调 System.Speech，零安装
  vidu   云端语音合成（不在本模块实现，由 bridge.py 的 vidu 分支处理）

`TTS_BACKEND=auto`（默认）按顺序探测：
  1. Piper —— 仅当 piper 包可导入**且**音色文件存在
  2. macOS -> say
  3. Windows -> sapi

诊断命令：
  python tts_backend.py                    # 打印平台、生效后端、可用音色
  python tts_backend.py --list-voices
  python tts_backend.py --say "你好呀"
"""

import argparse
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "audio")
PIPER_DIR = os.path.join(BASE_DIR, "piper_voices")
PIPER_MODEL = os.path.join(PIPER_DIR, "zh_CN-xiao_ya-medium.onnx")
WIN_TTS_PS1 = os.path.join(BASE_DIR, "tools", "win_sapi_tts.ps1")

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

SAPI_TIMEOUT = float(os.environ.get("SAPI_TTS_TIMEOUT", "30") or 30)
# 单条朗读文本上限：直播回复本来就短，超长只可能是异常输入（刷屏 / 注入）
SAPI_MAX_CHARS = int(os.environ.get("SAPI_TTS_MAX_CHARS", "800") or 800)
VALID_BACKENDS = ("piper", "say", "sapi", "vidu")

# .env 由 verify_pipeline 统一加载；这里再兜底一次，保证本模块可独立运行
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv 缺失不阻断
    pass


def env(name, default=""):
    """读环境变量；空串视为未设置。"""
    return (os.environ.get(name) or default).strip()


# ---------------------------------------------------------------- 平台辅助


def configure_console():
    """避免 Windows 下输出重定向时因 GBK 编码抛 UnicodeEncodeError。

    只放宽 errors，不改 encoding —— 保持控制台原有代码页，
    中文仍能正常显示，无法编码的 emoji 降级为占位符而不是崩溃。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def _hidden_kwargs():
    """Windows 上不弹控制台黑框（pythonw 启动时尤其明显）。"""
    if IS_WIN:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def _new_wav_path():
    os.makedirs(AUDIO_DIR, exist_ok=True)
    name = "reply_%d_%s.wav" % (int(time.time() * 1000), uuid.uuid4().hex[:6])
    return os.path.join(AUDIO_DIR, name)


def _powershell_exe():
    """定位 PowerShell；优先 Windows PowerShell 5.1，其次 PowerShell 7。"""
    for cand in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        p = shutil.which(cand)
        if p:
            return p
    raise RuntimeError("未找到 PowerShell，Windows SAPI 后端不可用")


# ---------------------------------------------------------------- 后端探测


def piper_available():
    """Piper 是否真的可用：包能导入 + 音色文件存在（不触发重型 import）。"""
    try:
        if importlib.util.find_spec("piper") is None:
            return False
    except (ImportError, ValueError):
        return False
    return os.path.isfile(PIPER_MODEL)


def resolve_backend(raw=None):
    """把配置值解析成实际生效的后端名。

    auto  / 空       -> 按平台与依赖探测
    local            -> 旧版兼容别名，等价于平台原生（say / sapi）
    piper/say/sapi/vidu -> 显式指定，平台不符时报错而不是静默降级
    """
    name = (raw if raw is not None else env("TTS_BACKEND", "auto")).strip().lower()

    if name in ("", "auto"):
        if piper_available():
            return "piper"
        if IS_MAC:
            return "say"
        if IS_WIN:
            return "sapi"
        raise RuntimeError("当前平台无可用本地 TTS，请安装 piper 或显式设置 TTS_BACKEND")

    if name == "local":
        # 旧版 .env 里写 TTS_BACKEND=local 的按平台原生处理
        if IS_MAC:
            return "say"
        if IS_WIN:
            return "sapi"
        return "piper"

    if name not in VALID_BACKENDS:
        raise ValueError(
            "TTS_BACKEND=%r 无效，可选：auto / piper / say / sapi / vidu / local" % name)

    if name == "say" and not IS_MAC:
        raise ValueError("TTS_BACKEND=say 仅 macOS 可用；Windows 请用 sapi 或 piper")
    if name == "sapi" and not IS_WIN:
        raise ValueError("TTS_BACKEND=sapi 仅 Windows 可用；macOS 请用 say 或 piper")
    return name


# ---------------------------------------------------------------- 音色


_default_voice_cache = None


def _detect_win_voice():
    """未配置 WIN_VOICE 时，自动挑第一个中文音色。"""
    try:
        voices = list_voices("sapi")
    except Exception:
        return ""
    for name, culture, _gender in voices:
        if (culture or "").lower().startswith("zh"):
            return name
    return voices[0][0] if voices else ""


def default_voice():
    """当前平台默认音色（macOS: SAY_VOICE / Windows: WIN_VOICE 或自动探测）。"""
    global _default_voice_cache
    if _default_voice_cache is not None:
        return _default_voice_cache
    if IS_MAC:
        voice = env("SAY_VOICE", "Tingting")
    elif IS_WIN:
        voice = env("WIN_VOICE", "") or _detect_win_voice()
    else:
        voice = env("WIN_VOICE", "")
    _default_voice_cache = voice
    return voice


def pick_local_voice(content):
    """按弹幕关键词选本地音色，返回 (voice, 标签)。

    映射由环境变量提供：macOS 读 SAY_VOICE_MAP，Windows 读 WIN_VOICE_MAP，
    形如 {"御姐":"Microsoft Huihui Desktop"}。长关键词优先匹配。
    """
    default = default_voice()
    raw = env("WIN_VOICE_MAP" if IS_WIN else "SAY_VOICE_MAP", "")
    mapping = {}
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                mapping = loaded
        except Exception:
            mapping = {}
    for label in sorted(mapping, key=len, reverse=True):
        if label and label in content:
            return mapping[label], label
    return default, "默认"


def list_voices(backend=None):
    """列出音色，返回 [(name, culture, gender), ...]。"""
    backend = backend or resolve_backend()
    if backend == "sapi":
        if not IS_WIN:
            return []
        proc = subprocess.run(
            [_powershell_exe(), "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", WIN_TTS_PS1, "-ListVoices"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, **_hidden_kwargs())
        if proc.returncode != 0:
            raise RuntimeError("列出音色失败：%s" % (proc.stderr or "").strip()[:300])
        result = []
        for line in (proc.stdout or "").splitlines():
            parts = line.strip().split("\t")
            if len(parts) == 3 and parts[0]:
                result.append((parts[0], parts[1], parts[2]))
        return result
    if backend == "say":
        if not IS_MAC:
            return []
        proc = subprocess.run(["say", "-v", "?"], capture_output=True, text=True,
                              timeout=30)
        result = []
        for line in (proc.stdout or "").splitlines():
            m = re.match(r"^(.+?)\s{2,}([A-Za-z_-]+)\s*(?:#|$)", line.strip())
            if m:
                result.append((m.group(1).strip(), m.group(2), ""))
        return result
    if backend == "piper":
        return [("zh_CN-xiao_ya-medium", "zh-CN", "Female")] if piper_available() else []
    return []


# ---------------------------------------------------------------- 各后端实现

_PIPER_VOICE = None


def _load_piper():
    """懒加载 Piper 音色，只加载一次。"""
    global _PIPER_VOICE
    if _PIPER_VOICE is None:
        try:
            from piper import PiperVoice
        except ImportError as e:
            raise RuntimeError(
                'Piper 未安装：pip install "piper-tts[zh]" unicode-rbnf，'
                '或把 .env 里的 TTS_BACKEND 改为 auto') from e
        if not os.path.isfile(PIPER_MODEL):
            raise RuntimeError(
                "缺少 Piper 音色文件 %s，请先运行 setup_piper.sh（Windows 需手动下载）"
                % PIPER_MODEL)
        _PIPER_VOICE = PiperVoice.load(PIPER_MODEL, download_dir=PIPER_DIR)
    return _PIPER_VOICE


def piper_tts(text):
    """本地 Piper TTS：预热后极快（约 20 倍实时），输出 22050Hz 单声道 wav。"""
    import wave

    import numpy as np

    voice = _load_piper()
    out = _new_wav_path()
    chunks = list(voice.synthesize(text))
    if not chunks:
        raise RuntimeError("Piper 未返回音频")
    # piper-tts >=1.2 返回 AudioChunk(audio_int16_array=...)，旧版返回裸 PCM
    if hasattr(chunks[0], "audio_int16_array"):
        pcm = np.concatenate([c.audio_int16_array for c in chunks])
        rate = getattr(chunks[0], "sample_rate", 22050) or 22050
    else:
        pcm = np.concatenate([np.frombuffer(bytes(c), dtype=np.int16) for c in chunks])
        rate = 22050
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype(np.int16).tobytes())
    return out


def say_tts(text, voice=None):
    """macOS 原生 TTS。"""
    if not IS_MAC:
        raise RuntimeError("say 后端仅 macOS 可用")
    out = _new_wav_path()
    subprocess.run(
        ["say", "-v", voice or default_voice(), "-o", out,
         "--file-format=WAVE", "--data-format=LEI16@22050", text],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out


def sapi_tts(text, voice=None):
    """Windows SAPI5 TTS（PowerShell + System.Speech）。

    文本经 UTF-8 临时文件传入，绝不进命令行 —— 弹幕内容不可控，
    走参数会暴露给 PowerShell 解析器。
    """
    if not IS_WIN:
        raise RuntimeError("sapi 后端仅 Windows 可用")
    if not os.path.isfile(WIN_TTS_PS1):
        raise RuntimeError("缺少 %s，请确认仓库完整" % WIN_TTS_PS1)

    if len(text) > SAPI_MAX_CHARS:
        text = text[:SAPI_MAX_CHARS]

    out = _new_wav_path()
    fd, tmp = tempfile.mkstemp(prefix="dh_tts_", suffix=".txt")
    try:
        # 直接用 mkstemp 返回的 fd 打开，避免 close 与 open 之间文件被替换（TOCTOU）
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        cmd = [_powershell_exe(), "-NoProfile", "-NonInteractive",
               "-ExecutionPolicy", "Bypass", "-File", WIN_TTS_PS1,
               "-TextFile", tmp, "-OutFile", out]
        chosen = voice or default_voice()
        if chosen:
            cmd += ["-Voice", chosen]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=SAPI_TIMEOUT, **_hidden_kwargs())
        if proc.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) == 0:
            raise RuntimeError("SAPI TTS 失败(rc=%s)：%s"
                               % (proc.returncode, (proc.stderr or "").strip()[:300]))
        return out
    except BaseException:
        # 失败时清掉可能残留的半截 wav，避免前端播放到损坏文件
        try:
            if os.path.isfile(out):
                os.remove(out)
        except OSError:
            pass
        raise
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def synthesize(text, voice=None, backend=None):
    """统一入口：文本 -> wav 路径。"""
    backend = backend or resolve_backend()
    if backend == "piper":
        return piper_tts(text)
    if backend == "say":
        return say_tts(text, voice)
    if backend == "sapi":
        return sapi_tts(text, voice)
    raise ValueError("synthesize() 不支持后端 %r（vidu 由 bridge.py 处理）" % backend)


def warmup(backend=None):
    """预热 Piper（加载模型 + 跑一次空合成）。

    SAPI 每次调用都是独立进程，预热无意义，故不做。
    """
    backend = backend or resolve_backend()
    if backend != "piper":
        return
    try:
        voice = _load_piper()
        list(voice.synthesize("。"))
    except Exception:
        pass


# ---------------------------------------------------------------- CLI


def main():
    ap = argparse.ArgumentParser(description="跨平台 TTS 诊断工具")
    ap.add_argument("--backend", default=None, help="覆盖 TTS_BACKEND")
    ap.add_argument("--list-voices", action="store_true", help="列出可用音色")
    ap.add_argument("--say", metavar="TEXT", default=None, help="合成一段文本试听")
    ap.add_argument("--voice", default=None, help="指定音色")
    args = ap.parse_args()

    configure_console()
    print("平台      : %s" % platform.platform())
    print("脚本目录  : %s" % BASE_DIR)

    try:
        backend = resolve_backend(args.backend)
    except (ValueError, RuntimeError) as e:
        print("后端解析失败: %s" % e)
        return 1
    print("生效后端  : %s" % backend)
    print("默认音色  : %s" % (default_voice() or "(未配置)"))

    if args.list_voices:
        print("\n可用音色：")
        for name, culture, gender in list_voices(backend):
            print("  %-34s %-8s %s" % (name, culture, gender))

    if args.say:
        print("\n合成中…")
        try:
            t0 = time.time()
            path = synthesize(args.say, args.voice, backend)
        except Exception as e:  # noqa: BLE001
            print("合成失败: %s" % e)
            return 1
        print("已生成: %s (%d 字节, 耗时 %.2fs)"
              % (path, os.path.getsize(path), time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
